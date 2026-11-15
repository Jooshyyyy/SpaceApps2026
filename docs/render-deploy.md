# Public web + ESP32 WSS on Render

Render serves the public web and `/reloj/ws` on one HTTPS port. The ESP32
initiates an outbound WSS connection; no public UDP port, USB connection to
Render, or router port-forwarding is required. Render terminates public TLS.
The native `eva_bus` remains private on loopback.

## Quick path

1. Push the reviewed changes to your own repository, then create a Render
   Blueprint from `render.yaml` (publishing is a separate action).
2. Render builds the Docker image and runs the service on `0.0.0.0:$PORT`.
   Open `https://YOUR-SERVICE.onrender.com/app.html`, or `/base` for the base.
3. Read the generated `WATCH_TOKEN` in Render's environment settings. Copy it
   **only** to the ESP32's ignored `watch_config.h`, never the frontend or URL.
   Set `WATCH_HOST` to the hostname without `https://` or a path, and configure
   the trusted root certificate. Follow [the firmware instructions](../hardware/reloj_esp32/README.md).
4. Compile/flash the ESP32 using those instructions. After WiFi and NTP sync,
   it connects to `wss://YOUR-SERVICE.onrender.com/reloj/ws`.
5. `GET /estado` should report `cloud: true`, `ws: true`, `open: true`.
   Open the astronaut page to generate `$W` frames. Its tab must remain open:
   the current app, not an autonomous background mission, produces watch data.
   Holding the watch button produces an event at `/reloj?since=0`.

**The default Git checkout does not include terrain binary layers.** This first
path verifies public web + watch transport using the precomputed mission, not
the complete live route engine. Missing live layers return 404; missing live
bus/planner returns 503. Prepare the layers separately as described below.

## Public access means public controls

The web and its existing HTTP controls are deliberately unauthenticated, as
requested. Any visitor can claim the current astronaut tab, change the shared
simulation, send watch frames or trigger simulated orders. The device secret
only protects joining `/reloj/ws`; it does **not** authorize web operators.
This is a shared demonstration, not an operational emergency/safety system.
Use one service instance and one watch. State/events are in memory and reset
on restart. Do not horizontally scale this implementation.

Never include `WATCH_TOKEN` in a URL, HTML, mission JSON, screenshot or git
commit. Rotate it in Render and reflash the watch if exposed. A second
simultaneous device is rejected with 409. Browser `Origin` is rejected on the
device endpoint, while the ordinary web continues to work without a login.

## Separate data preparation from startup

Only these small processed artifacts are needed by the running app:

| Artifact | Purpose |
|---|---|
| `data/processed/aoi.json` | Grid metadata matching the selected mission |
| `elevation.bin`, `slope.bin`, `cost.bin` | Terrain, slope and traversability |
| `environment.json` | Environmental defaults |
| `radiation.bin`, `thermal_inertia.bin` | Optional layers when declared in AOI metadata |

The current 1000×1000 terrain/slope/cost grids total about 9 MB; with the two
optional grids, about 17 MB. The source DEM is about 5.8 GB. Do not load the
source into the web request path or download/ingest it on every restart.

### Option A — prepare elsewhere, deploy the processed image

Run the existing pipeline on a machine with the original Gale DEM:

```bash
python3 -m venv .venv
.venv/bin/pip install numpy==2.2.6 requests==2.32.5
.venv/bin/python scripts/ingest.py \
  --tif data/raw/Gale_DEM_SMG_1m.tif --out data/processed
make -C core_engine
./core_engine/eva_engine info --dir data/processed
```

Use the existing project's raw-processing pipeline for optional radiation and
thermal grids if your AOI references them. Keep AOI/mission coordinates
consistent; do not replace the source with a different region or an `.IMG`
product just because a downloader catalog calls it `gale_dtm`.

