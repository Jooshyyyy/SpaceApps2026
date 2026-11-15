# ARES · Mars EVA Nav

Prototipo de navegación para una actividad extravehicular (EVA) en Marte: una interfaz para la tablet del astronauta, un panel de base de control y un reloj ESP32 con OLED. Permite visualizar terreno y rutas, estimar consumibles y retorno al refugio, y simular contingencias como tormentas, radiación, pérdida de enlace y SOS.

> **Deploy en Vercel:** Vercel puede alojar el frontend estático, pero **no ejecuta `servidor.py` tal como está escrito**. Para la aplicación funcional necesitás además una máquina persistente con el backend y los datos procesados. Esta carpeta no incluye ese backend ni sus scripts de descarga/ingesta; más abajo se identifica qué falta, sin asumir comandos que no están disponibles.

## Descripción técnica

| Componente | Implementación y responsabilidad |
|---|---|
| Frontend | `app.html`: HTML/CSS/JavaScript sin build, Leaflet 1.9.4 para mapas 2D y Three.js 0.160.0 para terreno 3D. Incluye las vistas de astronauta, base y reloj simulado. |
| Servidor HTTP y puente | `servidor.py`: biblioteca estándar de Python; sirve archivos, expone endpoints HTTP y conecta las pantallas con el bus y el reloj. Usa threads y estado en memoria. |
| Bus de eventos | Ejecutable externo `communication/eva_bus`. El servidor lo inicia y se conecta por WebSocket local; el navegador consulta el servidor por HTTP. |
| Motor de rutas | Ejecutable externo `core_engine/eva_engine`. Calcula rutas y simulaciones sobre las capas de `data/processed/`. |
| Generación de misión | `generar_mision.py`: combina el área de interés, pendientes, transitabilidad y posiciones reales de Curiosity para regenerar `mission.json` con rutas de vista previa. |
| Hardware opcional | ESP32 + OLED SH1106; recibe telemetría y devuelve eventos SOS por serie USB o UDP en la red local. |

```text
Navegador (tablet / base)
    │ HTTP: archivos, /plan, /bus*, /eva/*, /reloj*, /estado, /w
    ▼
servidor.py ── WebSocket local ── eva_bus ── eva_engine
    │                                      │
    └── USB 115200 / UDP 4210 ── ESP32      └── data/processed/
```

El estado compartido no se persiste: reiniciar el proceso lo pierde. La aplicación representa una EVA compartida, no sesiones independientes por usuario. Abrí la tablet y la base contra el mismo backend.

## 1. Requisitos y estructura

Para ejecutar el sistema completo necesitás:

- Linux con Python 3.10 o posterior como base recomendada; el servidor usa `termios` y dispositivos `/dev/ttyUSB*`, por lo que no corre directamente en Windows.
- El repositorio del backend del equipo, referido por el código como `SpaceApps2026`, con sus ejecutables compilados para el sistema anfitrión.
- Las capas procesadas de la misma zona y resolución que la misión.
- Acceso a internet para Leaflet, Three.js y las teselas NASA Mars Trek. La aplicación actual no es completamente offline.
- Opcional: ESP32 y firmware del reloj. No hace falta conectar el reloj para abrir las pantallas web.

Esta copia contiene el frontend, el servidor puente, el generador y los JSON de misión/waypoints. **No contiene** `core_engine/`, `communication/`, `data/`, `scripts/` ni `hardware/`.

Podés organizar las dos carpetas así:

```text
workspace/
├── SpaceApps2026/                  # Backend externo, no incluido aquí
│   ├── core_engine/eva_engine      # Ejecutable
│   ├── communication/eva_bus       # Ejecutable
│   └── data/processed/
│       ├── aoi.json
│       ├── elevation.bin
│       ├── slope.bin
│       └── cost.bin
└── prototipo/                     # Esta carpeta
    ├── app.html
    ├── servidor.py
    ├── generar_mision.py
    ├── mission.json
    ├── msl_waypoints.json
    ├── logo_ares.png
    └── oled_logo.png
```

