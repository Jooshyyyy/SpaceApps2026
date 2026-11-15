# ARES · Mars Navigation Map

**NASA Space Apps Challenge 2026 — "Interplanetary Survival Guide: Martian Map"**

ARES is a navigation system for an astronaut on a surface EVA (extravehicular
activity) in Gale crater, Mars. It turns public NASA/USGS data into safe walking
routes. While the astronaut moves, it tracks oxygen, battery, radiation dose and
time-to-return, and it keeps the astronaut, their wrist display and Mission
Control in sync.

> **Live demo (visitor view):** https://projectares-omtx.onrender.com/app.html
> Scan the QR code at our table to follow the live EVA from your phone. The
> astronaut tablet is driven locally by the team. Visitors watch it in real time
> but can't control it.

---

## The problem

On Mars there is no GPS, no rescue team close by, and the terrain can kill: steep
slopes, loose dust that swallows boots, radiation, and dust storms. An astronaut
needs answers to three questions at all times:

1. **Where can I walk safely?**
2. **Do I have enough oxygen and power to get back?**
3. **What do I do if something goes wrong?**

ARES answers those questions with a map, a route and a wrist screen that the
astronaut can read at a glance.

## What it does

- **Safe routing on real terrain.** A multicriteria A\* search runs over a 1 m
  per pixel HiRISE elevation model of the Curiosity 2025 EVA site. It avoids
  slopes above 20° and penalizes climbs, radiation exposure and loose regolith.
- **Life-support projection (PLSS).** Every route is simulated in both
  directions, out and back. Oxygen, battery, radiation dose and time-to-return
  are tracked as the astronaut walks, and the EVA is flagged for abort if the
  return trip is no longer feasible.
- **Contingencies.** Dust storms, radiation peaks, low O₂, low battery and loss
  of the habitat link each have a distinct alarm sound. An emergency
  replans the route straight back to the habitat.
- **SOS** from the tablet or the watch: hold for 1.5 s to send, hold again to
  cancel. Mission Control sees it at once and acknowledges it.
- **Voice announcements.** Short spoken cues at key moments, e.g. "Welcome,
  astronaut. Select your destination", "Halfway there", "You are almost there",
  "Heading back to the habitat". Each cue has several variants and a cooldown,
  so the voice never nags.
- **Science targets.** The stops are real Curiosity 2025 waypoints from NASA.
  The astronaut can also pick any point on the map, even mid-EVA, and the route
  is recomputed from where they stand.
- **2D and 3D views.** A slope-hazard overlay on HiRISE imagery, plus a 3D
  terrain view at true 1:1 elevation.

## The three screens

