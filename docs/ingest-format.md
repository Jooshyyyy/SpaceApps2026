# Layer 1 ingestion contract

`scripts/ingest.py` turns a window of the Gale DEM into four files in one output
directory. The C++ engine reads only these files; it never reads the source
GeoTIFF. This document is the contract between layer 1 (Python) and layer 2
(C++). If the two disagree, the engine is wrong.

## Output directory

```
<out>/
  elevation.bin   float32, little-endian, row-major, metres
  slope.bin       float32, little-endian, row-major, degrees
  cost.bin        uint8,   row-major, A* penalty
  aoi.json        metadata, grid shape, georeferencing, statistics
```

All three grids share one shape: `rows = aoi.rows`, `cols = aoi.cols`, and cell
`(r, c)` sits at byte offset `(r * cols + c) * sizeof(T)`. Row 0 is the northern
edge. Nothing is padded, compressed, or interleaved.

## aoi.json

```json
{
  "format": "mars-eva-aoi/1",
  "name": "lower-mount-sharp-nw-flank",
  "source": {
    "path": "Gale_DEM_SMG_1m.tif",
    "bytes": 5400000000,
    "width": 36000,
    "height": 40100,
    "bigtiff": true,
    "rows_per_strip": 1,
    "nodata": -3.4028234663852886e+38,
    "sha256": "…"
  },
  "crs": {
    "name": "GCS_Mars_2000",
    "ellipsoid": "Mars_2000_IAU_IAG",
    "equatorial_radius_m": 3396190.0
  },
  "aoi": {
    "row0": 24000, "col0": 28000, "rows": 1000, "cols": 1000,
    "bbox_lonlat": { "west": …, "south": …, "east": …, "north": … },
    "cell_size_m": { "dy": …, "dx": … }
  },
  "grids": { "elevation": {…}, "slope": {…}, "cost": {…} },
  "policy": { "max_slope_deg": 20.0, "cost_rule": "…" },
  "stats": { … }
}
```

Fields the engine must honour:

- `aoi.rows`, `aoi.cols` — the grid shape. Never infer it from the file size.
- `aoi.cell_size_m.dy`, `aoi.cell_size_m.dx` — metric cell size at the AOI's
  middle row. A* move costs must use these, not the geographic pixel size.
- `aoi.bbox_lonlat` — the AOI's outer corner in degrees, for placing markers.
- `grids.*.dtype` — the element type. `float32` for elevation and slope,
  `uint8` for cost.
- `policy.max_slope_deg` — the threshold used to build the cost grid. A* must
  agree with it, or the engine will route through cells layer 1 called
  impassable.

## Elevation

`float32`, metres above the Mars 2000 reference. `nodata` from `aoi.json`
(-3.4028234663852886e+38) marks a void. A void is not zero metres: it is "no
measurement". The engine must never treat it as ground.

## Slope

`float32`, degrees, computed with central differences in the interior and
one-sided differences at the border. A no-data cell is `NaN`, never `0.0`.
`NaN` means "unknown"; do not compare it with `>` or `<` without a finiteness
check first.

## Cost

`uint8`, one byte per cell:

| value | meaning |
|---|---|
| `0` | no data |
| `1` | flattest traversable cell |
| `2..254` | traversable, rising penalty |
| `255` | impassable: slope above `policy.max_slope_deg` |

The mapping is `cost = 1 + round(253 * clip(slope, 0, max_slope) / max_slope)`,
clamped to `1..254`. Values never wrap: a cell either stays traversable or
saturates at 255. A* must treat `255` as a hard wall and `0` as unknown ground,
not as cheap ground.

## Reading it in C++

```cpp
std::ifstream in(dir + "/elevation.bin", std::ios::binary);
std::vector<float> elevation(rows * cols);
in.read(reinterpret_cast<char*>(elevation.data()), elevation.size() * sizeof(float));
```

The host is little-endian; if you ever target a big-endian host, byte-swap the
float32 grids and leave the uint8 grid alone. Read `aoi.json` first and check
`format == "mars-eva-aoi/1"`; refuse anything else instead of guessing.

## Regenerating

```bash
python3 scripts/ingest.py \
  --tif Gale_DEM_SMG_1m.tif \
  --out data/processed \
  --row 24000 --col 28000 --size 1000 \
  --name lower-mount-sharp-nw-flank
```

`--info` prints the source raster's layout without writing anything. The
artifacts are deterministic for the same window and threshold; the source
SHA-256 in `aoi.json` proves which DEM produced them.
