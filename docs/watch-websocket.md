# Reloj por WebSocket público (`/reloj/ws`)

El reloj ESP32 ahora puede hablar con `servidor.py` por un WebSocket saliente en
el mismo puerto HTTP público que la web. En modo cloud el servidor **no abre
UDP 4210 ni puertos serie**: el único enlace del reloj es `/reloj/ws`. El
transporte local (USB y UDP) sigue siendo el comportamiento por defecto cuando
no se usa `--cloud`.

Este documento es el contrato congelado entre backend y firmware. Si el
firmware y el servidor discrepan, este archivo es la referencia.

## Camino rápido

1. Generá un secreto y arrancá el modo cloud:

   ```bash
   export WATCH_TOKEN="$(openssl rand -hex 32)"
   python3 prototipo/servidor.py --cloud --host 0.0.0.0 --port "${PORT:-8791}" --backend .
   ```

2. El firmware abre `wss://<host-público>/reloj/ws` con
   `Authorization: Bearer <WATCH_TOKEN>` (nunca en la URL).
3. Verificá `GET /estado`: `cloud` y `ws` deben ser `true` mientras el reloj
   está conectado.

Dependencia: **Python 3.11 o posterior, solo biblioteca estándar**. No hace
falta instalar paquetes: `prototipo/requirements.txt` no declara dependencias y
`python3 -m pip install -r prototipo/requirements.txt` es un no-op.

## Contrato del dispositivo

| Tema | Contrato |
|---|---|
| URL | `GET /reloj/ws` en el mismo puerto HTTP de la web. Sin query string. |
| TLS | El servidor habla `ws://`; el proxy público (Render) termina TLS. El firmware usa `wss://`. |
| Autenticación | Cabecera `Authorization: Bearer <WATCH_TOKEN>` en el upgrade. Secreto independiente de la web. |
| Navegadores | Un `Origin` presente se rechaza con `403`: el endpoint es solo del dispositivo. |
| Tokens en URL | Cualquier query string se rechaza con `400`. No se aceptan `?token=` ni similares. |
| Un solo reloj | Un segundo upgrade autenticado se rechaza con `409`; nunca se roba el enlace del primero. |
| Versión | RFC 6455, versión 13. No se negocia subprotocolo. |

Rechazos del upgrade, en orden de evaluación:

| Respuesta | Causa |
|---|---|
| `400` | Ruta distinta de `/reloj/ws`, query string, upgrade o versión inválidos, `Sec-WebSocket-Key` inválida. |
| `401` | Falta `Authorization`, el esquema no es `Bearer`, o el token no coincide (`hmac.compare_digest`). |
| `403` | La petición trae `Origin`. |
| `409` | Ya hay un reloj WebSocket conectado. |
| `404` | `/reloj/ws` no está habilitado (arranque local sin `--cloud`). |

Los cuerpos de error son JSON `{"error": "<motivo>"}` y nunca repiten el token.

### Servidor a reloj

Solo texto, trama `$W` ya validada por `POST /w`, **sin modificar**, con su
newline final:

```text
$W,<rumbo|-1>,<az>,<dist_m>,<o2%>,<bat%>,<ttr_s>,<dosis_uSv_x10>,<flags>,<texto>\n
```

- Ejemplo: `$W,090,180,42,95,88,120,3,0,OK\n`
- Tamaño máximo: 200 bytes (incluido `\n`), igual que `POST /w`.
- El servidor no agrega cabeceras, prefijos, JSON ni reemplazos.
- Envío acotado: se espera hasta 2 s a que el socket acepte escritura; no hay
  cola ilimitada. Si no hay reloj, `POST /w` responde `503`.
- El WebSocket usa TCP: dentro de una conexión cada push se entrega una sola
  vez; el firmware no necesita repetir tramas `$W` como hacía por UDP. Tras
  una reconexión, el siguiente push vuelve a llegar completo.

### Reloj a servidor

