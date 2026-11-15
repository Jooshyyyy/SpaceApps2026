# Mars EVA core engine — layer 2

Native C++20 routing and life-support simulation over the frozen layer 1
artifacts. It has no third-party dependencies and builds with a plain Makefile.

## Build and test

```sh
make
make tests
```

The tests cover the JSON/grid contract, multicriteria A*, PLSS accounting and
the command-line interface. They use synthetic grids; real-AOI measurements are
reported separately, never presented as unit-test fixtures.

## Input contract

`--dir` must contain `aoi.json`, `elevation.bin`, `slope.bin` and `cost.bin` in
the `mars-eva-aoi/1` format defined by `../docs/ingest-format.md`. The engine
never reads the source GeoTIFF. It rejects unknown format tags, invalid grid
shapes and binary files whose exact byte count disagrees with the metadata.

Cost `0` is unknown ground, `1..254` is traversable with increasing terrain
penalty, and `255` is impassable. `radiation.bin` and `thermal_inertia.bin` are
optional. When present, routing accounts for modeled exposure and loose-soil
drag. The PLSS uses the calibrated RAD baseline recorded in `aoi.json`. Missing
layers are reported explicitly; the engine never invents them.

## Commands

```sh
# Inspect the AOI and available layers.
./eva_engine info --dir ../data/processed

# Route from refuge/start to objective/goal.
./eva_engine route --dir ../data/processed --start 500,500 --goal 600,650

# Simulate the outbound leg and an independently routed return leg.
./eva_engine sim --dir ../data/processed --start 500,500 --goal 600,650
```

Route and simulation options:

- `--slope-weight N`: terrain penalty scale, default `4`.
- `--climb-weight N`: additional cost per uphill metre, default `5`.
- `--radiation-weight N`: exposure weight, default `1`; it has an effect only
  when `radiation.bin` exists.
- `--soil-weight N`: loose-regolith penalty, default `1`; it has an effect only
  when `thermal_inertia.bin` exists.
- `--orthogonal`: disable diagonal steps.
- `--allow-corner-cutting`: permit diagonals whose two adjacent cells are not
  both traversable. The safe default forbids this.
- `sim --o2-capacity KG` and `sim --battery-capacity WH`: override initial
  consumables.
- `sim --path`: include both ordered paths. `route` always includes its path.

Successful commands emit one JSON object on stdout. Invalid arguments or input
artifacts emit `{"ok":false,"error":"..."}` on stderr and exit `2`. A valid
query with no route exits `0` with `"found": false`; a simulation without both
legs has `"return_feasible": false` and raises `EVA_ABORT`.

## PLSS assumptions and alerts

The default PLSS values in `include/engine/plss.hpp` are explicitly **tunable
simulation assumptions**, not measured suit hardware specifications. Oxygen,
battery and dose are integrated per step using distance, local slope and walking
speed. `ttr_s` is the simulated time for the inbound leg. Remaining oxygen and
battery are reported at the objective, after the outbound leg.

The engine owns alert decisions. It computes `RAD_PEAK`, `O2_LOW`, `BATT_LOW`
and `EVA_ABORT`; clients render the numeric bitmask and names without deriving
their own alerts. `STORM`, `COMMS_LOST` and `ROUTE_STALE` belong to layer 3
events and are not inferred here.

## Observed real-AOI run

On the 1000 x 1000 Curiosity 2025 EVA site, routing from `(500,500)` to
`(550,550)` with all environment layers found a `71.17` m path. Mean thermal
inertia along it was `137.13` SI and the calibrated round-trip dose was
`0.00314` mSv. The PLSS projection remained feasible with `1.1842` kg oxygen
and `246.32` Wh at the objective. These are observations on this development
host and dataset, not performance or hardware guarantees.
