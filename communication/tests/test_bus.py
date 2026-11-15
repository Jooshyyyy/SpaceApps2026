"""Public WebSocket/engine integration tests. Python standard library only."""
import base64
import binascii
import hashlib
import json
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
BUS = ROOT / 'communication/eva_bus'
ENGINE = ROOT / 'core_engine/eva_engine'


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=3)
        self.buffer = bytearray()
        key = base64.b64encode(b'0123456789abcdef').decode()
        self.sock.sendall((f'GET / HTTP/1.1\r\nHost: localhost:{port}\r\n'
                           'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            self.buffer.extend(self.sock.recv(4096))
        headers, tail = bytes(self.buffer).split(b'\r\n\r\n', 1)
        assert headers.startswith(b'HTTP/1.1 101'), headers
        expected = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        assert expected in headers
        self.buffer = bytearray(tail)

    def close(self):
        self.sock.close()

    def exact(self, size):
        while len(self.buffer) < size:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError('WebSocket closed')
            self.buffer.extend(data)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def receive(self):
        first, second = self.exact(2)
        assert not second & 0x80
        size = second & 127
        if size == 126:
            size = struct.unpack('!H', self.exact(2))[0]
        elif size == 127:
            size = struct.unpack('!Q', self.exact(8))[0]
        return first & 15, self.exact(size)

    def send(self, payload, opcode=1, masked=True):
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        if isinstance(payload, str):
            payload = payload.encode()
        header = bytes([0x80 | opcode])
        flag = 0x80 if masked else 0
        if len(payload) < 126:
            header += bytes([flag | len(payload)])
        elif len(payload) <= 65535:
            header += bytes([flag | 126]) + struct.pack('!H', len(payload))
        else:
            header += bytes([flag | 127]) + struct.pack('!Q', len(payload))
        if masked:
            mask = b'\x12\x34\x56\x78'
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            header += mask
        self.sock.sendall(header + payload)

    def until(self, kind, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            opcode, payload = self.receive()
            if opcode == 1:
                doc = json.loads(payload)
                if doc['type'] == kind:
                    return doc
        raise AssertionError('missing message ' + kind)

    def telemetry(self, predicate=lambda frame: True):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            opcode, payload = self.receive()
            if opcode != 2:
                continue
            assert len(payload) == 24
            assert payload[:2] == b'M\x01'
            assert binascii.crc_hqx(payload[:22], 0xFFFF) == struct.unpack('<H', payload[22:])[0]
            frame = struct.unpack('<BBHIHHhBBHHBBH', payload)
            if predicate(frame):
                return frame
        raise AssertionError('expected telemetry state did not arrive within 3 seconds')


class BusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eva-bus-')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        meta = {'format': 'mars-eva-aoi/1', 'name': 'bus fixture',
                'source': {'nodata': -3.402823e38},
                'aoi': {'rows': 5, 'cols': 5,
                        'bbox_lonlat': {'west': 1, 'east': 2, 'north': 4, 'south': 3},
                        'cell_size_m': {'dx': 1, 'dy': 1}},
                'policy': {'max_slope_deg': 20}}
        (self.data / 'aoi.json').write_text(json.dumps(meta))
        (self.data / 'elevation.bin').write_bytes(struct.pack('<25f', *([-3600.] * 25)))
        (self.data / 'slope.bin').write_bytes(struct.pack('<25f', *([0.] * 25)))
        (self.data / 'cost.bin').write_bytes(bytes([1] * 25))
        self.wrapper = self.data / 'engine_wrapper.py'
        self.wrapper.write_text(
            '#!' + sys.executable + '\n'
            'import os, sys, time\n'
            'from pathlib import Path\n'
            'root = Path(__file__).parent\n'
            'if sys.argv[1] == "sim" and (root / "slow").exists(): time.sleep(0.6)\n'
            'if sys.argv[1] == "sim" and (root / "hang").exists(): time.sleep(10)\n'
            'os.execv(' + repr(str(ENGINE)) + ', [' + repr(str(ENGINE)) + '] + sys.argv[1:])\n')
        self.wrapper.chmod(0o700)
        self.proc = subprocess.Popen(
            [str(BUS), '--dir', str(self.data), '--engine', str(self.wrapper),
             '--port', '0', '--start', '0,0', '--goal', '4,4'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        ready, _, _ = select.select([self.proc.stdout], [], [], 5)
        self.assertTrue(ready, 'bus did not become ready')
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError('bus exited before ready: ' + self.proc.stderr.read())
        event = json.loads(line)
        self.assertEqual(event['type'], 'listening')
        self.port = event['port']

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def client(self):
        client = Client(self.port)
        self.addCleanup(client.close)
        return client

    def test_hello_real_engine_and_cadence(self):
        client = self.client()
        hello = client.until('hello')
        self.assertEqual(hello['protocol'], 'mars-eva-edge/1')
        self.assertEqual(hello['frame_bytes'], 24)
        self.assertEqual(hello['telemetry_hz'], 10)
        self.assertEqual(hello['altitude_origin_m'], -3600)
        self.assertEqual(hello['engine']['aoi']['rows'], 5)
        self.assertFalse(hello['engine']['radiation_available'])
        update = client.until('route_update')
        self.assertTrue(update['result']['found'])
        frames, received = [], []
        for _ in range(6):
            frames.append(client.telemetry())
            received.append(time.monotonic())
        for previous, frame in zip(frames, frames[1:]):
            self.assertEqual((frame[2] - previous[2]) & 65535, 1)
            self.assertGreaterEqual((frame[3] - previous[3]) & 0xFFFFFFFF, 75)
            self.assertLessEqual((frame[3] - previous[3]) & 0xFFFFFFFF, 250)
        self.assertGreater(received[-1] - received[0], .3)
        self.assertLess(received[-1] - received[0], 1.5)
        self.assertEqual(frames[-1][6], 0)  # relative altitude

    def test_emergency_recalculates_and_broadcasts_to_both_clients(self):
        a, b = self.client(), self.client()
        a.until('route_update')
        b.until('route_update')
        a.send({'type': 'position', 'row': 2, 'col': 2})
        self.assertEqual(a.until('event_ack')['event'], 'position')
        self.assertEqual(b.until('route_update')['cause'], 'position')
        a.until('route_update')
        a.send({'type': 'storm', 'active': True})
        self.assertEqual(a.until('event_ack')['event'], 'storm')
        update = a.until('route_update')
        self.assertEqual(update['cause'], 'storm')
        self.assertEqual(update['result']['goal'], {'row': 0, 'col': 0})
        self.assertEqual(b.until('route_update')['cause'], 'storm')
        frame = a.telemetry(lambda frame: frame[11] & 1 and not frame[11] & 32)
        self.assertEqual(frame[4:6], (2, 2))
        a.send({'type': 'radiation_peak', 'active': True})
        self.assertEqual(a.until('route_update')['cause'], 'radiation_peak')
        a.send({'type': 'storm', 'active': False})
        self.assertEqual(a.until('route_update')['result']['goal'], {'row': 0, 'col': 0})
        a.send({'type': 'radiation_peak', 'active': False})
        self.assertEqual(a.until('route_update')['result']['goal'], {'row': 4, 'col': 4})

    def test_invalid_controls_do_not_change_mission_or_counters(self):
        c = self.client()
        initial = c.until('route_update')
        revision = initial['revision']
        files = {p.name: p.read_bytes() for p in self.data.iterdir()}
        for control in [
            {'type': 'position', 'row': -1, 'col': 0},
            {'type': 'position', 'row': 1.5, 'col': 0},
            {'type': 'position', 'row': 1e99, 'col': 0},
            {'type': 'set_goal', 'row': 10, 'col': 0},
            {'type': 'storm', 'active': 'true'},
            {'type': 'unknown'},
            '{',
        ]:
            c.send(control)
            error = c.until('error')
            self.assertEqual(error['revision'], revision)
            self.assertEqual(error['position'], {'row': 0, 'col': 0})
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.data.iterdir()})
        c.send({'type': 'set_goal', 'row': 1, 'col': 1})
        result = c.until('route_update')
        self.assertEqual(result['revision'], revision + 1)
        self.assertEqual(result['result']['goal'], {'row': 1, 'col': 1})

    def test_engine_failure_is_surfaced_and_stays_stale(self):
        c = self.client()
        c.until('route_update')
        (self.data / 'cost.bin').write_bytes(b'')
        c.send({'type': 'position', 'row': 1, 'col': 1})
        error = c.until('error')
        self.assertIn('engine', error['error'])
        self.assertEqual(error['revision'], 1)
        c.telemetry(lambda frame: frame[11] & 32)
        self.assertIsNone(self.proc.poll())

    def test_ping_close_and_reject_unmasked_frames(self):
        c = self.client()
        c.until('route_update')
        c.send(b'hello', opcode=9)
        while True:
            opcode, payload = c.receive()
            if opcode == 10:
                self.assertEqual(payload, b'hello')
                break
        c.send(b'', opcode=8)
        while True:
            opcode, _ = c.receive()
            if opcode == 8:
                break
        other = self.client()
        other.until('route_update')
        other.send(b'{}', masked=False)
        with self.assertRaises((EOFError, ConnectionResetError)):
            while True:
                other.receive()
        self.assertIsNone(self.proc.poll())

    def test_telemetry_continues_during_recalculation(self):
        c = self.client()
        c.until('route_update')
        (self.data / 'slow').touch()
        c.send({'type': 'position', 'row': 2, 'col': 2})
        c.until('event_ack')
        frames = [c.telemetry() for _ in range(4)]
        self.assertTrue(all(frame[11] & 32 for frame in frames))
        for previous, frame in zip(frames, frames[1:]):
            self.assertEqual((frame[2] - previous[2]) & 65535, 1)
            self.assertLess((frame[3] - previous[3]) & 0xFFFFFFFF, 250)
        self.assertEqual(c.until('route_update')['cause'], 'position')
        c.telemetry(lambda frame: not frame[11] & 32)

    def test_engine_timeout_is_bounded_without_losing_clients(self):
        c = self.client()
        c.until('route_update')
        (self.data / 'hang').touch()
        began = time.monotonic()
        c.send({'type': 'set_goal', 'row': 1, 'col': 1})
        error = c.until('error', timeout=7)
        self.assertIn('timeout', error['error'])
        self.assertLess(time.monotonic() - began, 6)
        c.telemetry(lambda frame: frame[11] & 32)
        self.assertIsNone(self.proc.poll())

    def test_shutdown_releases_socket(self):
        self.proc.terminate()
        self.assertEqual(self.proc.wait(timeout=3), 0)


if __name__ == '__main__':
    unittest.main()