| Screen | Who uses it | What it shows |
|---|---|---|
| **Astronaut tablet** | The astronaut | Map, route, life support, stops, SOS. The panel can be collapsed with the small arrow on top. |
| **ARES Watch** (ESP32 + 1.3" OLED) | The astronaut's wrist | Heading arrow, distance, O₂, battery, dose, return time, alerts, status LED, SOS button. |
| **Mission Control** | The habitat / base | Live astronaut status and position, return orders, SOS acknowledgement, mission log. |

The public link (visitor mode) shows the astronaut tablet mirrored live, full
screen, with no controls.

## Architecture

```
NASA / USGS public data
        │
        ▼
Layer 1 · Python ingest (scripts/)          → data/processed/*.bin + aoi.json + environment.json
        │
        ▼
Layer 2 · C++20 engine (core_engine/)       → A* routing + PLSS simulation + alerts (JSON)
        │
        ▼
Layer 3 · C++20 Edge Bus (communication/)   → WebSocket: route updates + 10 Hz binary telemetry
        │
        ▼
prototipo/servidor.py (Python, stdlib only) → tablet page, Mission Control, watch link (Bluetooth/USB/WiFi)
        │                                         │
        ▼                                         ▼
Astronaut tablet (browser) ◄──────────► ARES Watch (ESP32)
        │
        └── live state mirror ──► Render (public visitor view)
```

None of the layers uses a third-party library. The ingest step reads the 5.4 GB
BigTIFF directly with no GDAL or rasterio. The engine and the bus are plain C++20
built with Makefiles. The server and the watch bridge use only the Python
standard library.

## Data sources

| Data | Source | Used for |
|---|---|---|
| HiRISE DEM of Gale crater, 1 m/pixel (`Gale_DEM_SMG_1m.tif`) | NASA / USGS | Elevation and slope for the 1 km × 1 km area of interest |
| MSL RAD, Sol 805 | NASA PDS (Curiosity) | Calibrated radiation baseline (≈0.033 mSv/h) |
| MSL REMS, Sol 1 | NASA PDS (Curiosity) | Ambient pressure and temperature |
| MGS TES thermal inertia | NASA PDS | Loose-soil (sinkage) risk |
| Curiosity 2025 traverse waypoints | NASA | Real science stops |
| HiRISE orbital imagery | NASA / University of Arizona | Map background and target views |

Area of interest: `curiosity-2025-eva-site`, 1000 × 1000 cells, about
137.374–137.391° E and 4.792–4.809° S.

## Run it locally

Requirements: Linux, `g++` with C++20, `make`, Python 3.10+.

```sh
# 1. Build the engine and the bus
make -C core_engine
make -C communication

# 2. (Optional) regenerate the processed layers. They are already committed in this branch.
python3 scripts/ingest.py --help             # needs the 5.4 GB Gale DEM
python3 scripts/fetch_mars_data.py --only rad_sample tes_thermal rems_pressure_sample
python3 scripts/process_raw_data.py

# 3. Start the server
cd prototipo
python3 -I servidor.py --port 8791
```

Then open:

- Astronaut tablet: http://127.0.0.1:8791/app.html
- Mission Control: http://127.0.0.1:8791/base
- Android tablet over USB: `adb reverse tcp:8791 tcp:8791`, then open the same
  URL on the tablet.

Only one astronaut page should be open at a time. That screen drives the watch.

### Public visitor view (how our demo runs)

The cloud copy runs from the `Dockerfile` in public, read-only mode
(`--publico --sin-hardware`). The laptop running the real demo mirrors its live
state to it:

```sh
python3 -I servidor.py --port 8791 --espejo https://<your-app>.onrender.com
```

Environment variables for the cloud service: `OPERADOR_TOKEN` and `RELOJ_TOKEN`.
Each is a random secret of at least 20 alphanumeric characters, and the laptop
uses the same values. Visitors can only watch; every write is rejected.

## ARES Watch (hardware)

NodeMCU ESP-32S, 1.3" SH1106 OLED and an RGB status LED, powered by a 3.7 V
Li-ion cell through a boost module. The firmware is in `hardware/reloj_esp32/`.

| Part | Pin |
|---|---|
| OLED SH1106 (I²C 0x3C) | SDA = GPIO22, SCL = GPIO21 |
| SOS button (to GND, pull-up) | GPIO25 |
| RGB LED, common cathode | R = GPIO27, G = GPIO26, B = GPIO14 |

```sh
arduino-cli compile --fqbn esp32:esp32:esp32:UploadSpeed=115200,PartitionScheme=huge_app hardware/reloj_esp32
```

- **Links.** It uses Bluetooth Classic as `ARES-Watch` and the server connects to
  it automatically. USB serial and WiFi also work.
- **Protocol.** The tablet sends `$W,<bearing>,<az>,<dist_m>,<o2%>,<bat%>,<ttr_s>,<dose_x10>,<flags>,<text>`
  to the watch. The watch sends `$B,SOS` or `$B,CANCEL` back.
- **LED.** Green = all good · yellow = no link · blinking red = danger ·
  red/blue = SOS · magenta = Mission Control acknowledged.

## Repository map

| Path | Contents |
|---|---|
| `scripts/` | Layer 1: DEM ingest, NASA data download and processing, plus tests |
| `data/processed/` | Area-of-interest grids (elevation, slope, cost, radiation, thermal inertia) and metadata |
| `core_engine/` | Layer 2: routing and PLSS engine (`eva_engine`), see its README |
| `communication/` | Layer 3: WebSocket Edge Bus (`eva_bus`), see its README |
| `docs/` | Frozen formats: `ingest-format.md`, `telemetry-frame.md` |
| `prototipo/` | Tablet / Mission Control web app, server, watch bridge, mission generator |
| `hardware/` | ESP32 watch firmware and wiring test sketches |
| `Dockerfile` | Public visitor deployment |

## Honest limitations

- Oxygen, battery, dose and time-to-return are **model outputs**. They come from
  tunable PLSS assumptions, not from real suit sensors.
- Routing covers one 1 km × 1 km area of interest. Radiation is a calibrated
  surface model, not a live measurement.
- The full astronaut / watch / Mission Control loop has been exercised on our
  own hardware during development. It has not been validated as a flight-ready
  or field-tested system.

## Acknowledgements

Built with open data from NASA (PDS, HiRISE, Curiosity MSL RAD/REMS, MGS TES)
and USGS Astrogeology, for the NASA Space Apps Challenge 2026.