Solo texto, un mensaje por pulsación:

```text
$B,<botón>\n
```

- `<botón>`: uno o más caracteres ASCII alfanuméricos (`bytes.isalnum()`).
- La línea completa, sin el `\n`, debe medir menos de 24 bytes, igual que el
  parser USB/UDP existente.
- Ejemplos válidos: `$B,SOS\n`, `$B,CANCEL\n`.
- Cada mensaje aceptado llama a la misma `SerialPort._button` existente y suma
  un evento con `via: "ws"`. Orden y `seq` son los globales del reloj.
- No hay deduplicación por WebSocket: TCP ya es confiable, enviá una vez por
  pulsación. (El anti-duplicado de 1 s solo aplica al transporte UDP `wifi`).

Cierres ante entrada inválida:

| Código | Causa |
|---|---|
| `1002` | Frame sin máscara, bits reservados, opcode no soportado, control inválido o fragmentación inválida. |
| `1003` | Mensaje binario. |
| `1008` | Falta el newline, no empieza con `$B,`, botón no alfanumérico o línea demasiado larga. |
| `1009` | Frame o mensaje mayor de 200 bytes. |
| `1001` | Heartbeat sin respuesta (ver abajo). |

Un mensaje rechazado no crea eventos ni incrementa `seq`; el estado visible
queda igual y el enlace se cierra.

### Heartbeat y limpieza

- El servidor envía `ping` con payload `hb` cada 20 s.
- El firmware debe responder `pong` (las librerías WebSocket estándar lo
  hacen automáticamente).
- Si pasan 15 s desde el ping sin `pong`, el servidor cierra con `1001`.
- El servidor responde los `ping` que envíe el dispositivo y atiende `close`.
- Al desconectarse, el enlace se libera; un reloj puede reconectar y vuelve a
  ser el único activo. Los eventos ya aceptados permanecen.

## Contrato HTTP existente

El modo cloud **no cambia** estos endpoints. La web sigue siendo pública, tal
como se aprobó; el único secreto es el del dispositivo.

| Endpoint | Contrato |
|---|---|
| `POST /reloj/tomar` | Cuerpo con sesión alfanumérica. `400` si es inválida. La última pantalla que lo llama controla el reloj. |
| `POST /w` | Cabecera `X-Sesion` igual a la sesión dueña. `409` si otra pantalla controla; `400` si la trama `$W` es inválida; `503` si ningún transporte está disponible; `200` con el estado si se envió. Startswith `$W,`, newline final y ASCII imprimible, máximo 200 bytes. |
| `GET /estado` | Estado del reloj (tabla siguiente). |
| `GET /reloj?since=N` | `{"events": [...], "tty": ..., ...}`: eventos con `seq > N` más todos los campos de estado. `since` no numérico equivale a `0`. |

Antes de que una pantalla llame a `/reloj/tomar`, no hay sesión dueña y `POST
/w` no se rechaza por `X-Sesion` (comportamiento previo conservado); intenta
escribir y devuelve `503` si no hay transporte.

### Estado público del reloj

`GET /estado` y `GET /reloj` exponen exactamente estos campos (la tabla es el
contrato; el orden de claves no importa):

| Campo | Tipo | Significado |
|---|---|---|
| `tty` | string | Ruta serie local; en cloud es el valor sintético `"wss"`. |
| `open` | bool | Hay algún transporte disponible: USB abierto, WiFi reciente o WebSocket conectado. |
| `usb` | bool | El puerto serie está abierto. En cloud siempre `false`. |
| `wifi` | string o `null` | IP del reloj anunciada por UDP en los últimos 6 s. En cloud siempre `null`. |
| `sent` | int | Cantidad de `POST /w` enviados con éxito (una por escritura, aunque haya más de un transporte). |
| `error` | string | Último error de transporte; vacío tras un envío exitoso. |
| `ws` | bool | Hay un reloj conectado por `/reloj/ws`. |
| `cloud` | bool | El proceso corre con `--cloud` (sin UDP ni serie). |

