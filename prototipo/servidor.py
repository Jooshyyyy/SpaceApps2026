"""Servidor local del prototipo: interfaz web + backend del equipo + reloj USB.

- Sirve la interfaz (app.html, mission.json) y las capas del backend
  (elevation.bin, slope.bin de SpaceApps2026/data/processed).
- Lanza el Edge Bus del equipo (communication/eva_bus, capa 3) y se conecta a él
  como cliente WebSocket NATIVO: el bus rechaza navegadores (cabecera Origin), así
  que el navegador habla con este servidor por HTTP y este con el bus.
    GET  /bus?rev=N  -> estado: hello, telemetría 10 Hz decodificada, último plan
                        (solo si su revisión es distinta de N), últimos eventos.
    POST /bus/cmd    -> reenvía un control JSON al bus (position, set_goal, storm,
                        radiation_peak, comms_lost).
- Reenvía al ESP32 por USB cada trama del reloj: POST /w.
Solo librería estándar.

Uso:  python3 servidor.py [--backend ../SpaceApps2026] [--port 8791] [--tty /dev/ttyUSB0]
"""
import argparse
import base64
import http.server
import json
import os
import select
import socket
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_LINE = 200
WATCH_UDP_PORT = 4210  # el reloj se anuncia por broadcast ("$H,reloj") y recibe las tramas por WiFi
CONTROL_TYPES = {"position", "set_goal", "storm", "radiation_peak", "comms_lost"}
FLAG_NAMES = ["STORM", "RAD_PEAK", "O2_LOW", "BATT_LOW", "COMMS_LOST", "ROUTE_STALE", "EVA_ABORT"]


# ---------------------------------------------------------------- reloj USB
class SerialPort:
    def __init__(self, path: str):
        self.path = path
        self.fd = None
        self.lock = threading.Lock()
        self.sent = 0
        self.error = ""
        self.buttons = []   # eventos de botones del reloj: {"seq", "t", "button"}
        self.seq = 0
        self.wifi_addr = None  # (ip, puerto) del reloj por WiFi, aprendido de su anuncio
        self.wifi_seen = 0.0
        self.last_btn = {}     # anti-duplicados: el reloj manda cada botón 3 veces por UDP
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind(("0.0.0.0", WATCH_UDP_PORT))
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._udp_reader, daemon=True).start()

    def _button(self, name: str, via: str):
        with self.lock:
            now = time.time()
            if via == "wifi" and now - self.last_btn.get(name, 0) < 1.0:
                return
            self.last_btn[name] = now
            self.seq += 1
            self.buttons = (self.buttons + [{"seq": self.seq, "t": now, "button": name, "via": via}])[-20:]

    def wifi_ok(self) -> bool:
        return self.wifi_addr is not None and time.time() - self.wifi_seen < 6

    def _udp_reader(self):
        """Reloj por WiFi: anuncios "$H,reloj" y botones "$B,SOS" del mismo equipo."""
        while True:
            try:
                data, addr = self.udp.recvfrom(256)
            except OSError:
                time.sleep(1)
                continue
            for line in data.split(b"\n"):
                line = line.strip()
                if line == b"$H,reloj":
                    with self.lock:
                        self.wifi_addr, self.wifi_seen = addr, time.time()
                elif (line.startswith(b"$B,") and line[3:].isalnum() and len(line) < 24
                      and self.wifi_addr and addr[0] == self.wifi_addr[0]):
                    self._button(line[3:].decode(), "wifi")

    def _reader(self):
        """Lee las líneas que manda el ESP32 ("$B,SOS") y las guarda como eventos."""
        buf = b""
        while True:
            with self.lock:
                if self.fd is None:
                    try:
                        self._open()
                    except OSError as exc:
                        self.error = str(exc)
                fd = self.fd
            if fd is None:
                time.sleep(1.0)
                continue
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    continue
                chunk = os.read(fd, 256)
            except OSError:
                with self.lock:
                    if self.fd == fd:
                        os.close(fd)
                        self.fd = None
                continue
            if not chunk:
                time.sleep(0.2)
                continue
            buf = (buf + chunk)[-512:]
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"$B,") and line[3:].isalnum() and len(line) < 24:
                    self._button(line[3:].decode(), "usb")

    def events_since(self, since: int):
        with self.lock:
            return [e for e in self.buttons if e["seq"] > since]

    def _open(self):
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0                          # crudo
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL      # 8N1
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        self.fd = fd

    def write(self, data: bytes) -> bool:
        """Manda la trama por WiFi (si el reloj se anunció) y por USB (si está conectado)."""
        ok = False
        if self.wifi_ok():
            try:
                self.udp.sendto(data, self.wifi_addr)
                ok = True
            except OSError:
                pass
        with self.lock:
            try:
                if self.fd is None:
                    self._open()
                os.write(self.fd, data)
                ok = True
            except OSError as exc:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
                if not ok:
                    self.error = str(exc)
            if ok:
                self.sent += 1
                self.error = ""
        return ok

    def status(self) -> dict:
        wifi = self.wifi_ok()
        return {"tty": self.path, "open": self.fd is not None or wifi, "usb": self.fd is not None,
                "wifi": self.wifi_addr[0] if wifi else None, "sent": self.sent, "error": self.error}