Si el prototipo está dentro del repositorio completo, los scripts también detectan el backend en la carpeta superior cuando allí existe `core_engine/`. Usar `--backend` explícito evita ambigüedades.

## 2. Descarga e ingesta de datos

### 2.1. Obtener el backend y el terreno

Primero pedile al equipo la URL del repositorio `SpaceApps2026`, sus instrucciones de compilación y el dataset de la zona `curiosity-2025-eva-site`.

**Este checkout no permite reproducir todavía la ingesta del terreno desde cero:** no incluye la URL del repositorio del backend, el identificador/URL del DEM de origen, los scripts de descarga y transformación ni los comandos de compilación. No alcanza con descargar los waypoints de NASA.

En el repositorio del backend, seguí su procedimiento para producir:

| Archivo | Consumidor y requisito |
|---|---|
| `aoi.json` | El generador lee `name`, `aoi.rows`, `aoi.cols` y `aoi.bbox_lonlat` con `north`, `south`, `east`, `west`; también puede leer `environment_summary`. |
| `slope.bin` | Pendientes `float32` en orden de filas; lo usan el generador y el frontend. |
| `cost.bin` | Un byte por celda; los scripts consideran transitables los valores de 1 a 254. |
| `elevation.bin` | Elevaciones utilizadas por el servidor/frontend para la vista del terreno. Debe provenir de la misma ingesta. |

La interfaz y el planificador del prototipo asumen una grilla de **1000 × 1000** y un refugio en **fila 500, columna 500**. No sustituyas las capas por otro DEM o resolución sin adaptar y validar el código. Para esa grilla, `slope.bin` ocupa 4.000.000 bytes y `cost.bin` 1.000.000 bytes; el tamaño por sí solo no valida coordenadas, unidades ni contenido.

### 2.2. Posiciones de Curiosity

Ya se incluye `msl_waypoints.json`, suficiente como entrada para generar la misión si tenés el backend y las capas. Para descargar una versión actualizada desde NASA, ejecutá desde `prototipo/`:

```bash
curl --fail --location \
  'https://mars.nasa.gov/mmgis-maps/MSL/Layers/json/MSL_waypoints.json' \
  --output msl_waypoints.download.json

# Comprobar el formato antes de reemplazar la copia incluida.
python3 - <<'PY'
import json
from pathlib import Path
p = Path('msl_waypoints.download.json')
data = json.loads(p.read_text())
assert data['type'] == 'FeatureCollection'
assert data['features'], 'NASA devolvió una colección vacía'
for feature in data['features']:
    assert 'sol' in feature['properties']
    assert len(feature['geometry']['coordinates']) >= 2
print(f"Waypoints legibles: {len(data['features'])}")
PY

# Solo si la descarga y la validación anteriores terminaron correctamente:
cp msl_waypoints.json msl_waypoints.backup.json
mv msl_waypoints.download.json msl_waypoints.json
```

Esta validación comprueba estructura, no compatibilidad geográfica. El generador elige los soles **4582, 4544, 4612 y 4530**; esos puntos deben existir dentro del área de interés. La fuente NASA puede cambiar: conservá una copia de los datos que usaste si necesitás reproducibilidad.