Ejemplo cloud conectado:

```json
{"tty": "wss", "open": true, "usb": false, "wifi": null,
 "sent": 12, "error": "", "ws": true, "cloud": true}
```

Ejemplo cloud desconectado:

```json
{"tty": "wss", "open": false, "usb": false, "wifi": null,
 "sent": 12, "error": "", "ws": false, "cloud": true}
```

`GET /reloj?since=1` en cloud agrega los eventos:

```json
{"events": [{"seq": 2, "t": 1770000000.25, "button": "CANCEL", "via": "ws"}],
 "tty": "wss", "open": true, "usb": false, "wifi": null,
 "sent": 12, "error": "", "ws": true, "cloud": true}
```

| Campo de evento | Significado |
|---|---|
| `seq` | Entero global del reloj, empieza en 1 y crece por cada botón aceptado. |
| `t` | Epoch en segundos (float) del servidor al aceptar el botón. |
| `button` | Nombre recibido, por ejemplo `SOS` o `CANCEL`. |
| `via` | `ws` en cloud; `usb` o `wifi` en el prototipo local. |

Se conservan los últimos 20 eventos y sobreviven a la desconexión del reloj.
El indicador de la web solo distingue WiFi/USB; para cloud la fuente de verdad
es el campo `ws` de `/estado` o `/reloj`.

## Modos de arranque

| Modo | Comando | UDP 4210 | Puerto serie | `/reloj/ws` | Host por defecto |
|---|---|---|---|---|---|
| Local heredado | `python3 prototipo/servidor.py` | Sí | Sí, `--tty auto` | `404` | `127.0.0.1` |
| Cloud | `python3 prototipo/servidor.py --cloud ...` | No | No | Sí, con token | `0.0.0.0` |

En cloud, si `--backend` no contiene `communication/eva_bus`,
`core_engine/eva_engine` y `data/processed/cost.bin`, el servidor arranca igual
y desactiva bus/rutas (`/bus`, `/bus/cmd`, `/bus/reset` y `/plan` responden
`503`). Esto evita que la web pública dependa de la ingesta pesada o de
binarios nativos. `--port` acepta el `PORT` que inyecta Render. `WATCH_TOKEN`
solo se lee en modo cloud; si falta, está vacío o contiene espacios, el proceso
termina sin abrir el puerto.

El proxy público debe reenviar upgrades WebSocket al mismo puerto HTTP; no se
publica ningún puerto adicional. Tras cada deploy o reinicio el firmware debe
reconectar; el servidor acepta de nuevo al reloj cuando el anterior se libera.

## Verificación

```bash
python3 -m unittest discover -s prototipo/tests -v
```

Comprobación manual mínima (sin hardware):

```bash
curl -s http://127.0.0.1:8791/estado
# {"tty": "wss", ... "cloud": true}
```

Checklist:

- [ ] `WATCH_TOKEN` está definido solo en el entorno del servicio y no en URL,
      logs, imágenes ni el repositorio.
- [ ] El firmware usa `wss://<host>/reloj/ws` y `Authorization: Bearer ...`.
- [ ] `GET /estado` muestra `ws: true` con el reloj conectado y `ws: false`
      después de desconectarlo.
- [ ] Una segunda conexión autenticada recibe `409` mientras la primera sigue
      recibiendo `$W`.
- [ ] Un `$B,SOS\n` aparece en `GET /reloj?since=0` con `via: "ws"`.
- [ ] Reiniciar el servicio no descarga ni ingiere datos de Marte.

## Siguiente paso

El contrato del dispositivo está implementado y probado en el backend. El
firmware (T2) consume este documento para el WSS saliente con TLS verificado,
reconexión y fallback USB; el despliegue (T3) define el Docker/blueprint de
Render y la preparación separada de las capas procesadas.
