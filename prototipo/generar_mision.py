"""Genera mission.json para el prototipo a partir del backend del equipo.

- Zona, refugio y capas: SpaceApps2026/data/processed (aoi.json + *.bin).
- Paradas científicas: posiciones reales de Curiosity (MSL_waypoints.json de NASA)
  que caen dentro de la zona.
- Rutas de vista previa: eva_engine sim --path (capa 2), una por parada.

Uso:  python3 generar_mision.py [--backend ../SpaceApps2026]
"""
import argparse
import json
import math
import subprocess
from array import array
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFUGE = (500, 500)
STOP_SOLS = [("A", 4582), ("B", 4544), ("C", 4612), ("D", 4530)]  # soles de Curiosity elegidos


def load_f32(path: Path) -> array:
    a = array("f")
    a.frombytes(path.read_bytes())
    return a


def main():
    ap = argparse.ArgumentParser()
    # Dentro del repo del equipo el backend es la carpeta de arriba; en una copia suelta, ../SpaceApps2026.
    default_backend = HERE.parent if (HERE.parent / "core_engine").is_dir() else HERE.parent / "SpaceApps2026"
    ap.add_argument("--backend", type=Path, default=default_backend)
    args = ap.parse_args()
    data = args.backend / "data" / "processed"
    engine = args.backend / "core_engine" / "eva_engine"

    aoi = json.loads((data / "aoi.json").read_text())
    rows, cols = aoi["aoi"]["rows"], aoi["aoi"]["cols"]
    bb = aoi["aoi"]["bbox_lonlat"]
    slope = load_f32(data / "slope.bin")
    cost = (data / "cost.bin").read_bytes()

    def ll(r, c):  # centro de celda -> [lat, lon]
        return [bb["north"] - (r + 0.5) / rows * (bb["north"] - bb["south"]),
                bb["west"] + (c + 0.5) / cols * (bb["east"] - bb["west"])]

    def nearest_ok(r, c):  # celda transitable más cercana (el waypoint puede caer en roca)
        for rad in range(0, 15):
            best = None
            for dr in range(-rad, rad + 1):
                for dc in range(-rad, rad + 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < rows and 0 <= cc < cols and 1 <= cost[rr * cols + cc] <= 254:
                        d = dr * dr + dc * dc
                        if best is None or d < best[0]:
                            best = (d, rr, cc)
            if best:
                return best[1], best[2]
        raise ValueError(f"sin celda transitable cerca de {r},{c}")

    wps = {}
    for f in json.loads((HERE / "msl_waypoints.json").read_text())["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        if bb["west"] <= lon <= bb["east"] and bb["south"] <= lat <= bb["north"]:
            wps.setdefault(p["sol"], (lat, lon, p.get("site"), p.get("drive")))
    traverse = [[v[0], v[1], sol] for sol, v in sorted(wps.items())]  # [lat, lon, sol]

    stops = []
    for sid, sol in STOP_SOLS:
        lat, lon, site, drive = wps[sol]
        r = int((bb["north"] - lat) / (bb["north"] - bb["south"]) * rows)
        c = int((lon - bb["west"]) / (bb["east"] - bb["west"]) * cols)
        r, c = nearest_ok(r, c)
        res = json.loads(subprocess.run(
            [str(engine), "sim", "--dir", str(data), "--start", f"{REFUGE[0]},{REFUGE[1]}",
             "--goal", f"{r},{c}", "--path"], capture_output=True, text=True, check=True).stdout)
        if not res.get("found"):
            print(f"Parada {sid} (sol {sol}): sin ruta, se omite")
            continue
        path = [ll(q["row"], q["col"]) + [round(q["elevation_m"], 2),
                round(slope[q["row"] * cols + q["col"]], 1), q["row"], q["col"]]
                for q in res["outbound"]["route"]["path"]]
        stops.append({"id": sid, "sol": sol, "site": site, "drive": drive, "row": r, "col": c,
                      "ll": ll(r, c), "path": path, "out": res["outbound"]["sim"],
                      "inb": res["inbound"]["sim"], "flags": res["flags"], "alerts": res["alerts"]})
        print(f"Parada {sid}: sol {sol} -> celda {r},{c} · {res['outbound']['sim']['distance_m']:.0f} m")

    env = aoi.get("environment_summary", {})
    mission = {
        "aoi": {"name": aoi["name"], "bbox": bb,
                "refuge": {"row": REFUGE[0], "col": REFUGE[1], "ll": ll(*REFUGE)}},
        "stops": stops,
        "traverse": traverse,
        "plss": {"o2_capacity_kg": 1.2, "batt_capacity_wh": 250, "o2_low": 0.25, "batt_low": 0.2,
                 "dose_rate_msv_h": env.get("radiation_dose_rate_msv_h", 0.03)},
        "environment": env,
    }
    (HERE / "mission.json").write_text(json.dumps(mission))
    print(f"mission.json: {len(stops)} paradas, {len(traverse)} puntos del recorrido de Curiosity")


if __name__ == "__main__":
    main()
