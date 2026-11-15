#!/usr/bin/env python3
"""Tests for the raw Mars data processing pipeline."""

import unittest
from pathlib import Path
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_raw_data import (
    compute_sky_view_factor,
    generate_radiation_grid,
    parse_msl_rad_dose,
    parse_msl_rems,
    first_existing,
    classify_thermal_inertia,
)


class TestRawDataProcessing(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        raw = self.root / "data" / "raw"
        self.rad_path = first_existing(
            raw / "gale" / "rad_sample" / "RAD_RDR_2014_314_16_54_0805_V00.TXT",
            raw / "18_msl_rad_sol805" / "RAD_RDR_2014_314_16_54_0805_V00.TXT",
        )
        self.rems_path = first_existing(
            raw / "gale" / "rems_pressure_sample" / "RME_397535244RNV00010000000_______PA.TAB",
            raw / "19_msl_rems_sol1" / "RME_397535244RNV00010000000_______PA.TAB",
        )

    def test_parse_msl_rad_dose(self):
        metrics = parse_msl_rad_dose(self.rad_path)
        self.assertEqual(metrics["sol"], 805)
        self.assertGreater(metrics["observations_count"], 100)
        # Curiosity surface dose rate is ~0.02 - 0.05 mSv/h
        self.assertGreater(metrics["dose_rate_msv_per_h"], 0.01)
        self.assertLess(metrics["dose_rate_msv_per_h"], 0.10)

    def test_parse_msl_rems(self):
        rems = parse_msl_rems(self.rems_path)
        self.assertEqual(rems["sol"], 1)
        self.assertAlmostEqual(rems["pressure_pa"], 750.0, places=1)
        self.assertGreater(rems["density_kg_m3"], 0.01)

    def test_sky_view_factor_flat(self):
        flat = np.ones((50, 50), dtype=np.float32) * -3700.0
        svf = compute_sky_view_factor(flat, 1.0, 1.0, max_radius_px=10)
        # On perfectly flat terrain, SVF should be ~ 1.0
        self.assertTrue(np.all(svf >= 0.99))

    def test_sky_view_factor_trench(self):
        trench = np.zeros((50, 50), dtype=np.float32)
        # Deep canyon in the center
        trench[:, :20] = 100.0
        trench[:, 30:] = 100.0
        svf = compute_sky_view_factor(trench, 1.0, 1.0, max_radius_px=15)
        # Center trench should have lower SVF than plateaus
        self.assertLess(svf[25, 25], svf[25, 10])

    def test_radiation_grid_shape_and_range(self):
        synth_elev = np.linspace(-3800, -3500, 100 * 100).reshape((100, 100)).astype(np.float32)
        grid, stats = generate_radiation_grid(synth_elev, 1.0, 1.0, 0.032)
        self.assertEqual(grid.shape, (100, 100))
        self.assertEqual(grid.dtype, np.float32)
        self.assertGreaterEqual(np.nanmin(grid), 0.0)
        self.assertLessEqual(np.nanmax(grid), 1.0)

    def test_thermal_inertia_classification_is_data_driven(self):
        self.assertEqual(classify_thermal_inertia(139.5)[1:], ("high", "low/loose"))
        self.assertEqual(classify_thermal_inertia(275.0)[1:], ("low-to-moderate", "high/firm"))


if __name__ == "__main__":
    unittest.main()
