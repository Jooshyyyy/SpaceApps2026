#!/usr/bin/env python3
"""Tests for the public watch WebSocket gateway (`/reloj/ws`).

The suite runs with the standard library only. It starts the real HTTP handler
from `servidor.py` on a loopback port and speaks WebSocket by hand so the
device contract is exercised exactly as the firmware sees it.

Run with:  python3 -m unittest discover -s prototipo/tests -v
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROTO = ROOT / "prototipo"
sys.path.insert(0, str(PROTO))

import servidor  # noqa: E402
import watch_gateway  # noqa: E402

TOKEN = "watch-test-token-0123456789"
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
W_FRAME = b"$W,090,180,42,95,88,120,3,0,OK\n"


# ---------------------------------------------------------------- helpers
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    status = response.status
    conn.close()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def server_frame(buf: bytes):
    """Decode one unmasked server->client frame, or None when incomplete."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    opcode = b0 & 0x0F
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
    if len(buf) < index + length:
        return None
    return (bool(b0 & 0x80), opcode, buf[index:index + length]), buf[index + length:]


class WSClient:
    def __init__(self, sock: socket.socket, buffered: bytes = b""):
        self.sock = sock
        self.buf = buffered

    def send_frame(self, opcode: int, payload: bytes, fin: bool = True, mask: bool = True):
        b0 = (0x80 if fin else 0) | opcode
        mask_bit = 0x80 if mask else 0
        length = len(payload)
        if length < 126:
            head = bytes([b0, mask_bit | length])
        elif length < 65536:
            head = bytes([b0, mask_bit | 126]) + struct.pack(">H", length)
        else:
            head = bytes([b0, mask_bit | 127]) + struct.pack(">Q", length)
        if mask:
            key = os.urandom(4)
            body = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
            head += key
        else:
            body = payload
        self.sock.sendall(head + body)

    def send_text(self, text: str):
        self.send_frame(0x1, text.encode("ascii"))

    def recv_frame(self, timeout: float = 5.0):
        self.sock.settimeout(timeout)
        while True:
            parsed = server_frame(self.buf)
            if parsed is not None:
                (fin, opcode, payload), self.buf = parsed
                return fin, opcode, payload
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("el servidor cerró el WebSocket")
            self.buf += chunk

    def recv_text(self, timeout: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            left = max(0.01, deadline - time.monotonic())
            _, opcode, payload = self.recv_frame(left)
            if opcode == 0x9:
                self.send_frame(0xA, payload)
            elif opcode == 0x8:
                raise EOFError("el servidor cerró con código %d" % struct.unpack(">H", payload[:2])[0])
            elif opcode == 0x1:
                return payload

    def close(self, code: int = 1000):
        try:
            self.send_frame(0x8, struct.pack(">H", code))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def ws_handshake(port: int, token: str | None = TOKEN, origin: str | None = None,
                 path: str = "/reloj/ws", key: str | None = None):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = key or base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    if origin is not None:
        lines.append(f"Origin: {origin}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    head, _, rest = head.partition(b"\r\n\r\n")
    first, *raw_headers = head.decode("latin-1").split("\r\n")
    status = int(first.split()[1])
    headers = {}
    for line in raw_headers:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    if status != 101:
        length = int(headers.get("content-length", "0"))
        body = rest
        while len(body) < length:
            body += sock.recv(length - len(body))
        sock.close()
        return status, headers, body, None
    expected = base64.b64encode(hashlib.sha1((key + GUID.decode()).encode()).digest()).decode()
    if headers.get("sec-websocket-accept") != expected:
        sock.close()
        raise AssertionError("Sec-WebSocket-Accept inválido: %r" % headers.get("sec-websocket-accept"))
    return status, headers, b"", WSClient(sock, rest)


class WatchHarness:
    """Real `servidor.make_handler` on loopback, with the cloud watch gateway."""

    def __init__(self, gateway=None, serial=None):
        if gateway is None:
            serial = serial or servidor.SerialPort("wss", cloud=True)
            gateway = watch_gateway.WatchGateway(serial, TOKEN, ping_interval=30.0, pong_timeout=30.0)
        self.gateway = gateway
        self.serial = gateway.serial
        self._tmp = tempfile.TemporaryDirectory(prefix="watch-ws-test-")
        handler = servidor.make_handler(
            self.serial, None, Path(self._tmp.name), None, None, watch=gateway)
        quiet = type("QuietHandler", (handler,), {"log_message": lambda self, *args: None})
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), quiet)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
        self._tmp.cleanup()

    def connect(self, **kwargs):
        status, headers, body, client = ws_handshake(self.port, **kwargs)
        if status != 101:
            raise AssertionError("handshake %d: %r" % (status, body))
        return client

    def status(self):
        code, body = http_json(self.port, "GET", "/estado")
        assert code == 200, (code, body)
        return body


class WatchWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.harness = WatchHarness()
        self.addCleanup(self.harness.close)

    def wait_disconnected(self):
        wait_for(lambda: self.harness.status()["ws"] is False)
        self.assertFalse(self.harness.status()["ws"])

    def take_watch(self, sid: str = "sid1"):
        code, body = http_json(self.harness.port, "POST", "/reloj/tomar", sid.encode())
        self.assertEqual((code, body), (200, {"ok": True}))
        return sid

    # -- S1/S3: authenticated bridge, raw frames, button events -------------
    def test_authenticated_watch_bridges_w_frames_and_button_events(self):
        client = self.harness.connect()
        self.take_watch()

        code, body = http_json(self.harness.port, "POST", "/w", W_FRAME, {"X-Sesion": "sid1"})
        self.assertEqual(code, 200)
        self.assertEqual(body["sent"], 1)
        self.assertEqual(body["tty"], "wss")
        self.assertTrue(body["open"])
        self.assertTrue(body["ws"])
        self.assertTrue(body["cloud"])
        self.assertFalse(body["usb"])
        self.assertIsNone(body["wifi"])
        self.assertEqual(client.recv_text(), W_FRAME)  # raw $W payload unchanged

        client.send_text("$B,SOS\n")
        events = wait_for(lambda: http_json(self.harness.port, "GET", "/reloj?since=0")[1]["events"])
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]), {"seq", "t", "button", "via"})
        self.assertEqual((events[0]["seq"], events[0]["button"], events[0]["via"]), (1, "SOS", "ws"))

        client.send_text("$B,CANCEL\n")
        events = wait_for(lambda: http_json(self.harness.port, "GET", "/reloj?since=1")[1]["events"])
        self.assertEqual([(e["seq"], e["button"]) for e in events], [(2, "CANCEL")])
        self.assertEqual(http_json(self.harness.port, "GET", "/reloj?since=2")[1]["events"], [])

        client.close()
        self.wait_disconnected()
        code, body = http_json(self.harness.port, "GET", "/reloj?since=0")
        self.assertEqual(code, 200)
        self.assertEqual([(e["seq"], e["button"]) for e in body["events"]], [(1, "SOS"), (2, "CANCEL")])
        self.assertFalse(body["open"])  # events survive disconnect, link status is honest
        self.assertEqual(body["sent"], 1)

    def test_device_auth_origin_and_url_token_are_rejected(self):
        cases = [
            (None, None, "/reloj/ws", 401),
            ("wrong-token", None, "/reloj/ws", 401),
            (TOKEN, "https://browser.example", "/reloj/ws", 403),
            (TOKEN, None, "/reloj/ws?token=" + TOKEN, 400),
        ]
        for token, origin, path, expected in cases:
            status, _, body, client = ws_handshake(
                self.harness.port, token=token, origin=origin, path=path)
            self.assertIsNone(client)
            self.assertEqual(status, expected, (token, origin, path, body))
            self.assertIn("error", json.loads(body))
            self.assertNotIn(TOKEN, body.decode("latin-1"))
            self.assertFalse(self.harness.status()["ws"])
            self.assertEqual(http_json(self.harness.port, "GET", "/reloj?since=0")[1]["events"], [])

        client = self.harness.connect()
        self.assertTrue(self.harness.status()["ws"])
        client.close()
        self.wait_disconnected()

    def test_second_authenticated_watch_is_rejected_not_stolen(self):
        first = self.harness.connect()
        status, _, body, second = ws_handshake(self.harness.port, token=TOKEN)
        self.assertIsNone(second)
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(body))

        self.take_watch()
        code, _ = http_json(self.harness.port, "POST", "/w", W_FRAME, {"X-Sesion": "sid1"})
        self.assertEqual(code, 200)
        self.assertEqual(first.recv_text(), W_FRAME)  # ownership stayed with the first watch

        first.close()
        self.wait_disconnected()
        replacement = self.harness.connect()
        replacement.close()
        self.wait_disconnected()

    def test_malformed_oversized_unmasked_and_binary_inputs_are_rejected(self):
        def invalid(sender, expected_code):
            client = self.harness.connect()
            sender(client)
            fin, opcode, payload = client.recv_frame()
            self.assertTrue(fin)
            self.assertEqual(opcode, 0x8, "se esperaba un frame de cierre")
            self.assertEqual(struct.unpack(">H", payload[:2])[0], expected_code)
            client.sock.close()
            self.wait_disconnected()

        invalid(lambda c: c.send_text("$B,SO$\n"), 1008)
        invalid(lambda c: c.send_text("$B,SOS"), 1008)  # newline is part of the contract
        invalid(lambda c: c.send_text("$B," + "S" * 300 + "\n"), 1009)
        invalid(lambda c: c.send_frame(0x1, b"$B,SOS\n", mask=False), 1002)
        invalid(lambda c: c.send_frame(0x2, b"$B,SOS\n"), 1003)

        code, body = http_json(self.harness.port, "GET", "/reloj?since=0")
        self.assertEqual(code, 200)
        self.assertEqual(body["events"], [])
        self.assertEqual(body["sent"], 0)

    def test_heartbeat_closes_a_silent_device(self):
        serial = servidor.SerialPort("wss", cloud=True)
        gateway = watch_gateway.WatchGateway(serial, TOKEN, ping_interval=0.15, pong_timeout=0.25)
        harness = WatchHarness(gateway=gateway)
        self.addCleanup(harness.close)
        client = harness.connect()
        self.addCleanup(lambda: client.sock.close())
        saw_ping = False
        close_code = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                _, opcode, payload = client.recv_frame(timeout=1.0)
            except EOFError:
                break
            if opcode == 0x9:
                saw_ping = True
            elif opcode == 0x8:
                close_code = struct.unpack(">H", payload[:2])[0]
                break
        self.assertTrue(saw_ping)
        self.assertEqual(close_code, 1001)
        wait_for(lambda: harness.status()["ws"] is False)
        self.assertFalse(harness.status()["ws"])

    def test_heartbeat_pong_keeps_the_link_usable(self):
        serial = servidor.SerialPort("wss", cloud=True)
        gateway = watch_gateway.WatchGateway(serial, TOKEN, ping_interval=0.15, pong_timeout=0.30)
        harness = WatchHarness(gateway=gateway)
        self.addCleanup(harness.close)
        client = harness.connect()
        self.addCleanup(lambda: client.sock.close())
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                _, opcode, payload = client.recv_frame(timeout=max(0.05, deadline - time.monotonic()))
            except TimeoutError:
                break
            if opcode == 0x9:
                client.send_frame(0xA, payload)
            elif opcode == 0x8:
                self.fail("el enlace se cerró pese a responder los pong")
        self.assertTrue(harness.status()["ws"])
        code, body = http_json(harness.port, "POST", "/reloj/tomar", b"sid1")
        self.assertEqual(code, 200)
        code, _ = http_json(harness.port, "POST", "/w", W_FRAME, {"X-Sesion": "sid1"})
        self.assertEqual(code, 200)
        self.assertEqual(client.recv_text(), W_FRAME)
        client.close()

    # -- frozen public status contract --------------------------------------
    def test_cloud_serial_keeps_status_shape_and_avoids_local_transports(self):
        serial = servidor.SerialPort("wss", cloud=True)
        status = serial.status()
        for key in ("tty", "open", "usb", "wifi", "sent", "error", "ws", "cloud"):
            self.assertIn(key, status)
        self.assertEqual(status["tty"], "wss")
        self.assertFalse(status["open"])
        self.assertFalse(status["usb"])
        self.assertIsNone(status["wifi"])
        self.assertFalse(status["ws"])
        self.assertTrue(status["cloud"])
        self.assertEqual((status["sent"], status["error"]), (0, ""))
        self.assertIsNone(serial.udp)  # cloud never binds UDP 4210
        self.assertIsNone(serial.fd)   # cloud never loops opening serial ports
        self.assertFalse(serial.write(W_FRAME))
        after = serial.status()
        self.assertEqual(after["sent"], 0)
        self.assertTrue(after["error"])

    # -- PRESERVE: existing /w, /estado and /reloj polling behavior ---------
    def test_existing_owner_frame_validation_and_polling_are_preserved(self):
        # Sin dueño previo el prototipo no rechaza por sesión: intenta escribir y esto
        # es lo que hacía antes de este cambio (503 si no hay reloj disponible).
        code, body = http_json(self.harness.port, "POST", "/w", W_FRAME)
        self.assertEqual(code, 503)
        code, body = http_json(self.harness.port, "POST", "/reloj/tomar", b"bad sid")
        self.assertEqual((code, body), (400, {"error": "sesión inválida"}))
        self.take_watch()
        code, body = http_json(self.harness.port, "POST", "/w", W_FRAME, {"X-Sesion": "otra"})
        self.assertEqual((code, body), (409, {"error": "otra pantalla controla el reloj"}))
        code, body = http_json(self.harness.port, "POST", "/w", b"$X,bad\n", {"X-Sesion": "sid1"})
        self.assertEqual((code, body), (400, {"error": "bad frame"}))
        oversized = b"$W," + b"A" * 197 + b"\n"  # 201 bytes: el límite sigue en 200
        code, body = http_json(self.harness.port, "POST", "/w", oversized, {"X-Sesion": "sid1"})
        self.assertEqual((code, body), (400, {"error": "bad frame"}))
        code, body = http_json(self.harness.port, "POST", "/w", W_FRAME, {"X-Sesion": "sid1"})
        self.assertEqual(code, 503)
        self.assertFalse(body["open"])
        self.assertEqual(body["sent"], 0)
        code, body = http_json(self.harness.port, "GET", "/reloj?since=nope")
        self.assertEqual(code, 200)
        self.assertEqual(body["events"], [])
        for key in ("tty", "open", "usb", "wifi", "sent", "error", "ws", "cloud"):
            self.assertIn(key, body)

        client = self.harness.connect()
        code, body = http_json(self.harness.port, "POST", "/w", W_FRAME, {"X-Sesion": "sid1"})
        self.assertEqual(code, 200)
        self.assertEqual(body["sent"], 1)
        self.assertEqual(client.recv_text(), W_FRAME)
        client.close()

    # -- cloud entrypoint ---------------------------------------------------
    def test_cloud_entrypoint_fails_closed_without_watch_token(self):
        env = os.environ.copy()
        env.pop("WATCH_TOKEN", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="watch-backend-") as backend:
            result = subprocess.run(
                [sys.executable, str(PROTO / "servidor.py"), "--cloud", "--port", str(free_port()),
                 "--backend", backend],
                capture_output=True, text=True, timeout=15, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WATCH_TOKEN", result.stdout + result.stderr)

    def test_cloud_entrypoint_serves_public_web_without_heavy_startup(self):
        env = os.environ.copy()
        env["WATCH_TOKEN"] = TOKEN
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        port = free_port()
        with tempfile.TemporaryDirectory(prefix="watch-backend-") as backend:
            proc = subprocess.Popen(
                [sys.executable, str(PROTO / "servidor.py"), "--cloud", "--host", "127.0.0.1",
                 "--port", str(port), "--backend", backend],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            self.addCleanup(lambda: proc.poll() is None and proc.kill())

            def ready():
                try:
                    code, body = http_json(port, "GET", "/estado")
                    return body if code == 200 else None
                except OSError:
                    return None

            status = wait_for(ready, timeout=10)
            self.assertIsNotNone(status, "el servidor cloud no respondió")
            self.assertTrue(status["cloud"])
            self.assertFalse(status["ws"])
            self.assertTrue(status["tty"])
            code, body = http_json(port, "GET", "/bus")
            self.assertEqual(code, 503)
            self.assertIn("error", body)
            code, _ = http_json(port, "GET", "/app.html")
            self.assertEqual(code, 200)  # la web pública se sirve sin backend pesado
            rejected, _, rejected_body, client = ws_handshake(port, token="wrong-secret-value")
            self.assertEqual(rejected, 401)
            self.assertIsNone(client)
            self.assertNotIn(b"wrong-secret-value", rejected_body)
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=5)
        self.assertNotIn(TOKEN, stdout + stderr)
        self.assertNotIn("wrong-secret-value", stdout + stderr)
        self.assertNotIn("eva_bus", stdout)


if __name__ == "__main__":
    unittest.main()
