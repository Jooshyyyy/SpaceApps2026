"""Puente laptop ↔ nube para el reloj ESP32.

El servidor corre en la nube; el reloj sigue conectado a esta laptop por Bluetooth (o USB).
Este puente recibe las tramas del servidor (GET /reloj/stream) y se las pasa al reloj, y manda
a la nube los botones del reloj (POST /reloj/boton). Solo usa la librería estándar.

Uso:  python3 -I puente_reloj.py --servidor https://<tu-app>
      (token del reloj: variable RELOJ_TOKEN o archivo .token_reloj, el mismo que usa el servidor)
"""
import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # para reutilizar el enlace USB/Bluetooth de servidor.py (python -I no lo agrega)
from servidor import SerialPort  # noqa: E402


def read_token() -> str:
    tok = os.environ.get("RELOJ_TOKEN", "").strip()
    if not tok:
        try:
            tok = (HERE / ".token_reloj").read_text().strip()
        except OSError:
            pass
    if len(tok) < 20 or not tok.isalnum():
        raise SystemExit("Falta el token del reloj: exporta RELOJ_TOKEN o crea .token_reloj")
    return tok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--servidor", required=True, help="URL pública del servidor, p. ej. https://ares.onrender.com")
    ap.add_argument("--tty", default="auto")
    args = ap.parse_args()
    base = args.servidor.rstrip("/")
    if not base.startswith("https://") and not base.startswith("http://127.0.0.1"):
        raise SystemExit("Usa https:// para no mandar el token sin cifrar")
    token = read_token()
    if args.tty == "auto":
        ports = sorted(Path("/dev").glob("ttyUSB*"))
        args.tty = str(ports[0]) if ports else "/dev/ttyUSB0"
    watch = SerialPort(args.tty)  # USB + Bluetooth (+ WiFi local) hacia el reloj
    stats = {"frames": 0, "online": False}

    def buttons():  # reloj → nube
        seq = max((e["seq"] for e in watch.events_since(0)), default=0)
        while True:
            for e in watch.events_since(seq):
                seq = e["seq"]
                req = urllib.request.Request(base + "/reloj/boton", data=f"$B,{e['button']}".encode(),
                                             headers={"X-Reloj-Token": token}, method="POST")
                for _ in range(3):
                    try:
                        urllib.request.urlopen(req, timeout=10).read()
                        print(f"botón {e['button']} → nube", flush=True)
                        break
                    except (urllib.error.URLError, OSError) as exc:
                        print("no se pudo mandar el botón:", exc, flush=True)
                        time.sleep(1)
            time.sleep(0.2)

    def report():
        while True:
            st = watch.status()
            links = [n for n, ok in (("Bluetooth", st.get("bt")), ("USB", st.get("usb"))) if ok]
            print(f"nube {'OK' if stats['online'] else 'desconectada'} · reloj por {' + '.join(links) or 'nada'}"
                  f" · {stats['frames']} tramas", flush=True)
            time.sleep(10)

    threading.Thread(target=buttons, daemon=True).start()
    threading.Thread(target=report, daemon=True).start()
    backoff = 1
    while True:  # nube → reloj
        try:
            req = urllib.request.Request(base + "/reloj/stream", headers={"X-Reloj-Token": token})
            with urllib.request.urlopen(req, timeout=15) as resp:
                stats["online"], backoff = True, 1
                for line in resp:
                    if line.startswith(b"$W,") and line.endswith(b"\n") and len(line) < 200:
                        watch.write(line)
                        stats["frames"] += 1
        except urllib.error.HTTPError as exc:
            stats["online"] = False
            if exc.code == 403:
                raise SystemExit("El servidor rechazó el token del reloj (RELOJ_TOKEN distinto)")
            print("servidor:", exc, flush=True)
        except (urllib.error.URLError, OSError) as exc:
            stats["online"] = False
            print("sin conexión con la nube:", exc, flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 15)


if __name__ == "__main__":
    main()
