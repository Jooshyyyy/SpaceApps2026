# Layer 2 — core engine (multicriteria A* + PLSS)

Route: inline C++20 under `core_engine/`. No git operations. No new third-party
dependencies: g++ 16 is present, cmake is not, so the build is a plain Makefile.

## Specs

S1. Layer 2 is the native C++ core engine: multicriteria A* over the layer 1
cost grid plus a PLSS life-support simulator. It consumes only
`data/processed/{elevation,slope,cost}.bin` and `aoi.json`, never the source
GeoTIFF. User: "para ya con la parte de la ingesta, ingresa ya a aplicar la
capa 2 del motor".

S2. No git work: "deja de complicarte con el git, no hay nada y punto". No
commits, no branches, no `.gitignore` edits.

S3. The engine is the single source of truth for alert flags; clients only
render them. Alert bits follow the frozen frame contract: bit0 STORM, bit1
RAD_PEAK, bit2 O2_LOW, bit3 BATT_LOW, bit4 COMMS_LOST, bit5 ROUTE_STALE,
bit6 EVA_ABORT. Layer 2 computes O2_LOW, BATT_LOW, RAD_PEAK and EVA_ABORT;
STORM, COMMS_LOST and ROUTE_STALE arrive from layer 3 events.

S4. No fabricated data. Radiation-aware routing exists only when layer 1 emits
`radiation.bin`; without it the engine reports radiation unavailable and routes
on distance and slope. A* latency is measured and reported, never claimed.

S5. Layer 1 stays frozen. The engine reads `aoi.json` and refuses any format
other than `mars-eva-aoi/1`. Grid shape comes from the metadata, never from the
file size.

## Tasks

| ID | Task | Specs | Route | Status |
|----|------|-------|-------|--------|
| T1 | Minimal JSON parser + grid loader with validation | S1, S5 | inline | done |
| T2 | Multicriteria A*: 8-connectivity, walls, no corner cutting | S1, S4 | inline | done |
| T3 | PLSS simulator: O2, battery, dose, time-to-return, alert flags | S1, S3 | inline | done |
| T4 | CLI: `info`, `route`, `sim` with JSON output | S1, S4 | inline | done |
| T5 | Measure A* latency on the real AOI and report actual numbers | S4 | inline | done |

## Log

L1. "para ya con la parte de la ingesta, ingresa ya a aplicar la capa 2 del
motor"

L2. "deja de complicarte con el git, no hay nada y punto"

L3. "ahora contiua"

L4. "continua"

L5. Verification: clean build with no warnings; 66/66 C++ checks and 7/7 CLI
end-to-end tests pass. Real AOI route `(500,500) -> (600,650)`: found, 152
cells, 191.606945 m, 31.857422 m ascent, 65,630 expanded nodes. Five measured
A* runs: 14.930533, 13.281220, 17.413200, 13.401637, 14.526210 ms (median
14.526210 ms). Round trip feasible, TTR 265.584117 s, alert mask 0. No
`radiation.bin`; output reports radiation unavailable. No git operations.
