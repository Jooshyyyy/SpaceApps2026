"""Opt-in real Docker smoke: WATCH_TEST_IMAGE=mars-eva:ws-check python3 -m
unittest discover -s prototipo/tests -p test_render_container.py -v
"""
import os
import subprocess
import time
import unittest
import urllib.request

from test_watch_websocket import TOKEN, W_FRAME, http_json, ws_handshake


@unittest.skipUnless(os.environ.get('WATCH_TEST_IMAGE'), 'set WATCH_TEST_IMAGE for Docker smoke')
class RenderContainerTests(unittest.TestCase):
    def test_public_web_device_frames_and_buttons(self):
        image = os.environ['WATCH_TEST_IMAGE']
        cid = subprocess.check_output([
            'docker', 'run', '--rm', '-d', '-p', '127.0.0.1::10000',
            '-e', 'WATCH_TOKEN=' + TOKEN, image,
        ], text=True).strip()
        try:
            binding = subprocess.check_output(['docker', 'port', cid, '10000/tcp'], text=True).strip()
            port = int(binding.rsplit(':', 1)[1])
            deadline = time.monotonic() + 20
            while True:
                try:
                    status, state = http_json(port, 'GET', '/estado')
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            self.assertEqual(status, 200)
            self.assertTrue(state['cloud'])
            self.assertFalse(state['ws'])
            for path in ('/app.html', '/watch-status.js'):
                with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}') as response:
                    self.assertEqual(response.status, 200)
            status, _, _, ws = ws_handshake(port)
            self.assertEqual(status, 101)
            try:
                status, _ = http_json(port, 'POST', '/reloj/tomar', b'containersmoke')
                self.assertEqual(status, 200)
                status, _ = http_json(port, 'POST', '/w', W_FRAME, {'X-Sesion': 'containersmoke'})
                self.assertEqual(status, 200)
                self.assertEqual(ws.recv_text(), W_FRAME)
                ws.send_text('$B,SOS\n')
                deadline = time.monotonic() + 3
                while True:
                    status, state = http_json(port, 'GET', '/reloj?since=0')
                    if state['events'] or time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                self.assertEqual(status, 200)
                self.assertEqual(state['events'][-1]['button'], 'SOS')
                self.assertEqual(state['events'][-1]['via'], 'ws')
            finally:
                ws.close()
            check = subprocess.run([
                'docker', 'exec', cid, 'sh', '-c',
                'test -x core_engine/eva_engine && test -x communication/eva_bus '
                '&& test ! -d data/raw && test ! -d hardware',
            ], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, (check.stdout, check.stderr))
        finally:
            # Only the container created by this test is stopped; --rm cleans it.
            stopped = subprocess.run(['docker', 'stop', '--time', '1', cid], capture_output=True, text=True)
            self.assertEqual(stopped.returncode, 0, (stopped.stdout, stopped.stderr))