# ---------------------------------------------------------------- Edge Bus
def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def decode_frame(buf: bytes):
    """Trama de telemetría v1 (docs/telemetry-frame.md). None si es inválida."""
    if len(buf) != 24 or buf[0] != 0x4D or buf[1] != 1 or buf[21] != 0:
        return None
    if crc16_ccitt_false(buf[:22]) != struct.unpack_from("<H", buf, 22)[0]:
        return None
    (_, _, seq, t_ms, x, y, alt_dm, o2, batt, rad_cs, ttr, flags, _, _) = struct.unpack(
        "<BBHIHHhBBHHBBH", buf)
    return {"seq": seq, "t_ms": t_ms, "pos_x": x, "pos_y": y, "alt_dm": alt_dm,
            "o2": None if o2 == 0xFF else o2 / 200, "batt": None if batt == 0xFF else batt / 200,
            "rad_msv": None if rad_cs == 0xFFFF else rad_cs / 100,
            "ttr_s": None if ttr == 0xFFFF else ttr, "flags": flags,
            "flag_names": [n for i, n in enumerate(FLAG_NAMES) if flags >> i & 1]}


class BusClient:
    """Cliente WebSocket mínimo (RFC 6455) hacia eva_bus en 127.0.0.1."""

    def __init__(self, port: int):
        self.port = port
        self.sock = None
        self.lock = threading.Lock()
        self.state = {"connected": False, "hello": None, "telemetry": None, "route": None,
                      "events": [], "error": ""}
        threading.Thread(target=self._run, daemon=True).start()

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("el bus cerró el handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("handshake rechazado: " + head.split(b"\r\n", 1)[0].decode())
        s.settimeout(None)
        return s, head.split(b"\r\n\r\n", 1)[1]

    def _run(self):
        while True:
            try:
                sock, rest = self._connect()
                with self.lock:
                    self.sock = sock
                    self.state.update(connected=True, error="")
                self._read_loop(sock, rest)
            except OSError as exc:
                with self.lock:
                    self.state.update(connected=False, error=str(exc))
                    self.sock = None
            time.sleep(1.0)

    def _read_loop(self, sock, buf: bytes):
        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("bus desconectado")
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out

        message, msg_op = b"", None
        while True:
            b0, b1 = need(2)
            op, fin, ln = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", need(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", need(8))[0]
            payload = need(ln)
            if op == 0x8:
                raise ConnectionError("el bus cerró la conexión")
            if op == 0x9:
                self._send(0xA, payload)
                continue
            if op in (0x1, 0x2):
                message, msg_op = payload, op
            elif op == 0x0:
                message += payload
            else:
                continue
            if fin:
                self._handle(msg_op, message)

    def _handle(self, op, data: bytes):
        with self.lock:
            if op == 0x2:
                frame = decode_frame(data)
                if frame:
                    self.state["telemetry"] = frame
                return
            try:
                msg = json.loads(data)
            except ValueError:
                return
            kind = msg.get("type")
            if kind == "hello":
                self.state["hello"] = msg
            elif kind == "route_update":
                self.state["route"] = msg
            if kind in ("event_ack", "error", "route_update"):
                ev = {k: v for k, v in msg.items() if k != "result"}
                self.state["events"] = (self.state["events"] + [ev])[-12:]

    def _send(self, op: int, payload: bytes):
        with self.lock:
            sock = self.sock
        if sock is None:
            raise ConnectionError("bus no conectado")
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_json(self, obj: dict):
        self._send(0x1, json.dumps(obj).encode())

    def clear(self):
        with self.lock:
            self.state.update(hello=None, telemetry=None, route=None, events=[])
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass

    def snapshot(self, known_rev):
        with self.lock:
            st = dict(self.state)
        route = st.pop("route")
        st["revision"] = route["revision"] if route else None
        st["route"] = route if route and route["revision"] != known_rev else None
        return st


