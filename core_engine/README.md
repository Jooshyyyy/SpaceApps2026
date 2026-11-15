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
penalty, and `255` is impassable. `radiation.bin` is optional. If absent,
radiation-aware routing is unavailable and the output says
`"radiation_available": false`; no radiation grid is invented.

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
- `--radiation-weight N`: exposure weight, default `0`; it has an effect only
  when `radiation.bin` exists.
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

On the frozen 1000 x 1000 Lower Mount Sharp AOI, routing from `(500,500)` to
`(600,650)` with default weights found a 152-cell path of `191.606945` m, with
`31.857422` m ascent and `65,630` expanded nodes. Five fresh CLI executions
measured A* itself (not grid loading) at `14.930533`, `13.281220`, `17.413200`,
`13.401637` and `14.526210` ms: minimum `13.281220`, median `14.526210`, maximum
`17.413200` ms. These are observations on this development host, not a latency
guarantee.

The matching round-trip simulation was feasible: outbound `265.838625` s,
inbound/TTR `265.584117` s, oxygen remaining at the objective `1.146264` kg,
battery remaining `237.775920` Wh, total dose `0.004429` mSv and alert mask `0`.
The real AOI has no radiation layer, so radiation-aware routing was not used.
