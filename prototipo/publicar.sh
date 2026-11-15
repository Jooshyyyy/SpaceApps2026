#!/usr/bin/env bash
# Publica el prototipo en internet con un túnel rápido de Cloudflare y genera los QR.
#   - Visitantes (jueces): ven la EVA en vivo (tablet, reloj y base), sin controlar nada.
#   - Operador: link con token (.token_operador) para la tablet que maneja la EVA real.
# Requisitos: servidor.py corriendo, cloudflared y qrencode instalados.
# Uso: ./publicar.sh   (Ctrl+C cierra el túnel)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8791}"

command -v cloudflared >/dev/null || { echo "Falta cloudflared:  sudo pacman -S cloudflared"; exit 1; }
command -v qrencode >/dev/null || { echo "Falta qrencode:  sudo pacman -S qrencode"; exit 1; }
curl -fs "http://127.0.0.1:$PORT/yo" >/dev/null || { echo "Primero arranca el servidor:  python3 -I servidor.py --port $PORT"; exit 1; }
[ -s .token_operador ] || { echo "No existe .token_operador (lo crea servidor.py al arrancar)"; exit 1; }

LOG="$(mktemp)"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null; rm -f "$LOG"' EXIT

URL=""
for _ in $(seq 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
[ -n "$URL" ] || { echo "No se pudo abrir el túnel:"; cat "$LOG"; exit 1; }

TOK="$(tr -d '[:space:]' < .token_operador)"
mkdir -p qr && chmod 700 qr
qrencode -s 12 -m 2 -o qr/visitante.png "$URL/"
qrencode -s 12 -m 2 -o qr/base.png "$URL/base"
( umask 077; qrencode -s 12 -m 2 -o qr/operador.png "$URL/op/$TOK" )

echo
echo "Visitantes (jueces):  $URL/        → qr/visitante.png  (base en vivo: qr/base.png)"
echo "Operador (tablet):   $URL/op/<token> → qr/operador.png  (NO lo compartas)"
echo "Operador (base):     $URL/op/<token>/base"
echo
qrencode -t ANSIUTF8 -m 1 "$URL/"
echo "Túnel abierto. Ctrl+C para cerrarlo."
wait "$PID"