def start_bus(backend: Path, port: int, goal: str):
    data = backend / "data" / "processed"
    cmd = [str(backend / "communication" / "eva_bus"), "--dir", str(data),
           "--engine", str(backend / "core_engine" / "eva_engine"),
           "--start", "500,500", "--goal", goal, "--port", str(port)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready, _, _ = select.select([proc.stdout], [], [], 10)
    line = proc.stdout.readline() if ready else ""
    if '"listening"' not in line:
        proc.kill()
        raise SystemExit(f"eva_bus no arrancó: {line or proc.stderr.read()}")
    print("eva_bus:", line.strip(), flush=True)
    return proc


# ---------------------------------------------------------------- HTTP
class BusProcess:
    """Proceso eva_bus reiniciable: cada EVA empieza con los consumibles completos."""

    def __init__(self, backend: Path, port: int, goal):
        self.backend, self.port, self.lock = backend, port, threading.Lock()
        self.proc = start_bus(backend, port, f"{goal[0]},{goal[1]}")

    def restart(self, goal):
        with self.lock:
            self.stop()
            self.proc = start_bus(self.backend, self.port, f"{goal[0]},{goal[1]}")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Planner:
    """Vista previa de rutas: eva_engine sim --path (capa 2) para un objetivo elegido."""

    def __init__(self, backend: Path):
        self.data = backend / "data" / "processed"
        self.engine = backend / "core_engine" / "eva_engine"
        self.cost = (self.data / "cost.bin").read_bytes()

    def snap(self, r: int, c: int):
        """Celda transitable más cercana (radio 20): el punto tocado puede ser roca."""
        for rad in range(21):
            best = None
            for dr in range(-rad, rad + 1):
                for dc in range(-rad, rad + 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < 1000 and 0 <= cc < 1000 and 1 <= self.cost[rr * 1000 + cc] <= 254:
                        d = dr * dr + dc * dc
                        if best is None or d < best[0]:
                            best = (d, rr, cc)
            if best:
                return best[1], best[2]
        return None

    def plan(self, start, goal):
        cell = self.snap(*goal)
        if cell is None:
            return {"ok": False, "error": "zona intransitable (pendiente > 20°)"}
        out = subprocess.run(
            [str(self.engine), "sim", "--dir", str(self.data), "--start", f"{start[0]},{start[1]}",
             "--goal", f"{cell[0]},{cell[1]}", "--path"], capture_output=True, text=True, timeout=5)
        res = json.loads(out.stdout or out.stderr or "{}")
        res["snapped"] = {"row": cell[0], "col": cell[1]}
        return res


ORDER_TYPES = {"ret", "storm", "rad", "comms", "sos", "ack", "close"}


class Shared:
    """Estado de la EVA compartido entre pantallas: la tablet del astronauta publica
    su estado y la base (otra pantalla) lo lee y le manda órdenes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = None
        self.at = 0.0
        self.orders = []
        self.seq = 0

    def publish(self, state):
        with self.lock:
            self.state, self.at = state, time.time()

    def read(self):
        with self.lock:
            return {"state": self.state, "age": time.time() - self.at if self.state else None}

    def order(self, kind):
        with self.lock:
            self.seq += 1
            self.orders = (self.orders + [{"seq": self.seq, "type": kind}])[-40:]

    def since(self, n):
        with self.lock:
            return {"seq": self.seq, "orders": [o for o in self.orders if o["seq"] > n]}


SHARED = Shared()


def make_handler(serial: SerialPort, bus: BusClient, data_dir: Path, busproc: "BusProcess", planner: Planner):
    data_files = {"/elevation.bin", "/slope.bin"}
    owner = {"id": None}  # solo la última pantalla del astronauta que abrió la página le escribe al reloj

    def cell(text):
        r, c = (int(v) for v in text.split(","))
        if not (0 <= r < 1000 and 0 <= c < 1000):
            raise ValueError
        return r, c

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(HERE), **kwargs)

        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def end_headers(self):
            if self.path.split("?")[0].endswith((".html", ".json", ".bin")):
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":  # la tablet entra directo al prototipo
                self.send_response(302)
                self.send_header("Location", "/app.html")
                self.end_headers()
                return
            if path == "/base":  # pantalla de la base de control (observadora)
                self.send_response(302)
                self.send_header("Location", "/app.html?rol=base")
                self.end_headers()
                return
            if path == "/eva/estado":
                return self._reply(200, SHARED.read())
            if path == "/eva/ordenes":  # ?since=N
                since = query[6:] if query.startswith("since=") else "0"
                return self._reply(200, SHARED.since(int(since) if since.isdigit() else 0))
            if path == "/estado":
                return self._reply(200, serial.status())
            if path == "/reloj":  # botones del reloj: ?since=N
                since = query[6:] if query.startswith("since=") else "0"
                return self._reply(200, {"events": serial.events_since(int(since) if since.isdigit() else 0),
                                         **serial.status()})
            if path == "/plan":  # ?goal=r,c[&start=r,c]
                try:
                    q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
                    goal, start = cell(q["goal"]), cell(q.get("start", "500,500"))
                except (ValueError, KeyError):
                    return self._reply(400, {"error": "celda inválida"})
                try:
                    return self._reply(200, planner.plan(start, goal))
                except (subprocess.TimeoutExpired, ValueError) as exc:
                    return self._reply(500, {"error": str(exc)})
            if path == "/bus":
                rev = query[4:] if query.startswith("rev=") else None
                return self._reply(200, bus.snapshot(int(rev) if rev and rev.isdigit() else None))
            if path in data_files:  # capas del backend, no copias locales
                body = (data_dir / path[1:]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return super().do_GET()

        def _body(self, limit):
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length) if 0 < length <= limit else None

        def do_POST(self):
            if self.path == "/reloj/tomar":
                sid = (self._body(64) or b"").decode(errors="ignore")
                if not (sid and sid.isalnum()):
                    return self._reply(400, {"error": "sesión inválida"})
                owner["id"] = sid
                return self._reply(200, {"ok": True})
            if self.path == "/w":
                if self.headers.get("X-Sesion") != owner["id"]:  # pestañas viejas: no mezclan sus datos
                    return self._reply(409, {"error": "otra pantalla controla el reloj"})
                line = self._body(MAX_LINE)
                # Solo tramas del reloj: "$W,...\n" en ASCII imprimible.
                if not (line and line.startswith(b"$W,") and line.endswith(b"\n")
                        and all(32 <= b < 127 for b in line[:-1])):
                    return self._reply(400, {"error": "bad frame"})
                ok = serial.write(line)
                return self._reply(200 if ok else 503, serial.status())
            if self.path == "/eva/estado":  # la tablet del astronauta publica su estado
                try:
                    state = json.loads(self._body(400_000) or b"")
                except ValueError:
                    state = None
                if not isinstance(state, dict):
                    return self._reply(400, {"error": "estado inválido"})
                SHARED.publish(state)
                return self._reply(200, {"ok": True})
            if self.path == "/eva/orden":  # orden de la base hacia la tablet del astronauta
                try:
                    kind = json.loads(self._body(128) or b"").get("type")
                except (ValueError, AttributeError):
                    kind = None
                if kind not in ORDER_TYPES:
                    return self._reply(400, {"error": "orden no permitida"})
                SHARED.order(kind)
                return self._reply(200, {"ok": True})
            if self.path == "/bus/reset":  # nueva EVA: reinicia eva_bus con el objetivo elegido
                try:
                    msg = json.loads(self._body(256) or b"")
                    goal = (int(msg["row"]), int(msg["col"]))
                    if not (0 <= goal[0] < 1000 and 0 <= goal[1] < 1000):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    return self._reply(400, {"error": "objetivo inválido"})
                bus.clear()
                busproc.restart(goal)
                return self._reply(200, {"ok": True})
            if self.path == "/bus/cmd":
                raw = self._body(1024)
                try:
                    msg = json.loads(raw or b"")
                except ValueError:
                    msg = None
                if not isinstance(msg, dict) or msg.get("type") not in CONTROL_TYPES:
                    return self._reply(400, {"error": "control no permitido"})
                try:
                    bus.send_json(msg)
                except OSError as exc:
                    return self._reply(503, {"error": str(exc)})
                return self._reply(200, {"ok": True})
            return self._reply(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            first = str(args[0]) if args else ""
            if not any(p in first for p in ("/w ", "/bus", "/reloj", "/eva")):
                super().log_message(fmt, *args)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    # Dentro del repo del equipo el backend es la carpeta de arriba; en una copia suelta, ../SpaceApps2026.
    default_backend = HERE.parent if (HERE.parent / "core_engine").is_dir() else HERE.parent / "SpaceApps2026"
    ap.add_argument("--backend", type=Path, default=default_backend)
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--bus-port", type=int, default=8765)
    ap.add_argument("--tty", default="auto", help="puerto del reloj; auto = primer /dev/ttyUSB*")
    args = ap.parse_args()
    backend = args.backend.resolve()
    mission = json.loads((HERE / "mission.json").read_text())
    first = mission["stops"][0]
    busproc = BusProcess(backend, args.bus_port, (first["row"], first["col"]))
    bus = BusClient(args.bus_port)
    if args.tty == "auto":  # al reconectar el cable el ESP32 puede pasar de ttyUSB0 a ttyUSB1
        ports = sorted(Path("/dev").glob("ttyUSB*"))
        args.tty = str(ports[0]) if ports else "/dev/ttyUSB0"
    serial = SerialPort(args.tty)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(serial, bus, backend / "data" / "processed", busproc, Planner(backend)))
    print(f"Prototipo: http://127.0.0.1:{args.port}/app.html · bus ws://127.0.0.1:{args.bus_port} "
          f"· reloj {args.tty}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        busproc.stop()


if __name__ == "__main__":
    main()