Las teselas del mapa se solicitan en tiempo de ejecución a [NASA Mars Trek](https://trek.nasa.gov/), usando la capa `curiosity_hirise_mosaic`. Son una fuente visual separada de las capas binarias del motor; no reemplazan la ingesta del terreno.

### 2.3. Comprobar los artefactos y generar la misión

Desde `prototipo/`, con el backend preparado en la ruta del ejemplo:

```bash
# Comprobación de presencia/permisos; no compila ni ingiere datos.
test -x ../SpaceApps2026/core_engine/eva_engine && \
test -x ../SpaceApps2026/communication/eva_bus && \
test -s ../SpaceApps2026/data/processed/aoi.json && \
test -s ../SpaceApps2026/data/processed/elevation.bin && \
test -s ../SpaceApps2026/data/processed/slope.bin && \
test -s ../SpaceApps2026/data/processed/cost.bin && \
echo 'Artefactos presentes'

# Guardá la misión anterior: el generador sobrescribe mission.json.
cp mission.json mission.backup.json
python3 generar_mision.py --backend ../SpaceApps2026

python3 - <<'PY'
import json
from pathlib import Path
m = json.loads(Path('mission.json').read_text())
assert m['stops'], 'No se generaron paradas con ruta'
assert all(s['path'] for s in m['stops'])
print(f"Misión lista: {len(m['stops'])} paradas")
PY
```

No continúes si falta algún artefacto o falla la generación. El generador imprime cada parada calculada y puede omitir las que no tengan ruta. La misión incluida no sustituye las capas ni los ejecutables requeridos por el servidor.

## 3. Arranque local y comprobación

Desde `prototipo/`:

```bash
python3 servidor.py --backend ../SpaceApps2026 --port 8791 --bus-port 8765
```

No hay dependencias Python de terceros que instalar para estos dos scripts. El comando anterior inicia también `eva_bus`; no lances otra instancia en el mismo puerto.

- Tablet: <http://127.0.0.1:8791/>
- Base de control: <http://127.0.0.1:8791/base>
- Estado del bus: <http://127.0.0.1:8791/bus>
- Estado del reloj: <http://127.0.0.1:8791/estado>

En otra terminal:

```bash
curl --fail http://127.0.0.1:8791/bus
curl --fail http://127.0.0.1:8791/mission.json
curl --fail --output /dev/null http://127.0.0.1:8791/slope.bin
curl --fail --output /dev/null http://127.0.0.1:8791/elevation.bin
```

Verificá que `/bus` indique `connected: true` después de unos segundos. En el navegador, comprobá las vistas 2D/3D, iniciá una EVA y abrí la base en otra pestaña: debe recibir el estado y poder ordenar el regreso. Un reloj desconectado no implica que el bus esté caído.

El servidor escucha **solo en `127.0.0.1`**. Para una tablet Android por USB, sin Wi-Fi:

```bash
adb reverse tcp:8791 tcp:8791
```

Abrí `http://127.0.0.1:8791` en la tablet. Para acceso desde otra máquina necesitás un proxy hacia el servidor; el script no tiene una opción `--host`.

## 4. Deploy: Vercel + backend persistente

### Qué se puede desplegar sin modificar el servidor

| Modalidad | Resultado |
|---|---|
| Solo los archivos actuales en Vercel | **No es una demo funcional:** faltan `slope.bin`, `elevation.bin` y todos los endpoints dinámicos. |
| Frontend y capas en Vercel, sin backend | Vista estática limitada con rutas precalculadas; no hay inicio de EVA funcional, recálculo ni sincronización con la base. |
| Frontend en Vercel + proxy a backend persistente | Arquitectura propuesta para conservar las funciones actuales. Requiere preparar y proteger el backend por separado. |
| `servidor.py` directamente como Vercel Function | No compatible tal cual: no expone un handler/app para ese runtime y depende de un proceso continuo y estado local. |

El problema no es solo WebSocket: el navegador usa HTTP. El servidor necesita mantener `eva_bus`, threads y memoria compartida durante la EVA, además de conectarse a USB/UDP si hay hardware. Convertirlo en funciones independientes requeriría otro diseño de estado, bus y gateway de hardware; **esta guía no implementa ese refactor**.

### 4.1. Preparar el backend fuera de Vercel

1. Usá una máquina Linux persistente y confiable: una PC local para la demo o una VM/servidor para acceso remoto.
2. Instalá allí el backend, los datos y esta carpeta; completá las secciones 1–3 **en esa máquina**.
3. Mantené el comando de arranque bajo un supervisor, por ejemplo `systemd`, con directorio de trabajo y rutas absolutas correctas. Usá **una sola instancia** para conservar el estado compartido actual.
4. Configurá un reverse proxy con un dominio HTTPS, por ejemplo `https://eva-backend.example.com`, hacia `http://127.0.0.1:8791`. `example.com` es un marcador: reemplazalo por tu dominio real. El puerto 8765 del bus debe seguir siendo local; no necesitás publicarlo.
5. Antes de exponerlo, agregá protección de acceso tanto al frontend como al origen y restringí el proxy a las rutas necesarias. **El servidor actual no implementa autenticación/autorización de usuarios**, permite órdenes compartidas y usa `SimpleHTTPRequestHandler`, que también puede servir archivos del directorio del prototipo. No lo publiques sin restricciones ni pongas secretos en ese directorio.
6. Si usás reloj, el backend debe correr en la máquina con USB o con alcance a su red local. Una VM en internet no descubre el broadcast UDP del ESP32 de tu casa; necesitás conectividad/gateway adicional, que no está implementado aquí.

No alcanza con poner una IP privada o `localhost` en Vercel: el proxy de Vercel debe poder alcanzar el origen HTTPS. Si usás un túnel para una demo local, protegelo y mantené encendidos tanto el túnel como la PC. CORS por sí solo no protege el backend.

### 4.2. Preparar el frontend para Vercel

Creá una carpeta de publicación que contenga **solo**:

```text
publicacion/
├── app.html
├── mission.json
├── logo_ares.png
├── oled_logo.png
└── vercel.json
```

Copiá los cuatro archivos desde este prototipo. No incluyas scripts Python, backups, fuentes del backend ni datos privados en la carpeta pública. En la configuración siguiente, las capas binarias se sirven desde el backend: no hace falta copiarlas a Vercel.

Creá `vercel.json` dentro de esa carpeta, reemplazando **todas** las apariciones de `https://eva-backend.example.com` por el origen HTTPS protegido que preparaste:

```json
{
  "rewrites": [
    { "source": "/", "destination": "/app.html" },
    { "source": "/base", "destination": "/app.html?rol=base" },
    { "source": "/plan", "destination": "https://eva-backend.example.com/plan" },
    { "source": "/bus", "destination": "https://eva-backend.example.com/bus" },
    { "source": "/bus/:path*", "destination": "https://eva-backend.example.com/bus/:path*" },
    { "source": "/eva/:path*", "destination": "https://eva-backend.example.com/eva/:path*" },
    { "source": "/reloj", "destination": "https://eva-backend.example.com/reloj" },
    { "source": "/reloj/:path*", "destination": "https://eva-backend.example.com/reloj/:path*" },
    { "source": "/estado", "destination": "https://eva-backend.example.com/estado" },
    { "source": "/w", "destination": "https://eva-backend.example.com/w" },
    { "source": "/slope.bin", "destination": "https://eva-backend.example.com/slope.bin" },
    { "source": "/elevation.bin", "destination": "https://eva-backend.example.com/elevation.bin" }
  ]
}
```

Estas reglas mantienen las URLs del mismo origen que usa `app.html`, sin cambiar JavaScript. Los parámetros de consulta y los POST deben llegar al backend; comprobalo después de publicar. Los rewrites **no agregan autenticación** ni ocultan/protegen por sí mismos el origen. La misión publicada debe corresponder a las capas del backend.

### 4.3. Publicar en Vercel

Desde la carpeta `publicacion/`, con Node.js/npm y una cuenta de Vercel:

```bash
npx vercel login
npx vercel
```

En el setup elegí:

| Ajuste | Valor |
|---|---|
| Framework Preset | `Other` |
| Root Directory | La carpeta de publicación |
| Build Command | Vacío, sin build |
| Output Directory | La raíz de publicación (`.`), no `dist` |
| Install Command | Sin instalación de dependencias del proyecto |

También podés importar un repositorio que contenga esa carpeta desde el dashboard de Vercel y configurar los mismos valores. No selecciones un framework Python para publicar estos archivos estáticos.

Primero validá la URL de preview. Cuando funcione y el acceso esté protegido, publicá a producción:

```bash
npx vercel --prod
```

No se requieren variables de entorno en el frontend actual. El dominio del backend se configura en los rewrites; no existe una variable `API_URL` consumida por `app.html`.

### 4.4. Checklist de aceptación

- [ ] `/` abre la tablet y `/base` abre la base.
- [ ] `mission.json`, `slope.bin` y `elevation.bin` responden correctamente; las capas no devuelven HTML de error/login.
- [ ] `/bus` devuelve JSON con `connected: true`, no una página estática ni un error del proxy.
- [ ] Se puede elegir un punto nuevo y obtener una ruta desde `/plan`.
- [ ] Iniciar EVA funciona a través del POST `/bus/reset` y la base recibe el estado.
- [ ] Las órdenes de regreso y el SOS se reflejan entre las dos pantallas.
- [ ] No hay errores de red en las vistas 2D/3D y las dependencias CDN cargan.
- [ ] El origen no permite acceso anónimo a controles ni archivos internos.
- [ ] Si corresponde, el reloj físico recibe datos desde el backend local.

**Estado de esta guía:** los comandos describen el setup, no prueban que exista un deployment. No se ejecutaron la descarga, la ingesta externa, la compilación del backend ni el deploy desde esta copia. Para cerrar el recorrido reproducible desde cero todavía hacen falta los scripts/datos originales del backend y verificar la integración en Vercel.

Referencias oficiales: [sitios sin build](https://vercel.com/docs/builds/configure-a-build), [rewrites](https://vercel.com/docs/project-configuration/vercel-json#rewrites), [CLI](https://vercel.com/docs/cli/deploy), [runtime Python](https://vercel.com/docs/functions/runtimes/python), [límites de funciones](https://vercel.com/docs/functions/limitations).

## 5. Reloj ESP32 (opcional)

El firmware y las herramientas indicados en esta sección pertenecen al repositorio completo; la carpeta `hardware/` no viene en esta copia. Plataforma: NodeMCU ESP-32S, core ESP32 3.x y librería U8g2. Desde la raíz que contenga `hardware/`:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32:UploadSpeed=115200 hardware/reloj_esp32
```

| Pieza | Pin |
|---|---|
| OLED 1,3" SH1106 (I2C 0x3C) | SDA = GPIO22, SCL = GPIO21, VCC = 3V3 |
| Botón SOS (a GND, pull-up a 3V3) | GPIO25 |
| LED RGB cátodo común (220–330 Ω por color) | R = GPIO27, G = GPIO26, B = GPIO14 |

Por USB, el servidor elige el primer `/dev/ttyUSB*`; para indicar otro puerto agregá `--tty /dev/ttyUSB0` al comando de arranque. Solo la última pantalla de astronauta que abrió la página controla el reloj.

Por Wi-Fi (USB solo para alimentación):

1. Sin Wi-Fi guardado, el reloj abre `MarsEVA-Reloj-XXXX`. Conectate con el celular, abrí `http://192.168.4.1` y guardá la red; la contraseña queda en el ESP32.
2. El reloj debe estar en la misma red que la máquina del backend. Se anuncia cada 2 s por UDP 4210 (`$H,reloj`); el servidor aprende su IP y le envía las tramas.
3. Si hay firewall, permití UDP 4210 **solo desde esa red local**.
4. Para borrar la configuración Wi-Fi, mantené el botón SOS mientras encendés el reloj.

El SOS de la tablet se activa manteniendo 1,5 s y se cancela manteniendo nuevamente. La base puede confirmar recepción y cerrar la emergencia. El cajón «Simulación» permite probar velocidad, tormenta, radiación, corte de enlace y SOS.

LED: verde = normal; amarillo = sin enlace; rojo parpadeante = peligro; rojo/azul = SOS; magenta = recepción confirmada por la base; azul respirando = esperando datos. El arranque muestra el logo ARES, también reproducido por la pantalla simulada.

Herramientas del firmware: `diagnostico_pines`, `calibrar_led`, `prueba_led` y `escaner_i2c`.

Protocolo hacia el reloj:

```text
$W,<rumbo|-1>,<az>,<dist_m>,<o2%>,<bat%>,<ttr_s>,<dosis_uSv_x10>,<flags>,<texto>\n
```

Botones del reloj: `$B,SOS` / `$B,CANCEL`. Por Wi-Fi se envían por UDP 4210; el reloj repite cada botón tres veces y el servidor descarta duplicados cercanos.
