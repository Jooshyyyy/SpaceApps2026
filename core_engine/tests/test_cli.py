#!/usr/bin/env python3
"""End-to-end tests for the layer 2 command-line interface (stdlib only)."""

import json
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "eva_engine"


def invoke(*args):
    return subprocess.run(
        [str(ENGINE), *map(str, args)],
        text=True,
        capture_output=True,
        check=False,
    )


class EngineCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.write_aoi()
        self.write_grid()

    def write_aoi(self, fmt="mars-eva-aoi/1"):
        metadata = {
            "format": fmt,
            "name": "CLI fixture",
            "source": {"nodata": -3.402823e38},
            "aoi": {
                "rows": 5,
                "cols": 5,
                "bbox_lonlat": {"west": 1.0, "south": 2.0, "east": 3.0, "north": 4.0},
                "cell_size_m": {"dy": 1.0, "dx": 1.0},
            },
            "policy": {"max_slope_deg": 20.0},
        }
        (self.data / "aoi.json").write_text(json.dumps(metadata), encoding="utf-8")

    def write_grid(self):
        (self.data / "elevation.bin").write_bytes(struct.pack("<25f", *([0.0] * 25)))
        (self.data / "slope.bin").write_bytes(struct.pack("<25f", *([0.0] * 25)))
        (self.data / "cost.bin").write_bytes(bytes([1] * 25))

    def test_info_reports_shape_and_missing_radiation(self):
        result = invoke("info", "--dir", self.data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["aoi"]["name"], "CLI fixture")
        self.assertEqual(payload["aoi"]["rows"], 5)
        self.assertEqual(payload["aoi"]["cols"], 5)
        self.assertEqual(payload["traversable_cells"], 25)
        self.assertFalse(payload["radiation_available"])

    def test_route_emits_metrics_and_ordered_path(self):
        result = invoke(
            "route", "--dir", self.data, "--start", "0,0", "--goal", "4,4"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["found"])
        self.assertEqual(len(payload["path"]), 5)
        self.assertEqual(payload["path"][0], {"row": 0, "col": 0, "elevation_m": 0.0})
        self.assertEqual(payload["path"][-1]["row"], 4)
        self.assertEqual(payload["path"][-1]["col"], 4)
        self.assertAlmostEqual(payload["length_m"], 4 * math.sqrt(2), places=6)
        self.assertGreaterEqual(payload["elapsed_ms"], 0.0)
        self.assertGreater(payload["expanded"], 0)
        self.assertFalse(payload["radiation_available"])

    def test_route_reports_unreachable_goal_without_process_error(self):
        costs = bytearray([1] * 25)
        costs[24] = 255
        (self.data / "cost.bin").write_bytes(costs)
        result = invoke(
            "route", "--dir", self.data, "--start", "0,0", "--goal", "4,4"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["found"])
        self.assertEqual(payload["path"], [])

    def test_sim_reports_round_trip_and_alerts(self):
        result = invoke("sim", "--dir", self.data, "--start", "0,0", "--goal", "0,4")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["found"])
        self.assertTrue(payload["return_feasible"])
        self.assertAlmostEqual(payload["outbound"]["sim"]["time_s"], 4.0, places=6)
        self.assertAlmostEqual(payload["inbound"]["sim"]["time_s"], 4.0, places=6)
        self.assertAlmostEqual(payload["ttr_s"], 4.0, places=6)
        self.assertEqual(payload["flags"], 0)
        self.assertEqual(payload["alerts"], [])
        self.assertEqual(payload["reasons"], [])
        self.assertFalse(payload["radiation_available"])

    def test_sim_without_a_route_is_not_return_feasible(self):
        costs = bytearray([1] * 25)
        costs[24] = 255
        (self.data / "cost.bin").write_bytes(costs)
        result = invoke("sim", "--dir", self.data, "--start", "0,0", "--goal", "4,4")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["found"])
        self.assertFalse(payload["return_feasible"])
        self.assertIn("EVA_ABORT", payload["alerts"])

    def test_invalid_input_returns_json_error(self):
        result = invoke("route", "--dir", self.data, "--start", "bad", "--goal", "4,4")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn("start", payload["error"])

    def test_unknown_option_is_rejected(self):
        result = invoke("info", "--dir", self.data, "--invented", "1")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn("unknown option", payload["error"])


if __name__ == "__main__":
    unittest.main()
