# Reloj ESP32 — WSS saliente a Render

El reloj recibe la telemetría `$W,...` por un WebSocket seguro saliente a
`wss://<host>:443/reloj/ws` y devuelve los botones `$B,<botón>` por el mismo
canal. El USB serie (115200) queda como enlace de respaldo local. El transporte
UDP anterior se eliminó: ya no hay anuncio `$H,reloj` ni ráfagas de botones por
WiFi.

## Camino rápido

1. Copiá `watch_config.example.h` a `watch_config.h` y completá host, token y
   PEM de la raíz TLS (ver [Certificado de Render](#certificado-de-render)).
2. Instalá dependencias y compilá (ver [Dependencias y compilación](#dependencias-y-compilación)).
3. Al arrancar, el reloj conecta el WiFi guardado, espera hora NTP válida y
   abre el WSS. El monitor serie muestra `$D,wss conectado <host>/reloj/ws`.
4. Si no hay `watch_config.h`, el reloj sigue funcionando por USB.

## Contrato y estado

| Tema | Decisión |
|------|----------|
| Endpoint | `wss://WATCH_HOST:443/reloj/ws`, mismo puerto HTTP público de Render |
| Autenticación | `Authorization: Bearer WATCH_TOKEN` en el upgrade; sin token en la URL |
| Servidor → reloj | `$W,<rumbo>,<az>,<dist_m>,<o2>,<bat>,<ttr>,<dosis>,<flags>,<texto>\n` |
| Reloj → servidor | `$B,SOS\n` / `$B,CANCEL\n`, una sola vez por pulsación |
| Límite de línea | 200 bytes de payload (`MAX_LINE` del servidor); una línea más larga se descarta completa hasta el próximo salto |
| Ping/pong | arduinoWebSockets responde pong; heartbeat de 15 s con pong esperado en 3 s y 2 fallos antes de reconectar |
| Un solo reloj | El servidor rechaza una segunda conexión autenticada; el firmware no intenta robar la sesión |
| HELLO | No se usa: el contrato es solo `$W` / `$B` |
| TLS | `beginSslWithCA(host, 443, path, WATCH_TRUSTED_ROOT_PEM)`; nunca `setInsecure` |
| Cabecera extra | `setExtraHeaders("Authorization: Bearer …")` reemplaza el `Origin: file://` por defecto. 2.6.1 agrega el CRLF final, por eso el valor no debe incluirlo |
| API 2.6.1 | No usar `beginSSL`: en ESP32 la librería define `SSL_AXTLS` y su 4.º argumento es una huella SHA-1, no el PEM de la CA |

### Política de transporte sin duplicados

| Enlace | Recepción | Botón SOS |
|--------|-----------|-----------|
| WSS Render | `$W` por WebSocket | un único frame si está conectado |
| USB 115200 | `$W` por línea serie | una única línea, siempre |
| UDP 4210 | eliminado | eliminado |

Cada enlace entrega cada pulsación una sola vez. El USB se usa para pruebas
locales o como respaldo cuando no se configuró el WSS; en una EVA desplegada se
usa el WSS y el USB queda como alimentación/diagnóstico. Si ambos enlaces llegan
al mismo backend al mismo tiempo, ese backend debe deduplicar por transporte:
el firmware no repite el evento en una misma conexión.

### Reglas de reconexión

- No se intenta TLS hasta que `time(nullptr)` supere 2025-01-01 (NTP).
- Un intento de handshake se considera colgado a los 15 s y se corta.
- Reintentos acotados: 1, 2, 4, 8, 16 y luego 30 s como máximo.
- Una conexión exitosa reinicia el contador; una caída de WiFi también agenda el
  reintento.
- En 2.6.1 `setReconnectInterval(0)` **no** desactiva el auto-retry: elimina la
  espera y reintenta en cada `loop()`. El sketch permite un solo `loop()`
  inmediato y luego fija `WSS_LIBRARY_RECONNECT_MS = 60 s`, mayor que la ventana
  de intento; durante el backoff tampoco llama `ws.loop()`.
- La espera entre reintentos no bloquea el loop. La conexión TLS de la librería
  es síncrona y puede bloquear durante su timeout interno: el límite de 15 s
  del sketch no puede interrumpir esa llamada. Ese tiempo requiere medición en
  la placa. El OLED muestra `WSS RECONECTANDO`, `WSS NO CONFIGURADO` o
  `ESPERANDO SERVIDOR`.

## Certificado de Render

El ESP32 valida el certificado del servidor contra una raíz configurada. No se
incluye ninguna raíz en el repositorio y no hay validación insegura de respaldo.

1. Descargá la cadena real del servicio:

   ```bash
   openssl s_client -connect TU_HOST:443 -servername TU_HOST -showcerts </dev/null > cadena.pem
   ```

2. Mirá emisor y sujeto de la cadena. Render puede usar Let's Encrypt, Google
   Trust Services u otra CA pública; no asumas la raíz:

   ```bash
   openssl x509 -in cadena.pem -noout -subject -issuer
   grep -E '^ *[is]:' cadena.pem
   ```

3. Conseguí el certificado **raíz** de esa CA desde su repositorio oficial o
   desde el almacén de confianza del sistema (`/etc/ssl/certs`). Guardalo como
   `raiz.pem` y verificá la cadena (la hoja y los intermedios salen de
   `cadena.pem`; solo la raíz va al firmware):

   ```bash
   openssl verify -CAfile raiz.pem -untrusted cadena.pem hoja.pem
   ```

4. Convertí `raiz.pem` a un literal C y pegalo en `watch_config.h`:

   ```bash
   python3 - raiz.pem <<'PY'
   import sys
   lines = open(sys.argv[1]).read().splitlines()
   for i, line in enumerate(lines):
       print('  "%s\\n"%s' % (line, ' \\' if i < len(lines) - 1 else ''))
   PY
   ```

   Queda así:

   ```c
   #define WATCH_TRUSTED_ROOT_PEM \
     "-----BEGIN CERTIFICATE-----\n" \
     "…base64 de la raíz…\n" \
     "-----END CERTIFICATE-----\n"
   ```

5. Si Render rota la CA, repetí el proceso y recompilá. El PEM es información
   pública; el token sí es un secreto y nunca debe commitearse.

## Dependencias y compilación

| Componente | Versión documentada | Instalación |
|------------|---------------------|-------------|
| ESP32 Arduino core | `esp32:esp32@3.0.7` | `arduino-cli core install esp32:esp32@3.0.7 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json` |
| U8g2 (olikraus) | `2.35.30` | `arduino-cli lib install "U8g2@2.35.30"` |
| WebSockets (Markus Sattler / Links2004) | `2.6.1` | `arduino-cli lib install "WebSockets@2.6.1"` |

El core 3.x es necesario porque el sketch usa `ledcAttach`/`ledcWrite` de esa
API. La llamada TLS correcta en 2.6.1 es `beginSslWithCA(...)`; `beginSSL(...)`
acepta el PEM como huella y no configura la CA en ESP32. Configurá el índice una
vez:

```bash
arduino-cli config init
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

Compilar (con o sin `watch_config.h`; sin él el WSS queda deshabilitado y solo
se compila el enlace USB):

```bash
arduino-cli compile --fqbn esp32:esp32:nodemcu-32s hardware/reloj_esp32
```

Subir al NodeMCU ESP-32S:

```bash
arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:nodemcu-32s hardware/reloj_esp32
```

> En este entorno `arduino-cli` no está instalado, así que la compilación y la
> placa física **no se verificaron**. No se instalaron herramientas globales ni
> se descargó ningún toolchain.

## Pruebas de host

Los helpers de transporte viven en `watch_transport.h` y no dependen de Arduino:

```bash
make -C hardware/reloj_esp32/tests
```

Cubren el reensamblado de líneas (CR/LF/CRLF, límite de 200 bytes y descarte
seguro), el backoff acotado con wraparound de `millis()`, el bloqueo TLS por
reloj, la validación de configuración, la cabecera `Authorization` sin CRLF
final, el frame `$B,<botón>\n` y un probe de las firmas de 2.6.1 para ESP32.
Resultado esperado: `109 checks, 0 failures` y `2.6.1 ESP32 API probe OK`. El
probe solo modela la firma/semántica de la librería; no emula TLS, WiFi ni el
cliente WebSocket real. Eso solo lo prueba compilar y usar la placa.

## USB de respaldo

Por el puerto serie a 115200 se puede enviar una línea `$W,...\n` por
actualización, igual que antes. Los botones siempre escriben `$B,SOS` o
`$B,CANCEL` por USB; si el WSS está conectado, además envían un único frame por
WebSocket.

## Diagnóstico

| Síntoma | Revisar |
|---------|---------|
| `$D,wss sin watch_config.h` | Falta copiar `watch_config.example.h` a `watch_config.h` |
| `$D,wss config invalida` | Host con `https://`, token vacío o PEM raíz ausente |
| `$D,wss esperando hora NTP` | El WiFi no llega a NTP; TLS no se intenta hasta tener hora válida |
| `$D,wss error` / desconexiones | CA raíz equivocada, token rechazado (401) o segundo reloj conectado |
| OLED `WSS RECONECTANDO` | Reintento en curso; el backoff máximo es 30 s |
| Sin datos por WSS pero sí por USB | Verificá el token del servidor y que el endpoint sea `/reloj/ws` |
