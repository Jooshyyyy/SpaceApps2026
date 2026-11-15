#!/usr/bin/env python3
"""Layer 1 ingestion: Gale DEM -> AOI elevation / slope / cost binaries.

Reads the raw NASA DEM GeoTIFF directly (no GDAL, no rasterio) and writes the
flat binaries consumed by the C++ core engine.

The source file is a BigTIFF, uncompressed, one row per strip. That layout lets
us seek to an exact pixel and read only the bytes we need, so cropping a 1 km x
1 km AOI out of a 5.4 GB raster costs ~4 MB of I/O instead of a full load.

Outputs (see docs/ingest-format.md for the frozen contract):

  elevation.bin   float32, row-major, metres, W*H values
  slope.bin       float32, row-major, degrees, W*H values
  cost.bin        uint8,   row-major, 0 invalid / 1..254 traversable / 255 impassable
  aoi.json        metadata: georeferencing, grid, statistics, source checksum

Run `python3 scripts/ingest.py --help` for the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# --- Physical constants and format constants -------------------------------

MARS_RADIUS_M = 3396190.0  # Mars 2000 IAU/IAG ellipsoid, equatorial radius
NODATA_SENTINEL = -3.4028234663852886e38  # float32 -FLT_MAX, GDAL's NODATA here
NODATA_THRESHOLD = -1e37  # anything below this is treated as "no data"

COST_INVALID = 0  # no data / outside the DEM
COST_IMPASSABLE = 255  # slope above the exclusion threshold
COST_MIN = 1  # flattest traversable cell
COST_MAX = 254  # steepest traversable cell
DEFAULT_MAX_SLOPE_DEG = 20.0


class TiffLayoutError(RuntimeError):
    """The file is a TIFF we cannot read row-wise; fail loudly, never guess."""


# --- Georeferencing --------------------------------------------------------


@dataclass(frozen=True)
class GeoRef:
    """Pixel -> lon/lat for a north-up geographic raster.

    Row 0 is the northern edge; the tiepoint names the outer corner of pixel
    (0, 0), so pixel (r, c) sits at lat0 - r * px_lat, lon0 + c * px_lon.
    """

    lon0: float
    lat0: float
    px_lon: float
    px_lat: float

    def pixel_to_lonlat(self, row: float, col: float) -> tuple[float, float]:
        return self.lat0 - row * self.px_lat, self.lon0 + col * self.px_lon

    def lonlat_to_pixel(self, lon: float, lat: float) -> tuple[float, float]:
        return (self.lat0 - lat) / self.px_lat, (lon - self.lon0) / self.px_lon

    def cell_size_m(self, row: float) -> tuple[float, float]:
        """Metric cell size at a given row: (dy, dx) in metres."""
        m_per_deg_lat = np.pi / 180.0 * MARS_RADIUS_M
        m_per_deg_lon = m_per_deg_lat * np.cos(np.radians(self.lat0 - row * self.px_lat))
        return self.px_lat * m_per_deg_lat, self.px_lon * m_per_deg_lon


# --- Raw TIFF reader -------------------------------------------------------

_TAG_WIDTH = 256
_TAG_HEIGHT = 257
_TAG_BITS = 258
_TAG_COMPRESSION = 259
_TAG_STRIP_OFFSETS = 273
_TAG_SAMPLES_PER_PIXEL = 277
_TAG_ROWS_PER_STRIP = 278
_TAG_STRIP_BYTE_COUNTS = 279
_TAG_PIXEL_SCALE = 33550
_TAG_TIEPOINT = 33922
_TAG_SAMPLE_FORMAT = 339

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8, 17: 8, 18: 8}


class TiffDEM:
    """Row-addressed reader for uncompressed single-band float32 GeoTIFFs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = open(self.path, "rb")
        try:
            self._read_header()
            self._read_tags()
            self._validate()
            self._read_strip_tables()
        except BaseException:
            # A rejected file must not leak its handle: the pipeline scans many
            # candidates and a raise here would otherwise strand every one.
            self._fh.close()
            raise

    # -- parsing ------------------------------------------------------------

    def _read_header(self) -> None:
        head = self._fh.read(16)
        if head[:2] == b"II":
            self._bo = "<"
        elif head[:2] == b"MM":
            self._bo = ">"
        else:
            raise TiffLayoutError("not a TIFF file: bad byte-order marker")
        magic = struct.unpack(self._bo + "H", head[2:4])[0]
        if magic == 43:
            self.is_bigtiff = True
            offset_size, _reserved, self._first_ifd = struct.unpack(self._bo + "HHQ", head[4:16])
            if offset_size != 8:
                raise TiffLayoutError(f"unsupported BigTIFF offset size {offset_size}")
        elif magic == 42:
            self.is_bigtiff = False
            self._first_ifd = struct.unpack(self._bo + "I", head[4:8])[0]
        else:
            raise TiffLayoutError(f"not a TIFF file: magic {magic}")

    def _read_tags(self) -> None:
        f = self._fh
        f.seek(self._first_ifd)
        if self.is_bigtiff:
            count = struct.unpack(self._bo + "Q", f.read(8))[0]
            entry_size, inline_size = 20, 8
        else:
            count = struct.unpack(self._bo + "H", f.read(2))[0]
            entry_size, inline_size = 12, 4
        self.tags: dict[int, tuple[int, int, int]] = {}
        for _ in range(count):
            raw = f.read(entry_size)
            if self.is_bigtiff:
                tag, typ, n = struct.unpack(self._bo + "HHQ", raw[:12])
                value = struct.unpack(self._bo + "Q", raw[12:20])[0]
            else:
                tag, typ, n = struct.unpack(self._bo + "HHI", raw[:8])
                value = struct.unpack(self._bo + "I", raw[8:12])[0]
            self.tags[tag] = (typ, n, value)
        self._inline_size = inline_size

    def _read_values(self, tag: int) -> tuple:
        """Return a tag's values, following the offset when they do not fit inline."""
        typ, count, value = self.tags[tag]
        size = _TYPE_SIZES[typ]
        total = size * count
        if total <= self._inline_size:
            if typ == 2:
                # ASCII values are stored raw in the field. Only reachable for
                # very short strings; longer ones take the offset path below.
                raw = struct.pack(self._bo + "Q", value)[:total]
            else:
                # Re-encode with the type's own format so the byte order is
                # correct for both little- and big-endian files.
                fmt = {1: "B", 3: "H", 4: "I", 12: "d", 16: "Q"}[typ]
                raw = struct.pack(self._bo + fmt, value)
        else:
            self._fh.seek(value)
            raw = self._fh.read(total)
        fmt = {1: "B", 2: "s", 3: "H", 4: "I", 12: "d", 16: "Q"}[typ]
        if typ == 2:
            return (raw,)
        return struct.unpack(self._bo + f"{count}{fmt}", raw)

    def _validate(self) -> None:
        t = self.tags
        for required in (_TAG_WIDTH, _TAG_HEIGHT, _TAG_STRIP_OFFSETS, _TAG_STRIP_BYTE_COUNTS):
            if required not in t:
                raise TiffLayoutError(f"missing required TIFF tag {required}")
        self.width = t[_TAG_WIDTH][2]
        self.height = t[_TAG_HEIGHT][2]
        bits = self._read_values(_TAG_BITS)[0]
        sample_format = self._read_values(_TAG_SAMPLE_FORMAT)[0] if _TAG_SAMPLE_FORMAT in t else 1
        compression = self._read_values(_TAG_COMPRESSION)[0] if _TAG_COMPRESSION in t else 1
        samples = self._read_values(_TAG_SAMPLES_PER_PIXEL)[0] if _TAG_SAMPLES_PER_PIXEL in t else 1
        if compression != 1:
            raise TiffLayoutError(f"compression {compression} unsupported; expected uncompressed (1)")
        if bits != 32 or sample_format != 3:
            raise TiffLayoutError(f"expected float32 samples, got bits={bits} sample_format={sample_format}")
        if samples != 1:
            raise TiffLayoutError(f"expected a single band, got {samples} samples per pixel")
        self.rows_per_strip = (
            self._read_values(_TAG_ROWS_PER_STRIP)[0] if _TAG_ROWS_PER_STRIP in t else self.height
        )
        self.nodata = (
            float(self._read_values(42113)[0].split(b"\x00")[0])
            if 42113 in t and isinstance(self._read_values(42113)[0], bytes)
            else NODATA_SENTINEL
        )
        self.georef = self._read_georef()

    def _read_georef(self) -> GeoRef | None:
        if _TAG_PIXEL_SCALE not in self.tags or _TAG_TIEPOINT not in self.tags:
            return None
        scale = self._read_values(_TAG_PIXEL_SCALE)
        tie = self._read_values(_TAG_TIEPOINT)
        # tiepoint: i, j, k, x, y, z  (pixel -> geographic for the outer corner)
        i, j = tie[0], tie[1]
        x, y = tie[3], tie[4]
        return GeoRef(
            lon0=x - i * scale[0],
            lat0=y + j * scale[1],
            px_lon=scale[0],
            px_lat=scale[1],
        )

    def _read_strip_tables(self) -> None:
        self.strip_offsets = np.asarray(self._read_values(_TAG_STRIP_OFFSETS), dtype=np.int64)
        self.strip_byte_counts = np.asarray(self._read_values(_TAG_STRIP_BYTE_COUNTS), dtype=np.int64)
        expected = -(-self.height // self.rows_per_strip)
        if len(self.strip_offsets) != expected:
            raise TiffLayoutError(
                f"strip count {len(self.strip_offsets)} does not match {expected} "
                f"({self.height} rows / {self.rows_per_strip} rows per strip)"
            )

    # -- access -------------------------------------------------------------

    def row_byte_offset(self, row: int) -> int:
        """Byte offset of the first pixel of `row` in the file."""
        strip = row // self.rows_per_strip
        within = row % self.rows_per_strip
        return int(self.strip_offsets[strip]) + within * self.width * 4

    def read_row(self, row: int, col0: int = 0, col1: int | None = None) -> np.ndarray:
        if not 0 <= row < self.height:
            raise IndexError(f"row {row} outside 0..{self.height - 1}")
        col1 = self.width if col1 is None else col1
        if not 0 <= col0 < col1 <= self.width:
            raise IndexError(f"column window {col0}..{col1} outside 0..{self.width}")
        n = col1 - col0
        self._fh.seek(self.row_byte_offset(row) + col0 * 4)
        raw = self._fh.read(n * 4)
        if len(raw) != n * 4:
            raise TiffLayoutError(f"short read on row {row}: {len(raw)} of {n * 4} bytes")
        return np.frombuffer(raw, dtype=self._bo + "f4")

    def read_window(self, row0: int, col0: int, rows: int, cols: int) -> np.ndarray:
        """Read a rectangle as a float32 array shaped (rows, cols), row-major."""
        if row0 < 0 or col0 < 0 or rows <= 0 or cols <= 0:
            raise ValueError("window must have non-negative origin and positive size")
        if row0 + rows > self.height or col0 + cols > self.width:
            raise ValueError("window exceeds raster bounds")
        out = np.empty((rows, cols), dtype=np.float32)
        for i in range(rows):
            out[i] = self.read_row(row0 + i, col0, col0 + cols)
        return out

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "TiffDEM":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- Terrain math ----------------------------------------------------------


def slope_degrees(elevation: np.ndarray, dy_m: float, dx_m: float) -> np.ndarray:
    """Surface slope in degrees from a metric elevation grid.

    Uses central differences in the interior and one-sided differences at the
    edges. No-data cells become NaN so they never masquerade as flat ground.
    """
    z = np.array(elevation, dtype=np.float64, copy=True)
    invalid = ~np.isfinite(z) | (z < NODATA_THRESHOLD)
    z[invalid] = np.nan
    gy, gx = np.gradient(z, dy_m, dx_m)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    # A no-data cell has no slope of its own: np.gradient would otherwise give
    # it a finite value from its valid neighbours, which is exactly the kind of
    # silent lie that turns a void into "flat, walkable ground".
    slope[invalid] = np.nan
    return slope


def cost_from_slope(slope: np.ndarray, max_slope_deg: float = DEFAULT_MAX_SLOPE_DEG) -> np.ndarray:
    """Quantise slope into an A*-friendly uint8 cost grid.

    0 = no data, 1..254 = traversable (1 flat, 254 at the threshold),
    255 = impassable. Values clamp; they never wrap.
    """
    if max_slope_deg <= 0:
        raise ValueError("max_slope_deg must be positive")
    s = np.asarray(slope, dtype=np.float64)
    cost = np.empty(s.shape, dtype=np.uint8)
    invalid = ~np.isfinite(s)
    steep = np.isfinite(s) & (s > max_slope_deg)
    ok = np.isfinite(s) & ~steep
    cost[invalid] = COST_INVALID
    cost[steep] = COST_IMPASSABLE
    scaled = COST_MIN + np.rint((COST_MAX - COST_MIN) * np.clip(s[ok], 0.0, max_slope_deg) / max_slope_deg)
    cost[ok] = np.clip(scaled, COST_MIN, COST_MAX).astype(np.uint8)
    return cost


# --- Pipeline --------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    row0: int
    col0: int
    rows: int
    cols: int


def resolve_window(
    dem: TiffDEM,
    row: int | None = None,
    col: int | None = None,
    size: int = 1000,
    bbox: tuple[float, float, float, float] | None = None,
) -> Window:
    """Turn either a pixel origin or a lon/lat bbox into a pixel window."""
    if bbox is not None:
        if dem.georef is None:
            raise TiffLayoutError("bbox requested but the raster has no georeferencing")
        lon_w, lat_s, lon_e, lat_n = bbox
        row0_f, col0_f = dem.georef.lonlat_to_pixel(lon_w, lat_n)
        row1_f, col1_f = dem.georef.lonlat_to_pixel(lon_e, lat_s)
        row0, col0 = int(round(row0_f)), int(round(col0_f))
        rows, cols = int(round(row1_f)) - row0, int(round(col1_f)) - col0
        if rows <= 0 or cols <= 0:
            raise ValueError(f"bbox {bbox} resolves to an empty window")
    else:
        row0 = 0 if row is None else row
        col0 = 0 if col is None else col
        rows = cols = size
    if row0 < 0 or col0 < 0 or row0 + rows > dem.height or col0 + cols > dem.width:
        raise ValueError(
            f"window row {row0} col {col0} size {rows}x{cols} exceeds raster {dem.height}x{dem.width}"
        )
    return Window(row0, col0, rows, cols)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def ingest(
    tif_path: Path,
    out_dir: Path,
    window: Window,
    max_slope_deg: float = DEFAULT_MAX_SLOPE_DEG,
    name: str | None = None,
    with_checksum: bool = True,
) -> dict:
    """Run the pipeline and return the manifest written to aoi.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with TiffDEM(tif_path) as dem:
        if dem.georef is None:
            raise TiffLayoutError("raster has no georeferencing; cannot place the AOI on Mars")
        elevation = dem.read_window(window.row0, window.col0, window.rows, window.cols)
        georef = dem.georef
        source = {
            "path": str(tif_path),
            "bytes": tif_path.stat().st_size,
            "width": dem.width,
            "height": dem.height,
            "bigtiff": dem.is_bigtiff,
            "rows_per_strip": dem.rows_per_strip,
            "nodata": dem.nodata,
            "sha256": sha256_file(tif_path) if with_checksum else None,
        }

    dy_m, dx_m = georef.cell_size_m(window.row0 + window.rows / 2)
    slope = slope_degrees(elevation, dy_m, dx_m)
    cost = cost_from_slope(slope, max_slope_deg)

    valid = elevation >= NODATA_THRESHOLD
    finite_slope = np.isfinite(slope)
    impassable = cost == COST_IMPASSABLE
    lat_n, lon_w = georef.pixel_to_lonlat(window.row0, window.col0)
    lat_s, lon_e = georef.pixel_to_lonlat(window.row0 + window.rows, window.col0 + window.cols)

    manifest = {
        "format": "mars-eva-aoi/1",
        "name": name or f"aoi_r{window.row0}_c{window.col0}_{window.rows}x{window.cols}",
        "generated_by": "scripts/ingest.py",
        "source": source,
        "crs": {
            "name": "GCS_Mars_2000",
            "ellipsoid": "Mars_2000_IAU_IAG",
            "equatorial_radius_m": MARS_RADIUS_M,
            "prime_meridian": "Reference_Meridian",
        },
        "aoi": {
            "row0": window.row0,
            "col0": window.col0,
            "rows": window.rows,
            "cols": window.cols,
            "bbox_lonlat": {
                "west": lon_w,
                "south": lat_s,
                "east": lon_e,
                "north": lat_n,
            },
            "cell_size_m": {"dy": dy_m, "dx": dx_m},
        },
        "grids": {
            "elevation": {
                "file": "elevation.bin",
                "dtype": "float32",
                "unit": "m",
                "order": "row-major",
                "bytes": window.rows * window.cols * 4,
                "nodata": dem.nodata,
            },
            "slope": {
                "file": "slope.bin",
                "dtype": "float32",
                "unit": "deg",
                "order": "row-major",
                "bytes": window.rows * window.cols * 4,
                "nodata": "NaN",
            },
            "cost": {
                "file": "cost.bin",
                "dtype": "uint8",
                "unit": "relative",
                "order": "row-major",
                "bytes": window.rows * window.cols,
                "encoding": {
                    "0": "no data",
                    "1": "flattest traversable",
                    "254": "steepest traversable",
                    "255": "impassable (slope above threshold)",
                },
            },
        },
        "policy": {
            "max_slope_deg": max_slope_deg,
            "cost_rule": "cost = 1 + round(253 * clip(slope, 0, max_slope_deg) / max_slope_deg)",
            "edge_handling": "one-sided differences at the raster border",
        },
        "stats": {
            "elevation_min_m": float(np.nanmin(elevation[valid])) if valid.any() else None,
            "elevation_max_m": float(np.nanmax(elevation[valid])) if valid.any() else None,
            "relief_m": float(np.nanmax(elevation[valid]) - np.nanmin(elevation[valid])) if valid.any() else None,
            "slope_mean_deg": float(np.nanmean(slope[finite_slope])) if finite_slope.any() else None,
            "slope_max_deg": float(np.nanmax(slope[finite_slope])) if finite_slope.any() else None,
            "cells_total": int(window.rows * window.cols),
            "cells_nodata": int((~valid).sum()),
            "cells_impassable": int(impassable.sum()),
            "fraction_impassable": float(impassable.mean()),
        },
    }

    elevation.astype("<f4", copy=False).tofile(out_dir / "elevation.bin")
    slope.astype("<f4", copy=False).tofile(out_dir / "slope.bin")
    cost.tofile(out_dir / "cost.bin")
    (out_dir / "aoi.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# --- CLI -------------------------------------------------------------------


def _parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in text.replace(",", " ").split()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox needs 4 numbers: west south east north")
    west, south, east, north = parts
    if not (west < east and south < north):
        raise argparse.ArgumentTypeError("bbox must be west<east and south<north")
    return west, south, east, north


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest.py",
        description=(
            "Layer 1 ingestion: crop a 1 km x 1 km AOI out of the Gale DEM and emit "
            "elevation.bin, slope.bin, cost.bin and aoi.json for the C++ core engine."
        ),
        epilog=(
            "Outputs (headerless, row-major, described by aoi.json):\n"
            "  elevation.bin  float32 metres\n"
            "  slope.bin      float32 degrees\n"
            "  cost.bin       uint8 0 invalid / 1..254 traversable / 255 impassable\n"
            "  aoi.json       georeferencing, grid descriptors, statistics\n"
            "\nThe AOI is either a pixel origin plus --size, or a --bbox in lon/lat."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--tif", required=True, type=Path, help="source DEM GeoTIFF")
    p.add_argument("--out", type=Path, default=None, help="output directory (required unless --info)")
    p.add_argument("--row", type=int, default=None, help="AOI origin row in pixels (default 0)")
    p.add_argument("--col", type=int, default=None, help="AOI origin column in pixels (default 0)")
    p.add_argument("--size", type=int, default=1000, help="AOI edge in pixels (default 1000)")
    p.add_argument(
        "--bbox",
        type=_parse_bbox,
        default=None,
        help="AOI as 'west south east north' in degrees (overrides --row/--col/--size)",
    )
    p.add_argument(
        "--max-slope",
        type=float,
        default=DEFAULT_MAX_SLOPE_DEG,
        help=f"slope exclusion threshold in degrees (default {DEFAULT_MAX_SLOPE_DEG})",
    )
    p.add_argument("--name", default=None, help="AOI name written into aoi.json")
    p.add_argument(
        "--no-checksum",
        action="store_true",
        help="skip the source SHA-256 (it reads the whole 5.4 GB file)",
    )
    p.add_argument("--info", action="store_true", help="print the raster header and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.info and args.out is None:
        print("ingest: error: --out is required unless --info is used", file=sys.stderr)
        return 2
    try:
        with TiffDEM(args.tif) as dem:
            if args.info:
                info = {
                    "path": str(args.tif),
                    "bigtiff": dem.is_bigtiff,
                    "width": dem.width,
                    "height": dem.height,
                    "rows_per_strip": dem.rows_per_strip,
                    "strips": len(dem.strip_offsets),
                    "nodata": dem.nodata,
                    "georef": {
                        "lon0": dem.georef.lon0,
                        "lat0": dem.georef.lat0,
                        "px_lon_deg": dem.georef.px_lon,
                        "px_lat_deg": dem.georef.px_lat,
                    }
                    if dem.georef
                    else None,
                }
                print(json.dumps(info, indent=2))
                return 0
            window = resolve_window(dem, args.row, args.col, args.size, args.bbox)
        manifest = ingest(
            args.tif,
            args.out,
            window,
            max_slope_deg=args.max_slope,
            name=args.name,
            with_checksum=not args.no_checksum,
        )
    except (TiffLayoutError, ValueError, OSError) as exc:
        print(f"ingest: error: {exc}", file=sys.stderr)
        return 2
    stats = manifest["stats"]
    print(
        f"{manifest['name']}: {window.rows}x{window.cols} px, "
        f"relief {stats['relief_m']:.1f} m, "
        f"slope mean {stats['slope_mean_deg']:.1f} deg, "
        f"impassable {stats['fraction_impassable'] * 100:.1f}%"
    )
    print(f"wrote {args.out}/elevation.bin, slope.bin, cost.bin, aoi.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
