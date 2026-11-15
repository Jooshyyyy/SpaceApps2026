"""Gateway WebSocket del reloj para el despliegue público (`/reloj/ws`).

Contrato congelado en `docs/watch-websocket.md`: el dispositivo abre el
WebSocket en el mismo puerto HTTP, se autentica con `Authorization: Bearer
<WATCH_TOKEN>` durante el upgrade y luego intercambia texto:

    servidor -> reloj   $W,<rumbo>,... \n   (la trama de /w, sin modificar)
    reloj -> servidor   $B,<botón> \n

El gateway usa solo la biblioteca estándar y se apoya en las invariantes
existentes de `SerialPort`: `_button` (seq, orden, últimos 20), `events_since`,
`status` y `write`. No hay UDP ni apertura de puertos serie en modo cloud.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import select
import socket
import struct
import threading
import time

ENDPOINT = "/reloj/ws"
MAX_MESSAGE_BYTES = 200       # mismo límite que POST /w
PING_INTERVAL = 20.0
PONG_TIMEOUT = 15.0
SEND_TIMEOUT = 2.0
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ProtocolError(Exception):
    """Entrada inválida del dispositivo: cierra con `code` y un motivo público."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode()


def _valid_key(key: str) -> bool:
    try:
        raw = base64.b64decode(key.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return False
    return len(raw) == 16


def validate_request(headers, raw_path: str, token: str):
    """Devuelve (status, mensaje) para rechazar, o None si el upgrade es válido."""
    if raw_path != ENDPOINT:  # cualquier query string queda fuera: no hay tokens en la URL
        return 400, "endpoint inválido"
    if headers.get("Origin") is not None:  # el endpoint es del dispositivo, no del navegador
        return 403, "origen de navegador no permitido"
    authorization = headers.get("Authorization", "")
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return 401, "se requiere Authorization: Bearer"
    try:
        matches = hmac.compare_digest(
            presented.encode("latin-1", "ignore"), token.encode("utf-8"))
    except (UnicodeError, TypeError):
        matches = False
    if not matches:
        return 401, "token inválido"  # nunca se repite el valor recibido
    if "websocket" not in headers.get("Upgrade", "").lower():
        return 400, "upgrade inválido"
    connection = [part.strip().lower() for part in headers.get("Connection", "").split(",")]
    if "upgrade" not in connection:
        return 400, "upgrade inválido"
    if headers.get("Sec-WebSocket-Version") != "13":
        return 400, "versión WebSocket no soportada"
    if not _valid_key(headers.get("Sec-WebSocket-Key", "")):
        return 400, "clave WebSocket inválida"
    return None


def parse_frame(buf: bytes):
    """Decodifica un frame de cliente. None si faltan bytes; ProtocolError si es inválido."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    opcode, fin = b0 & 0x0F, bool(b0 & 0x80)
    if b0 & 0x70:
        raise ProtocolError(1002, "bits reservados")
    if opcode not in (0x0, 0x1, 0x2, 0x8, 0x9, 0xA):
        raise ProtocolError(1002, "opcode no soportado")
    if not b1 & 0x80:  # todo frame de cliente debe venir enmascarado
        raise ProtocolError(1002, "frames sin máscara")
    length = b1 & 0x7F
    index = 2
    if length == 126:
        if len(buf) < 4:
            return None
        length = struct.unpack_from(">H", buf, 2)[0]
        index = 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length = struct.unpack_from(">Q", buf, 2)[0]
        index = 10
    if opcode >= 0x8 and (not fin or length > 125):
        raise ProtocolError(1002, "frame de control inválido")
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(1009, "mensaje demasiado grande")
    if len(buf) < index + 4 + length:
        return None
    mask = buf[index:index + 4]
    payload = bytes(byte ^ mask[i & 3] for i, byte in enumerate(buf[index + 4:index + 4 + length]))
    return (fin, opcode, payload), buf[index + 4 + length:]


def _frame(opcode: int, payload: bytes) -> bytes:
    length = len(payload)
    if length < 126:
        head = bytes([0x80 | opcode, length])
    elif length < 65536:
        head = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
    else:
        head = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)
    return head + payload


class DeviceLink:
    """Un único reloj conectado. Los envíos son acotados y serializados por lock."""

    def __init__(self, sock: socket.socket, on_button, ping_interval: float = PING_INTERVAL,
                 pong_timeout: float = PONG_TIMEOUT):
        self.sock = sock
        self.on_button = on_button
        self.ping_interval = max(0.05, float(ping_interval))
        self.pong_timeout = max(0.05, float(pong_timeout))
        self._send_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = threading.Event()
        self._close_sent = False
        self._fragment = None
        self._next_ping = time.monotonic() + self.ping_interval
        self._awaiting_pong = None
        self.last_pong = time.monotonic()
        self._tick = max(0.02, min(1.0, self.ping_interval / 2))

    # ------------------------------------------------------------ salida
    def _send(self, opcode: int, payload: bytes) -> bool:
        if self._closed.is_set() and opcode != 0x8:
            return False
        data = _frame(opcode, payload)
        with self._send_lock:
            try:
                _, writable, _ = select.select([], [self.sock], [], SEND_TIMEOUT)
                if not writable:
                    return False
                self.sock.sendall(data)
                return True
            except (OSError, ValueError):
                return False

    def send_text(self, data: bytes) -> bool:
        """Envía una trama ya validada para /w; nunca la reescribe."""
        if self._closed.is_set() or not data or len(data) > MAX_MESSAGE_BYTES:
            return False
        return self._send(0x1, data)

    def _send_close(self, code: int, reason: str):
        with self._close_lock:
            if self._close_sent:
                return
            self._close_sent = True
        payload = struct.pack(">H", code) + reason.encode("utf-8")[:123]
        self._send(0x8, payload)
        self._closed.set()
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self):
        self._closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ entrada
    def _message(self, payload: bytes):
        if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
            raise ProtocolError(1008, "se esperaba $B,<botón>\\n")
        line = payload[:-1]
        if not line.startswith(b"$B,") or not line[3:].isalnum() or len(line) >= 24:
            raise ProtocolError(1008, "trama de botón inválida")
        self.on_button(line[3:].decode("ascii"))

    def _handle(self, fin: bool, opcode: int, payload: bytes):
        if opcode == 0x8:  # cierre del dispositivo: eco y fin
            self._send_close(1000, "bye")
            return
        if opcode == 0x9:
            self._send(0xA, payload)
            return
        if opcode == 0xA:
            self._awaiting_pong = None
            self.last_pong = time.monotonic()
            return
        if opcode == 0x2:
            raise ProtocolError(1003, "solo se aceptan mensajes de texto")
        if opcode == 0x1:
            if self._fragment is not None:
                raise ProtocolError(1002, "fragmentación inválida")
            if fin:
                self._message(payload)
            else:
                self._fragment = bytearray(payload)
            return
        if opcode == 0x0:
            if self._fragment is None:
                raise ProtocolError(1002, "continuación sin mensaje")
            self._fragment += payload
            if len(self._fragment) > MAX_MESSAGE_BYTES:
                raise ProtocolError(1009, "mensaje demasiado grande")
            if fin:
                message, self._fragment = bytes(self._fragment), None
                self._message(message)
            return
        raise ProtocolError(1002, "opcode no soportado")

    def _consume(self, buf: bytes) -> bytes:
        while True:
            parsed = parse_frame(buf)
            if parsed is None:
                return buf
            (fin, opcode, payload), buf = parsed
            self._handle(fin, opcode, payload)
            if self._closed.is_set():
                return b""

    def _heartbeat(self) -> bool:
        now = time.monotonic()
        if self._awaiting_pong is not None:
            if now - self._awaiting_pong > self.pong_timeout:
                self._send_close(1001, "sin pong")
                return False
        elif now >= self._next_ping:
            self._next_ping = now + self.ping_interval
            self._awaiting_pong = now
            if not self._send(0x9, b"hb"):
                self._send_close(1001, "ping no enviado")
                return False
        return True

    def run(self, buffered: bytes = b""):
        buf = buffered or b""
        try:
            if buf:  # bytes que el parser HTTP ya había leído tras el upgrade
                buf = self._consume(buf)
            while not self._closed.is_set():
                if not self._heartbeat():
                    break
                try:
                    ready, _, _ = select.select([self.sock], [], [], self._tick)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    chunk = self.sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                buf = self._consume(buf)
                if len(buf) > 4096:
                    raise ProtocolError(1009, "buffer demasiado grande")
        except ProtocolError as exc:
            self._send_close(exc.code, exc.reason)
        finally:
            self.close()


class WatchGateway:
    """Valida el upgrade, garantiza un solo reloj y lo conecta a SerialPort."""

    def __init__(self, serial, token: str, ping_interval: float = PING_INTERVAL,
                 pong_timeout: float = PONG_TIMEOUT):
        if not token:
            raise ValueError("WATCH_TOKEN no puede estar vacío")
        self.serial = serial
        self.token = token
        self.ping_interval = ping_interval
        self.pong_timeout = pong_timeout

    def handle(self, handler, raw_path: str):
        """Atiende GET /reloj/ws. Rechaza con (status, body) o toma el socket y retorna None."""
        rejection = validate_request(handler.headers, raw_path, self.token)
        if rejection is not None:
            return rejection[0], {"error": rejection[1]}
        link = DeviceLink(handler.connection, self.serial.device_button,
                          self.ping_interval, self.pong_timeout)
        if not self.serial.claim_device(link):
            return 409, {"error": "ya hay un reloj conectado"}
        try:
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key(handler.headers['Sec-WebSocket-Key'])}\r\n"
                "\r\n"
            )
            handler.connection.sendall(response.encode("ascii"))
            handler.close_connection = True
            link.run(self._buffered(handler))
        except OSError:
            pass
        finally:
            self.serial.release_device(link)
            link.close()
        return None

    @staticmethod
    def _buffered(handler) -> bytes:
        """Bytes que el parser HTTP ya haya leído detrás de las cabeceras."""
        sock = handler.connection
        try:
            sock.setblocking(False)
            return handler.rfile.read1(65536) or b""
        except (BlockingIOError, OSError, ValueError):
            return b""
        finally:
            try:
                sock.setblocking(True)
            except (OSError, ValueError):
                pass