Build `docker build -t mars-eva .` **after** preparation: `.dockerignore` excludes
raw data and local executables, but intentionally includes processed `.bin`
files. The local image therefore contains your prepared layers. They are
ignored by Git, so a Render **Git-based build will not receive your local
layers**. To use this option on Render, separately publish that prepared image
to your registry and deploy from the image; registry publishing is not done by
these files. A clean Git-based Blueprint build starts in transport/demo mode.

### Option B — download and ingest on Render once (paid disk required)

This is a manual, opt-in paid setup, **not** provisioned by `render.yaml`.
Render's free service has neither a persistent disk nor the shell needed for
this preparation. First choose a paid service with sufficient disk/compute,
attach a persistent disk at `/app/data` (at least 10 GB for this source plus
working files), and keep one instance. Disk changes interrupt the service;
Render does not make that disk available during image build or one-off jobs.

From the paid service's Shell, explicitly prepare data under that mount:

```bash
mkdir -p /app/data/raw /app/data/processed
# Set DEM_URL to the original Gale_DEM_SMG_1m.tif source URL you trust.
# Do not paste credentials into shell history; use a Render secret if needed.
python3 - <<'PY'
import os
import shutil
import urllib.request
from pathlib import Path
url = os.environ['DEM_URL']
if not url.startswith('https://'):
    raise SystemExit('DEM_URL must use HTTPS')
path = Path('/app/data/raw/Gale_DEM_SMG_1m.tif')
with urllib.request.urlopen(url, timeout=60) as source, path.open('wb') as target:
    shutil.copyfileobj(source, target, length=1024 * 1024)
PY
# Expected checksum is for the source recorded in this repository's aoi.json.
echo 'd85e36126a366d68339d571b474319aad85d8ea32cc157bd8e3d330b036edd68  /app/data/raw/Gale_DEM_SMG_1m.tif' | sha256sum -c -
# Continue only if the checksum passes.
python3 scripts/ingest.py \
  --tif /app/data/raw/Gale_DEM_SMG_1m.tif --out /app/data/processed
./core_engine/eva_engine info --dir /app/data/processed
```

Complete any optional environmental processing before restarting the service.
If the disk hides `environment.json` from the image, supply the environment
metadata generated for the AOI as well. Restart only after all required layers
are ready: the server detects backend availability at startup. Do not prepare
or overwrite layers while the live engine uses them. Preserve a known-good
processed set before replacing it; migrations/replacement are not automated.

The original source URL and bytes are not supplied by this feature. No heavy
download, ingestion run, paid resource or real deployment was executed here.

## Local container verification

```bash
docker build -t mars-eva .
# Generate a secret; don't reuse this test-only value publicly.
docker run --rm -p 10000:10000 -e WATCH_TOKEN=local-test-only mars-eva
curl -f http://127.0.0.1:10000/estado
```

Automated container test (uses its own ephemeral port and stops only its own
container):

```bash
WATCH_TEST_IMAGE=mars-eva:ws-check python3 -m unittest discover \
  -s prototipo/tests -p test_render_container.py -v
```

Build with `docker build -t mars-eva:ws-check .` first. This smoke test has
passed locally: public web, authenticated upgrade, byte-identical `$W` frame,
SOS event through HTTP, native executables present and raw sources excluded.

Cloud mode deliberately opens no UDP socket or serial device. Ordinary local
USB mode is still available via `python3 prototipo/servidor.py`; the ESP32's old
UDP discovery was replaced by WSS, so its local fallback is now USB.

## Availability and limitations

- Free Render instances can sleep: the watch must reconnect and may miss
  button events while offline. Offline events are not queued or replayed.
- Deploys/restarts close WebSockets. Firmware uses heartbeat and bounded
  reconnect delay; TLS requires correct host, CA and NTP time.
- Missing a watch is not an unhealthy service: `/estado` remains 200 with
  `ws: false`. Check `/bus` separately for live routing availability.
- The source download, real Render TLS/upgrade and physical display/button
  behavior still need verification in the target environment.

References: [Render WebSockets](https://render.com/docs/websocket),
[Docker](https://render.com/docs/docker),
[persistent disks](https://render.com/docs/disks),
[device protocol](watch-websocket.md).
