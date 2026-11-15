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
- **Science on every stop: the 2025 boxwork campaign.** The four stops are real
  places Curiosity visited in May–July 2025 while it explored the *boxwork*
  formations of Mount Sharp. Each stop shows a science goal based on NASA's
  Curiosity mission blog for that sol. The goals cover honeycomb fractures, the
  boxwork ridges that may record ancient water flow, fractured blocks next to a
  frost-trap sand patch, and a ridge overlook. The astronaut can also pick any
  point on the map, even mid-EVA, and the route is recomputed from where they
  stand.
- **Know what you'll see before you get there.** For each stop, a scrollable
  360° panorama stitched from 12 raw Navcam images Curiosity took at that exact
  position sits next to the HiRISE orbital view.
- **Layered NASA data on the map.** Toggle terrain slope, radiation exposure
  (sheltered vs. open sky) and loose-dust risk (thermal inertia) on the map. An
  *Environment* card shows the RAD dose rate, REMS pressure and temperature,
  TES ground type, and the values at the astronaut's current position.
- **2D and 3D views.** HiRISE imagery with hazard overlays, plus a 3D terrain
  view at true 1:1 elevation that you can fly around freely, like Google Earth.
- **Works without internet on the tablet.** The laptop serves the map libraries
  and a cache of the HiRISE map tiles over the USB cable. A habitat crew far
  from Earth can't rely on the cloud either.

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

The backend has no third-party dependencies. The ingest step reads the 5.4 GB
BigTIFF directly, without GDAL or rasterio. The engine and the bus are plain
C++20 built with Makefiles. The server and the watch bridge use only the Python
standard library. The web interface uses Leaflet 1.9.4 (2D map) and three.js
0.160 (3D terrain), bundled in `prototipo/vendor/` so the tablet works offline.

## Data sources

| Data | Kind | Source | Time frame | Used for |
|---|---|---|---|---|
| HiRISE DEM of Gale crater, 1 m/pixel (`Gale_DEM_SMG_1m.tif`) | Topographic | NASA / USGS | — | Elevation, slope, routing cost, 3D terrain |
| HiRISE orbital mosaic (via JPL Mars Trek) | Optical | NASA / JPL / University of Arizona | — | Map background, 3D texture, orbital target view |
| MSL RAD, sol 805 | Radiation | NASA PDS (Curiosity) | 2014 | Calibrated dose baseline (≈33 µSv/h) combined with a sky-view model of terrain shielding |
| MSL REMS, sol 1 | Atmospheric | NASA PDS (Curiosity) | 2012 | Ambient pressure (750 Pa) and temperature |
| MGS TES thermal inertia (Putzig & Mellon 2007) | Thermal | NASA PDS | — | Loose-dust (sinkage) risk and walking drag |
| Curiosity traverse waypoints | Mission | NASA / JPL | May–Jul 2025 | Real stops on the boxwork campaign |
| Curiosity raw Navcam images | Surface imagery | NASA / JPL-Caltech raw images | sols 4530, 4544, 4582, 4612 | 360° "what you'll see" panorama at each stop |
| Curiosity mission blog | Science context | NASA Science | sols 4529–4613 | Science goal at each stop |

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

The tablet does not need internet. The server delivers the map libraries from
`prototipo/vendor/` and the HiRISE map tiles through `/tiles/…`. Tiles are
downloaded from Mars Trek once and kept in `prototipo/tile_cache/`, so open the
page once on a connected computer before going offline.

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
| `prototipo/pov/` | 360° Navcam panoramas for each stop (stitched from Curiosity raw images) |
| `prototipo/vendor/` | Leaflet and three.js, served locally for offline use |
| `hardware/` | ESP32 watch firmware and wiring test sketches |
| `Dockerfile` | Public visitor deployment |

## Honest limitations

- Oxygen, battery, dose and time-to-return are **model outputs**. They come from
  tunable PLSS assumptions, not from real suit sensors.
- Routing covers one 1 km × 1 km area of interest. Radiation is a calibrated
  surface model, not a live measurement.
- Dust-storm and radiation-peak alerts are triggered from the simulation drawer
  or by Mission Control for the demo. They are not a live space-weather feed.
- Science goals summarize what Curiosity's team did at those places, from the
  public mission blog. The panoramas are raw, uncalibrated Navcam frames joined
  side by side, so exposure changes from frame to frame.
- The full astronaut / watch / Mission Control loop has been exercised on our
  own hardware during development. It has not been validated as a flight-ready
  or field-tested system.

## References for the science stops

- Sols 4529–4531, "Honeycombs and Waffles… on Mars!": https://science.nasa.gov/blog/sols-4529-4531-honeycombs-and-waffles-on-mars/
- Sol 4546, "Martian Jenga" (covers the sol 4544 plan): https://science.nasa.gov/blog/sol-4546-martian-jenga/
- Sols 4582–4583, "A Rock and a Sand Patch": https://science.nasa.gov/blog/curiosity-blog-sols-4582-4583-a-rrock-and-a-sand-patch/
- Sols 4611–4613, "Scenic Overlook": https://science.nasa.gov/blog/curiosity-blog-sols-4611-4613-scenic-overlook/
- Raw images: NASA/JPL-Caltech, Mars Science Laboratory raw image archive.

## Acknowledgements

Built with open data from NASA (PDS, HiRISE, Curiosity MSL RAD/REMS, MGS TES)
and USGS Astrogeology, for the NASA Space Apps Challenge 2026.
