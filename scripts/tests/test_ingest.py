#!/usr/bin/env python3
"""Tests for the layer 1 ingestion pipeline.

No GDAL, no pytest: standard library unittest plus numpy, matching the runtime
the pipeline itself targets. The suite never touches the 5.4 GB real DEM; it
builds a small synthetic BigTIFF with the same layout (uncompressed, one row
per strip) and runs the real reader against it.

Run with:  python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest import (  # noqa: E402
    COST_IMPASSABLE,
    COST_INVALID,
    COST_MAX,
    COST_MIN,
    DEFAULT_AOI_COL,
    DEFAULT_AOI_ROW,
    DEFAULT_AOI_SIZE,
    DEFAULT_MAX_SLOPE_DEG,
    NODATA_SENTINEL,
    GeoRef,
    TiffDEM,
    TiffLayoutError,
    Window,
    cost_from_slope,
    ingest,
    resolve_window,
    slope_degrees,
)

# --- Synthetic BigTIFF builder ---------------------------------------------

_TAG_WIDTH = 256
_TAG_HEIGHT = 257
_TAG_BITS = 258
_TAG_COMPRESSION = 259
_TAG_STRIP_OFFSETS = 273
_TAG_SAMPLES_PER_PIXEL = 277
_TAG_ROWS_PER_STRIP = 278
_TAG_STRIP_BYTE_COUNTS = 279
_TAG_SAMPLE_FORMAT = 339
_TAG_PIXEL_SCALE = 33550
_TAG_TIEPOINT = 33922
_TAG_GDAL_NODATA = 42113

_LONG8 = 16
_DOUBLE = 12
_ASCII = 2
_SHORT = 3
_LONG = 4


def _entry(tag: int, typ: int, count: int, value: int) -> bytes:
    """One BigTIFF IFD entry: tag, type, count, inline value or offset."""
    return struct.pack("<HHQQ", tag, typ, count, value)


def write_bigtiff(
    path: Path,
    data: np.ndarray,
    *,
    scale: tuple[float, float] = (1.0, 1.0),
    tiepoint: tuple[float, float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    nodata: str = "-3.4028234663852886e+38",
    compression: int = 1,
    bits: int = 32,
    sample_format: int = 3,
    georef: bool = True,
) -> None:
    """Write a minimal uncompressed BigTIFF, one row per strip, little-endian.

    Set georef=False to omit the PixelScale and Tiepoint tags, producing a
    raster that carries no georeferencing at all.
    """
    rows, cols = data.shape
    payload = np.ascontiguousarray(data, dtype="<f4").tobytes()
    # PixelScale is three doubles (x, y, z); callers usually pass two.
    scale = tuple(scale) + (0.0,) * (3 - len(scale))

    entries = [
        (_TAG_WIDTH, _LONG, 1, cols),
        (_TAG_HEIGHT, _LONG, 1, rows),
        (_TAG_BITS, _SHORT, 1, bits),
        (_TAG_COMPRESSION, _SHORT, 1, compression),
        (_TAG_STRIP_OFFSETS, _LONG8, rows, 0),  # patched below
        (_TAG_SAMPLES_PER_PIXEL, _SHORT, 1, 1),
        (_TAG_ROWS_PER_STRIP, _LONG, 1, 1),
        (_TAG_STRIP_BYTE_COUNTS, _LONG8, rows, 0),  # patched below
        (_TAG_SAMPLE_FORMAT, _SHORT, 1, sample_format),
    ]
    if georef:
        entries.append((_TAG_PIXEL_SCALE, _DOUBLE, 3, 0))  # patched below
        entries.append((_TAG_TIEPOINT, _DOUBLE, 6, 0))  # patched below
    entries.append((_TAG_GDAL_NODATA, _ASCII, len(nodata) + 1, 0))  # patched below

    ifd_offset = 16
    ifd_size = 8 + len(entries) * 20 + 8
    cursor = ifd_offset + ifd_size

    strip_offsets_off = cursor
    cursor += 8 * rows
    strip_counts_off = cursor
    cursor += 8 * rows
    if georef:
        scale_off = cursor
        cursor += 8 * 3
        tie_off = cursor
        cursor += 8 * 6
    nodata_off = cursor
    cursor += len(nodata) + 1
    pixel_off = cursor

    strip_offsets = [pixel_off + i * cols * 4 for i in range(rows)]
    strip_counts = [cols * 4] * rows

    patched = []
    for tag, typ, count, value in entries:
        if tag == _TAG_STRIP_OFFSETS:
            value = strip_offsets_off
        elif tag == _TAG_STRIP_BYTE_COUNTS:
            value = strip_counts_off
        elif tag == _TAG_PIXEL_SCALE:
            value = scale_off
        elif tag == _TAG_TIEPOINT:
            value = tie_off
        elif tag == _TAG_GDAL_NODATA:
            value = nodata_off
        patched.append(_entry(tag, typ, count, value))

    blob = bytearray()
    blob += b"II" + struct.pack("<H", 43) + struct.pack("<HHQ", 8, 0, ifd_offset)
    blob += struct.pack("<Q", len(patched))
    blob += b"".join(patched)
    blob += struct.pack("<Q", 0)  # no next IFD
    blob += struct.pack(f"<{rows}Q", *strip_offsets)
    blob += struct.pack(f"<{rows}Q", *strip_counts)
    if georef:
        blob += struct.pack("<3d", *scale)
        blob += struct.pack("<6d", *tiepoint)
    blob += nodata.encode("ascii") + b"\x00"
    blob += payload
    path.write_bytes(bytes(blob))


# --- Georeferencing --------------------------------------------------------


class TestGeoRef(unittest.TestCase):
    def test_pixel_to_lonlat_uses_the_outer_corner(self):
        g = GeoRef(lon0=10.0, lat0=-4.0, px_lon=0.001, px_lat=0.001)
        lat, lon = g.pixel_to_lonlat(0, 0)
        self.assertAlmostEqual(lat, -4.0)
        self.assertAlmostEqual(lon, 10.0)
        lat, lon = g.pixel_to_lonlat(100, 50)
        self.assertAlmostEqual(lat, -4.1)
        self.assertAlmostEqual(lon, 10.05)

    def test_lonlat_to_pixel_round_trips(self):
        g = GeoRef(lon0=137.5, lat0=-4.6, px_lon=1.687e-05, px_lat=1.687e-05)
        for row, col in [(0, 0), (1234, 567), (40099, 35999)]:
            lat, lon = g.pixel_to_lonlat(row, col)
            r, c = g.lonlat_to_pixel(lon, lat)
            self.assertAlmostEqual(r, row, places=6)
            self.assertAlmostEqual(c, col, places=6)

    def test_cell_size_shrinks_toward_the_poles(self):
        g = GeoRef(lon0=0.0, lat0=0.0, px_lon=1e-05, px_lat=1e-05)
        dy_eq, dx_eq = g.cell_size_m(0)
        dy_hi, dx_hi = g.cell_size_m(50_000)
        self.assertAlmostEqual(dy_eq, dy_hi, places=3)  # meridians stay parallel
        self.assertLess(dx_hi, dx_eq)  # parallels converge


# --- Slope and cost --------------------------------------------------------


class TestSlope(unittest.TestCase):
    def test_flat_surface_has_zero_slope(self):
        z = np.full((5, 5), 100.0, dtype=np.float32)
        s = slope_degrees(z, 1.0, 1.0)
        self.assertTrue(np.allclose(s, 0.0, atol=1e-9))

    def test_known_ramp_matches_atan(self):
        # 1 m rise per 1 m east -> 45 degrees.
        z = np.tile(np.arange(10, dtype=np.float32), (10, 1))
        s = slope_degrees(z, 1.0, 1.0)
        self.assertAlmostEqual(float(s[5, 5]), 45.0, places=4)

    def test_metric_spacing_changes_the_angle(self):
        # Same 1 m rise but 2 m per column -> atan(0.5) = 26.565 degrees.
        z = np.tile(np.arange(10, dtype=np.float32), (10, 1))
        s = slope_degrees(z, 1.0, 2.0)
        self.assertAlmostEqual(float(s[5, 5]), 26.565051, places=4)

    def test_nodata_becomes_nan_and_never_flat(self):
        z = np.full((4, 4), 50.0, dtype=np.float32)
        z[1, 1] = NODATA_SENTINEL
        s = slope_degrees(z, 1.0, 1.0)
        self.assertTrue(np.isnan(s[1, 1]))
        # A nodata cell contaminates its neighbours instead of hiding as flat.
        self.assertTrue(np.isnan(s[1, 2]) or np.isnan(s[2, 1]))


class TestCost(unittest.TestCase):
    def test_flat_is_the_minimum_traversable_cost(self):
        s = np.zeros((3, 3), dtype=np.float32)
        c = cost_from_slope(s)
        self.assertTrue(np.all(c == COST_MIN))

    def test_threshold_is_impassable(self):
        s = np.array([[DEFAULT_MAX_SLOPE_DEG + 0.1, DEFAULT_MAX_SLOPE_DEG]], dtype=np.float32)
        c = cost_from_slope(s)
        self.assertEqual(int(c[0, 0]), COST_IMPASSABLE)
        # Exactly at the threshold stays traversable and saturates at COST_MAX.
        self.assertEqual(int(c[0, 1]), COST_MAX)

    def test_nan_is_invalid_not_impassable(self):
        s = np.array([[np.nan, 1.0]], dtype=np.float32)
        c = cost_from_slope(s)
        self.assertEqual(int(c[0, 0]), COST_INVALID)
        self.assertGreater(int(c[0, 1]), COST_MIN)

    def test_cost_is_monotone_in_slope(self):
        s = np.linspace(0.0, DEFAULT_MAX_SLOPE_DEG - 0.01, 200).reshape(1, -1).astype(np.float32)
        c = cost_from_slope(s).astype(np.int16)
        self.assertTrue(np.all(np.diff(c[0]) >= 0))

    def test_negative_and_over_threshold_slope_clamp(self):
        s = np.array([[-5.0, DEFAULT_MAX_SLOPE_DEG + 50.0]], dtype=np.float32)
        c = cost_from_slope(s)
        self.assertEqual(int(c[0, 0]), COST_MIN)
        self.assertEqual(int(c[0, 1]), COST_IMPASSABLE)


# --- Raw reader ------------------------------------------------------------


class TestReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.data = np.arange(35, dtype=np.float32).reshape(5, 7)
        self.path = self.dir / "synthetic.tif"
        write_bigtiff(
            self.path,
            self.data,
            scale=(0.001, 0.001),
            tiepoint=(0.0, 0.0, 0.0, 137.5, -4.6, 0.0),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_header_reports_a_bigtiff_single_row_strip_layout(self):
        with TiffDEM(self.path) as dem:
            self.assertTrue(dem.is_bigtiff)
            self.assertEqual((dem.height, dem.width), (5, 7))
            self.assertEqual(dem.rows_per_strip, 1)
            self.assertEqual(len(dem.strip_offsets), 5)

    def test_read_row_matches_the_source(self):
        with TiffDEM(self.path) as dem:
            for row in range(5):
                np.testing.assert_array_equal(dem.read_row(row), self.data[row])

    def test_read_window_matches_the_source(self):
        with TiffDEM(self.path) as dem:
            np.testing.assert_array_equal(dem.read_window(1, 2, 3, 4), self.data[1:4, 2:6])

    def test_window_outside_bounds_raises(self):
        with TiffDEM(self.path) as dem:
            with self.assertRaises(ValueError):
                dem.read_window(3, 0, 5, 2)

    def test_georef_is_read_from_scale_and_tiepoint(self):
        with TiffDEM(self.path) as dem:
            self.assertIsNotNone(dem.georef)
            self.assertAlmostEqual(dem.georef.lon0, 137.5)
            self.assertAlmostEqual(dem.georef.lat0, -4.6)
            self.assertAlmostEqual(dem.georef.px_lon, 0.001)

    def test_nodata_is_parsed_from_the_ascii_tag(self):
        with TiffDEM(self.path) as dem:
            self.assertAlmostEqual(dem.nodata, NODATA_SENTINEL, places=20)

    def test_compressed_file_is_rejected_loudly(self):
        bad = self.dir / "compressed.tif"
        write_bigtiff(bad, self.data, compression=2)
        with self.assertRaises(TiffLayoutError):
            TiffDEM(bad)

    def test_non_float32_file_is_rejected_loudly(self):
        bad = self.dir / "uint16.tif"
        write_bigtiff(bad, self.data, bits=16, sample_format=1)
        with self.assertRaises(TiffLayoutError):
            TiffDEM(bad)


# --- Window resolution -----------------------------------------------------


class TestResolveWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "synthetic.tif"
        write_bigtiff(
            self.path,
            np.zeros((100, 120), dtype=np.float32),
            scale=(0.001, 0.001),
            tiepoint=(0.0, 0.0, 0.0, 137.0, -4.0, 0.0),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_pixel_origin_and_size(self):
        with TiffDEM(self.path) as dem:
            w = resolve_window(dem, row=10, col=20, size=30)
            self.assertEqual((w.row0, w.col0, w.rows, w.cols), (10, 20, 30, 30))

    def test_default_window_is_the_curiosity_eva_site(self):
        # Use a lightweight stand-in large enough for the production default;
        # resolve_window only needs these bounds for a pixel-based window.
        class LargeDEM:
            height = 40100
            width = 36000

        w = resolve_window(LargeDEM())
        self.assertEqual(
            (w.row0, w.col0, w.rows, w.cols),
            (DEFAULT_AOI_ROW, DEFAULT_AOI_COL, DEFAULT_AOI_SIZE, DEFAULT_AOI_SIZE),
        )

    def test_bbox_maps_to_the_expected_pixel_window(self):
        with TiffDEM(self.path) as dem:
            # 0.010 deg = 10 px at 0.001 deg/px, starting at the raster corner.
            w = resolve_window(dem, bbox=(137.0, -4.010, 137.010, -4.0))
            self.assertEqual((w.row0, w.col0, w.rows, w.cols), (0, 0, 10, 10))

    def test_window_beyond_the_raster_raises(self):
        with TiffDEM(self.path) as dem:
            with self.assertRaises(ValueError):
                resolve_window(dem, row=95, col=0, size=10)


# --- End-to-end pipeline ---------------------------------------------------


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tif = self.dir / "dem.tif"
        # A 20x20 plane tilting 1 m per pixel east and 0.5 m per pixel south.
        rows = np.arange(20, dtype=np.float32)[:, None]
        cols = np.arange(20, dtype=np.float32)[None, :]
        data = 100.0 + 0.5 * rows + 1.0 * cols
        write_bigtiff(
            self.tif,
            data,
            scale=(0.001, 0.001),
            tiepoint=(0.0, 0.0, 0.0, 137.0, -4.0, 0.0),
        )
        self.out = self.dir / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, window: Window, **kwargs):
        return ingest(self.tif, self.out, window, with_checksum=False, **kwargs)

    def test_outputs_have_the_sizes_declared_in_the_manifest(self):
        manifest = self._run(Window(0, 0, 20, 20))
        self.assertEqual((self.out / "elevation.bin").stat().st_size, 20 * 20 * 4)
        self.assertEqual((self.out / "slope.bin").stat().st_size, 20 * 20 * 4)
        self.assertEqual((self.out / "cost.bin").stat().st_size, 20 * 20)
        self.assertEqual(manifest["grids"]["elevation"]["bytes"], 20 * 20 * 4)

    def test_manifest_records_the_aoi_and_its_georeferencing(self):
        manifest = self._run(Window(2, 3, 10, 12), name="unit-test-aoi")
        self.assertEqual(manifest["name"], "unit-test-aoi")
        self.assertEqual(manifest["format"], "mars-eva-aoi/1")
        self.assertEqual(manifest["aoi"]["row0"], 2)
        self.assertEqual(manifest["aoi"]["col0"], 3)
        self.assertEqual(manifest["aoi"]["rows"], 10)
        self.assertEqual(manifest["aoi"]["cols"], 12)
        self.assertAlmostEqual(manifest["aoi"]["bbox_lonlat"]["west"], 137.003)
        self.assertAlmostEqual(manifest["aoi"]["bbox_lonlat"]["north"], -4.002)
        self.assertIsNone(manifest["source"]["sha256"])

    def test_written_bytes_match_the_computed_arrays(self):
        manifest = self._run(Window(0, 0, 20, 20))
        z = np.fromfile(self.out / "elevation.bin", dtype="<f4").reshape(20, 20)
        s = np.fromfile(self.out / "slope.bin", dtype="<f4").reshape(20, 20)
        c = np.fromfile(self.out / "cost.bin", dtype=np.uint8).reshape(20, 20)

        # Elevation is copied verbatim, row-major.
        rows = np.arange(20, dtype=np.float32)[:, None]
        cols = np.arange(20, dtype=np.float32)[None, :]
        np.testing.assert_allclose(z, 100.0 + 0.5 * rows + 1.0 * cols, atol=1e-4)
        # The written slope is the metric slope of the written elevation. The
        # fixture's pixel scale is in degrees, so 0.001 deg is about 59 m on
        # Mars: a 1 m-per-column ramp is a gentle 1.08 deg, not 45 deg.
        cell = manifest["aoi"]["cell_size_m"]
        expected_slope = slope_degrees(z, cell["dy"], cell["dx"])
        np.testing.assert_allclose(s, expected_slope, atol=1e-4)
        np.testing.assert_array_equal(c, cost_from_slope(expected_slope))
        self.assertEqual(manifest["stats"]["cells_impassable"], int((c == COST_IMPASSABLE).sum()))

    def test_aoi_json_is_valid_json_and_matches_the_returned_manifest(self):
        manifest = self._run(Window(0, 0, 20, 20))
        on_disk = json.loads((self.out / "aoi.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, manifest)

    def test_ingest_refuses_a_raster_without_georeferencing(self):
        flat = self.dir / "nogeo.tif"
        write_bigtiff(flat, np.zeros((4, 4), dtype=np.float32), georef=False)
        with self.assertRaises(TiffLayoutError):
            ingest(flat, self.out, Window(0, 0, 4, 4), with_checksum=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
