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
import mimetypes
import re as _re
import gzip
from array import array
import urllib.error
import urllib.request
import hmac
import secrets
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
WATCH_BT_NAME = "ARES-Watch"  # nombre Bluetooth del reloj (puerto serie RFCOMM, canal 1)
WATCH_UDP_PORT = 4210  # el reloj se anuncia por broadcast ("$H,reloj") y recibe las tramas por WiFi
CONTROL_TYPES = {"position", "set_goal", "storm", "radiation_peak", "comms_lost"}
FLAG_NAMES = ["STORM", "RAD_PEAK", "O2_LOW", "BATT_LOW", "COMMS_LOST", "ROUTE_STALE", "EVA_ABORT"]


# ---------------------------------------------------------------- reloj USB
class SerialPort:
    def __init__(self, path: str, hardware: bool = True):
        self.path = path
        self.hardware = hardware  # False en la nube: el reloj llega por el puente de la laptop
        self.hub_cond = threading.Condition()  # tramas para el puente (puente_reloj.py)
        self.hub_seq = 0
        self.hub_frame = b""
        self.hub_clients = 0
        self.fd = None
        self.lock = threading.Lock()
        self.sent = 0
        self.error = ""
        self.buttons = []   # eventos de botones del reloj: {"seq", "t", "button"}
        self.seq = 0
        self.wifi_addr = None  # (ip, puerto) del reloj por WiFi, aprendido de su anuncio
        self.wifi_seen = 0.0
        self.last_btn = {}     # anti-duplicados: el reloj manda cada botón 3 veces por UDP
        self.bt = None         # socket RFCOMM con el reloj
        self.bt_mac = None
        self.bt_error = ""
        self.udp = None
        if not hardware:
            return
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind(("0.0.0.0", WATCH_UDP_PORT))
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._udp_reader, daemon=True).start()
        if hasattr(socket, "AF_BLUETOOTH"):
            threading.Thread(target=self._bt_loop, daemon=True).start()

    @staticmethod
    def _bt_find() -> str | None:
        """MAC del reloj entre los dispositivos que BlueZ ya conoce (emparejado o visto en un escaneo)."""
        try:
            out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for ln in out.splitlines():
            parts = ln.split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device" and parts[2].strip() == WATCH_BT_NAME:
                return parts[1]
        return None

    def _bt_loop(self):
        """Reloj por Bluetooth: conecta al puerto serie del ESP32 y lee sus botones; reintenta si se corta."""
        while True:
            mac = self._bt_find() or self.bt_mac  # BlueZ olvida los equipos no emparejados: se recuerda el último
            if not mac:
                self.bt_error = "buscando el reloj por Bluetooth"
                try:  # escaneo corto para que BlueZ lo vuelva a ver
                    subprocess.run(["bluetoothctl", "--timeout", "8", "scan", "on"],
                                   capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    time.sleep(5)
                continue
            self.bt_mac = mac
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            try:
                sock.settimeout(10)
                sock.connect((mac, 1))
                sock.settimeout(3)  # el reloj no manda nada salvo botones: el timeout solo sirve para revisar si el enlace sigue vivo
            except OSError as exc:
                self.bt_error = str(exc)
                sock.close()
                if not self._bt_find():
                    self.bt_mac = None  # quizá cambió de reloj: volver a buscar por nombre
                time.sleep(5)
                continue
            with self.lock:
                self.bt, self.bt_error = sock, ""
            buf = b""
            try:
                while self.bt is sock:
                    try:
                        chunk = sock.recv(512)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if line.startswith(b"$B,") and line[3:].isalnum() and len(line) < 24:
                            self._button(line[3:].decode(), "bt")
                    buf = buf[-MAX_LINE:]
            except OSError as exc:
                self.bt_error = str(exc)
            with self.lock:
                if self.bt is sock:
                    self.bt = None
            sock.close()
            time.sleep(2)

    def _button(self, name: str, via: str):
        with self.lock:
            now = time.time()
            if now - self.last_btn.get(name, 0) < 1.0:  # el mismo botón llega por varias vías / repetido por UDP
                return
            self.last_btn[name] = now
            self.seq += 1
            self.buttons = (self.buttons + [{"seq": self.seq, "t": now, "button": name, "via": via}])[-20:]

    def wifi_ok(self) -> bool:
        return self.udp is not None and self.wifi_addr is not None and time.time() - self.wifi_seen < 6

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
        with self.hub_cond:  # el puente de la laptop (si está conectado) la reenvía al reloj
            self.hub_seq += 1
            self.hub_frame = data
            self.hub_cond.notify_all()
            ok = self.hub_clients > 0
        bt = self.bt
        if bt is not None:
            try:
                bt.sendall(data)
                ok = True
            except OSError as exc:  # enlace muerto (p. ej. el reloj se reinició): se descarta y el hilo reconecta
                self.bt_error = str(exc)
                with self.lock:
                    if self.bt is bt:
                        self.bt = None
                for fn in (lambda: bt.shutdown(socket.SHUT_RDWR), bt.close):
                    try:
                        fn()
                    except OSError:
                        pass
        if self.wifi_ok():
            try:
                self.udp.sendto(data, self.wifi_addr)
                ok = True
            except OSError:
                pass
        if not self.hardware:
            with self.lock:
                if ok:
                    self.sent += 1
            return ok
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
        bt = self.bt_mac if self.bt is not None else None
        return {"tty": self.path, "open": self.fd is not None or wifi or bt is not None or self.hub_clients > 0, "usb": self.fd is not None,
                "bt": bt, "bt_error": self.bt_error if bt is None else "", "puente": self.hub_clients > 0,
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


def mirror_loop(base: str, token: str):
    """Espejo para visitantes: manda a la nube (Render) el estado de la EVA de la tablet local.
    Autenticado como operador (cookie con el token); los visitantes de la nube lo ven en vivo."""
    last_at, fails = 0.0, 0
    while True:
        time.sleep(0.5)
        with SHARED.lock:
            state, at = SHARED.state, SHARED.at
        if state is None or at == last_at:
            continue
        req = urllib.request.Request(base + "/eva/estado", data=json.dumps(state).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Cookie": f"{OP_COOKIE}={token}"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            last_at = at
            if fails:
                print("espejo: conectado de nuevo con", base, flush=True)
            fails = 0
        except (urllib.error.URLError, OSError) as exc:
            fails += 1
            if fails in (1, 10) or fails % 120 == 0:
                print("espejo: no se pudo publicar en la nube:", exc, flush=True)


mimetypes.add_type("application/manifest+json", ".webmanifest")
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"  # Leaflet y three.js servidos desde la laptop (sin internet en la tablet)
VENDOR = {"/vendor/" + str(p.relative_to(VENDOR_DIR)) for p in VENDOR_DIR.rglob("*") if p.is_file()} if VENDOR_DIR.is_dir() else set()
TILE_DIR = Path(__file__).resolve().parent / "tile_cache"
TILE_RE = _re.compile(r"^/tiles/Mars/EQ/curiosity_hirise_mosaic/1\.0\.0/default/default028mm/(\d{1,2})/(\d{1,6})/(\d{1,6})\.png$")
STATIC = {"/app.html", "/mission.json", "/logo_ares.png", "/oled_logo.png",
          "/manifest.webmanifest", "/sw.js", "/icons/ares-192.png", "/icons/ares-512.png",  # app instalable (PWA)
          "/pov/sol4530.jpg", "/pov/sol4544.jpg", "/pov/sol4582.jpg", "/pov/sol4612.jpg"}  # + panorámicas Navcam de Curiosity  # lo único que se sirve de esta carpeta
TOKEN_FILE = HERE / ".token_operador"  # no se sirven ni se suben a git
WATCH_TOKEN_FILE = HERE / ".token_reloj"
OP_COOKIE = "ares_op"
PUBLICO = False  # --publico: todo el que llega es de internet (servidor en la nube)


def load_token(env: str, path: Path) -> str:
    """Token secreto: variable de entorno (nube) o archivo local que se crea una vez (solo lectura del usuario)."""
    tok = os.environ.get(env, "").strip()
    if tok:
        if len(tok) < 20 or not tok.isalnum():
            raise SystemExit(f"{env} debe tener al menos 20 letras/números")
        return tok
    try:
        tok = path.read_text().strip()
        if len(tok) >= 20 and tok.isalnum():
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


class RateLimit:
    """Límite simple por visitante (IP real que informa Cloudflare): ~20 peticiones/s con ráfagas de 80."""
    def __init__(self, rate=20.0, burst=80.0):
        self.rate, self.burst, self.b, self.lock = rate, burst, {}, threading.Lock()

    def ok(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, t = self.b.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - t) * self.rate)
            if len(self.b) > 5000:
                self.b.clear()
            if tokens < 1:
                self.b[key] = (tokens, now)
                return False
            self.b[key] = (tokens - 1, now)
            return True


def make_handler(serial: SerialPort, bus: BusClient, data_dir: Path, busproc: "BusProcess", planner: Planner,
                 token: str, watch_token: str):
    data_files = {"/elevation.bin", "/slope.bin"}
    # capas compactas para la página (1 byte/celda + gzip): 4 MB -> unos cientos de KB cada una
    u8_layers = {
        "/slope.u8": ("slope.bin", lambda v: v),                         # grados enteros
        "/radiation.u8": ("radiation.bin", lambda v: v * 250),           # factor de exposición 0..1 -> 0..250
        "/thermal.u8": ("thermal_inertia.bin", lambda v: v - 100),       # inercia térmica - 100 (TES)
    }
    u8_cache = {}

    def layer_u8(path):
        if path not in u8_cache:
            src, enc = u8_layers[path]
            a = array("f")
            a.frombytes((data_dir / src).read_bytes())
            raw = bytes(min(255, max(0, round(enc(v)))) if v == v else 0 for v in a)
            u8_cache[path] = {"raw": raw, "gz": gzip.compress(raw, 6)}
        return u8_cache[path]
    limiter = RateLimit()
    owner = {"id": None}  # solo la última pantalla del astronauta que abrió la página le escribe al reloj

    def cell(text):
        r, c = (int(v) for v in text.split(","))
        if not (0 <= r < 1000 and 0 <= c < 1000):
            raise ValueError
        return r, c

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(HERE), **kwargs)

        def _remote(self) -> bool:
            """Viene de internet (túnel de Cloudflare o servidor en la nube) y no de esta misma laptop."""
            return (PUBLICO or self.client_address[0] not in ("127.0.0.1", "::1")
                    or "Cf-Connecting-Ip" in self.headers or "Cf-Ray" in self.headers)

        def _operator(self) -> bool:
            if not self._remote():
                return True  # la laptop y la tablet por USB (adb reverse) operan sin token
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == OP_COOKIE and hmac.compare_digest(v.encode(), token.encode()):
                    return True
            return False

        def _limited(self) -> bool:
            if not self._remote():
                return False  # la laptop y la tablet por USB no tienen límite (la tablet sola hace ~20 peticiones/s)
            key = self.headers.get("Cf-Connecting-Ip") or ""
            if not key and PUBLICO:  # detrás del proxy de la plataforma: la IP real va en X-Forwarded-For
                key = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            key = key or self.client_address[0]
            if limiter.ok(key):
                return False
            self._reply(429, {"error": "demasiadas peticiones"})
            return True

        def _watch_ok(self) -> bool:
            return hmac.compare_digest((self.headers.get("X-Reloj-Token") or "").encode(), watch_token.encode())

        def _stream(self):
            """Puente de la laptop: recibe cada trama del reloj en cuanto la manda la tablet (una por línea)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=ascii")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            hub = serial.hub_cond
            with hub:
                serial.hub_clients += 1
                seen = serial.hub_seq
            try:
                while True:
                    with hub:
                        hub.wait_for(lambda: serial.hub_seq != seen, timeout=2)
                        fresh = serial.hub_seq != seen
                        seen, frame = serial.hub_seq, serial.hub_frame
                    self.wfile.write(frame if fresh else b"$P\n")  # $P = sigo vivo (el reloj lo ignora)
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                with hub:
                    serial.hub_clients -= 1

        def _deny(self):
            return self._reply(403, {"error": "modo visitante: solo lectura"})

        def _public_status(self, st: dict) -> dict:
            if self._operator():
                return st
            return {"open": st["open"], "usb": st["usb"], "bt": bool(st.get("bt")), "wifi": bool(st.get("wifi")),
                    "puente": bool(st.get("puente"))}

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

        def _tile(self, path, m):
            f = TILE_DIR.joinpath(*m.groups()).with_suffix(".png")
            if not f.exists():
                try:
                    req = urllib.request.Request("https://trek.nasa.gov" + path, headers={"User-Agent": "ARES-prototype"})
                    data = urllib.request.urlopen(req, timeout=15).read()
                except (urllib.error.URLError, OSError):
                    return self._reply(404, {"error": "tile not available"})
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(f)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _vendor(self, path):
            body = (VENDOR_DIR / path[len("/vendor/"):]).read_bytes()
            ctype = {".js": "text/javascript", ".css": "text/css", ".png": "image/png"}.get(Path(path).suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self._limited():
                return
            path, _, query = self.path.partition("?")
            m = TILE_RE.match(path)
            if m:  # mosaico HiRISE de Mars Trek: se descarga una vez y queda en caché local
                return self._tile(path, m)
            if path in VENDOR:
                return self._vendor(path)
            if PUBLICO and (path.startswith("/op/") or path in ("/reloj/stream", "/plan")):
                return self._reply(404, {"error": "not found"})  # la nube es solo vista: nadie opera desde internet
            if path.startswith("/op/"):  # link del operador: guarda el token en una cookie y entra
                tok, _, dest = path[4:].partition("/")
                if not hmac.compare_digest(tok.encode(), token.encode()):
                    return self._reply(403, {"error": "token inválido"})
                self.send_response(302)
                secure = "; Secure" if self._remote() else ""
                self.send_header("Set-Cookie", f"{OP_COOKIE}={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict{secure}")
                self.send_header("Location", "/app.html?rol=base" if dest == "base" else "/app.html")
                self.end_headers()
                return
            if path == "/reloj/stream":
                if not self._watch_ok():
                    return self._reply(403, {"error": "token del reloj inválido"})
                return self._stream()
            if path == "/yo":
                return self._reply(200, {"operador": False if PUBLICO else self._operator()})
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
                return self._reply(200, self._public_status(serial.status()))
            if path == "/reloj":  # botones del reloj: ?since=N
                since = query[6:] if query.startswith("since=") else "0"
                return self._reply(200, {"events": serial.events_since(int(since) if since.isdigit() else 0),
                                         **self._public_status(serial.status())})
            if path == "/plan":  # ?goal=r,c[&start=r,c]
                if not self._operator():
                    return self._deny()
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
            if path in u8_layers:
                c = layer_u8(path)
                gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
                body = c["gz"] if gz else c["raw"]
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path in data_files:  # capas del backend, no copias locales
                body = (data_dir / path[1:]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path not in STATIC:  # no se exponen el código ni otros archivos de la carpeta
                return self._reply(404, {"error": "not found"})
            return super().do_GET()

        def _body(self, limit):
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length) if 0 < length <= limit else None

        def do_HEAD(self):  # sin HEAD: evitaría la lista de archivos permitidos
            self._reply(405, {"error": "método no permitido"})

        def do_POST(self):
            if self._limited():
                return
            if PUBLICO and self.path != "/eva/estado":  # nube solo vista: lo único que entra es el espejo de la laptop
                return self._deny()
            if self.path == "/reloj/boton":  # botón del reloj que reenvía el puente de la laptop
                if not self._watch_ok():
                    return self._reply(403, {"error": "token del reloj inválido"})
                line = (self._body(32) or b"").strip()
                if not (line.startswith(b"$B,") and line[3:].isalnum() and len(line) < 24):
                    return self._reply(400, {"error": "botón inválido"})
                serial._button(line[3:].decode(), "puente")
                return self._reply(200, {"ok": True})
            if not self._operator():
                return self._deny()
            if self.path == "/reloj/tomar":
                sid = (self._body(64) or b"").decode(errors="ignore")
                if not (sid and sid.isalnum()):
                    return self._reply(400, {"error": "sesión inválida"})
                owner["id"] = sid
                return self._reply(200, {"ok": True})
            if self.path == "/w":
                sid = self.headers.get("X-Sesion") or ""
                if owner["id"] is None and sid.isalnum():  # servidor reiniciado: la primera pantalla que escribe lo retoma
                    owner["id"] = sid
                if sid != owner["id"]:  # pestañas viejas: no mezclan sus datos
                    return self._reply(409, {"error": "otra pantalla controla el reloj"})
                line = self._body(MAX_LINE)
                # Solo tramas del reloj: "$W,...\n" en ASCII imprimible.
                if not (line and line.startswith(b"$W,") and line.endswith(b"\n")
                        and all(32 <= b < 127 for b in line[:-1])):
                    return self._reply(400, {"error": "bad frame"})
                ok = serial.write(line)
                return self._reply(200 if ok else 503, serial.status())
            if self.path == "/eva/estado":  # la tablet del astronauta publica su estado
                sid = self.headers.get("X-Sesion") or ""
                if owner["id"] is not None and sid != owner["id"]:  # solo la pantalla que controla el reloj
                    return self._reply(409, {"error": "otra pantalla controla la EVA"})
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
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8791")))
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 en la nube")
    ap.add_argument("--publico", action="store_true", help="servidor en internet: todos son visitantes salvo el operador con token")
    ap.add_argument("--espejo", help="URL https de la nube (p. ej. Render) donde los visitantes ven esta EVA en vivo")
    ap.add_argument("--sin-hardware", action="store_true", help="sin USB/Bluetooth/WiFi local: el reloj llega por puente_reloj.py")
    ap.add_argument("--bus-port", type=int, default=8765)
    ap.add_argument("--tty", default="auto", help="puerto del reloj; auto = primer /dev/ttyUSB*")
    args = ap.parse_args()
    global PUBLICO
    PUBLICO = args.publico
    backend = args.backend.resolve()
    mission = json.loads((HERE / "mission.json").read_text())
    first = mission["stops"][0]
    busproc = BusProcess(backend, args.bus_port, (first["row"], first["col"]))
    bus = BusClient(args.bus_port)
    if args.tty == "auto":  # al reconectar el cable el ESP32 puede pasar de ttyUSB0 a ttyUSB1
        ports = sorted(Path("/dev").glob("ttyUSB*"))
        args.tty = str(ports[0]) if ports else "/dev/ttyUSB0"
    serial = SerialPort(args.tty, hardware=not args.sin_hardware)
    token = load_token("OPERADOR_TOKEN", TOKEN_FILE)
    watch_token = load_token("RELOJ_TOKEN", WATCH_TOKEN_FILE)
    server = http.server.ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(serial, bus, backend / "data" / "processed", busproc, Planner(backend), token, watch_token))
    server.daemon_threads = True
    if args.espejo:
        if not args.espejo.startswith("https://"):
            raise SystemExit("--espejo debe ser https:// (el token viaja en la petición)")
        threading.Thread(target=mirror_loop, args=(args.espejo.rstrip("/"), token), daemon=True).start()
        print("Espejo para visitantes →", args.espejo, flush=True)
    print(f"Prototipo: http://{args.host}:{args.port}/app.html{' (público)' if PUBLICO else ''} · bus ws://127.0.0.1:{args.bus_port} "
          f"· reloj {args.tty}\nOperador por internet: <url pública>/op/<token>  (token en {TOKEN_FILE.name})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        busproc.stop()


if __name__ == "__main__":
    main()
