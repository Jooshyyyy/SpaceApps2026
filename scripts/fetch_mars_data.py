#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ingesta reproducible de datos de Marte (NASA Space Apps).

Instalacion: python -m pip install requests
Ejemplos:
    python descargar_marte.py --list
    python descargar_marte.py --region gale --profile base
    python descargar_marte.py --region gale --profile science
    python descargar_marte.py --region jezero --profile base
    python descargar_marte.py --region gale --only mola_megdr tes_thermal gale_dtm
    python descargar_marte.py --region jezero --profile full --max-gb 20

No convierte todos los productos a una sola tabla. Guarda archivos originales,
metadatos y manifest.json para un proceso ETL posterior. Fuentes NASA/USGS/PDS.
"""
from __future__ import annotations

import argparse
import hashlib
import zipfile
import json
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from typing import Any

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("Falta requests. Instala con: python -m pip install requests")

PDS_MOLA = (
    "https://pds-geosciences.wustl.edu/mgs/"
    "mgs-m-mola-5-megdr-l3-v1/mgsl_300x/meg004/"
)
PDS_TES = "https://pds-geosciences.wustl.edu/mgs/mgs-m-tes-5-timap-v1/"
GALE = (
    "https://www.uahirise.org/PDS/DTM/ESP/ORB_040700_040799/"
    "ESP_040770_1755_ESP_039280_1755/"
)
JEZERO = "https://asc-pds-services.s3.us-west-2.amazonaws.com/mosaic/mars2020_trn/HiRISE/"
REMS = (
    "https://atmos.nmsu.edu/PDS/data/PDS4/msl_rems_bundle/"
    "data_calibrated/SOL_00001_00089/SOL00001/"
)
RAD = (
    "https://pds-ppi.igpp.ucla.edu/data/MSL-M-RAD-3-RDR-V1.0/"
    "DATA/SOL_00805_00938/"
)
TES_WMS = "https://ms-mars.mars.asu.edu"
GEOLOGY_API = (
    "https://services.arcgis.com/nzS0F0zdNLvs7nc8/ArcGIS/rest/services/"
    "Mars_Geology_feature_layer/FeatureServer/0/query"
)

# [lonW, latS, lonE, latN], Mars planetocentric degrees, longitude East.
# El servicio USGS usa longitud -180..180, mientras que PDS/Mars Trek pueden usar 0..360.
REGIONS = {
    "gale": (136.85, -5.10, 138.00, -4.20),
    "jezero": (77.10, 18.10, 78.10, 19.00),
}


def entry(name: str, kind: str, url: str | None = None, *, region="all",
          level="base", purpose="", companions=(), notes="") -> dict[str, Any]:
    return dict(name=name, kind=kind, url=url, region=region,
                level=level, purpose=purpose, companions=list(companions), notes=notes)


CATALOG: dict[str, dict[str, Any]] = {
    "mola_megdr": entry(
        "MOLA MEGDR Global Topography, 4 pix/degree", "file",
        PDS_MOLA + "megt90n000cb.img", purpose="Elevacion global (DEM de baja resolucion)",
        companions=[PDS_MOLA + "megt90n000cb.lbl"],
        notes="Es topografia en metros; no detecta obstaculos a escala peatonal."
    ),
    "tes_thermal": entry(
        "MGS TES Derived Thermal Inertia Maps", "file",
        PDS_TES + "mgst_9001.zip", purpose="Inercia termica del suelo",
        notes="Incluye mascaras de interpolacion y etiquetas PDS en el ZIP."
    ),
    "usgs_geology": entry(
        "USGS Geologic Map of Mars, SIM 3292", "arcgis",
        GEOLOGY_API, purpose="Unidades geologicas de la region seleccionada",
        notes="Vectorial regional, sin clasificar directamente transitabilidad."
    ),
    "gale_dtm": entry(
        "HiRISE Gale DTM, DTEEC_040770_1755_039280_1755_A01", "file",
        GALE + "DTEEC_040770_1755_039280_1755_A01.IMG",
        region="gale", purpose="Elevacion local y pendientes, ~1 m/pixel",
        notes="Cubre solo un sector de Gale; no todo Mount Sharp. Unos 303 MB."
    ),
    "gale_ortho": entry(
        "HiRISE Gale Orthophoto, ESP_040770_1755_RED_C_01_ORTHO", "file",
        GALE + "ESP_040770_1755_RED_C_01_ORTHO.JP2",
        region="gale", purpose="Imagen local para identificar obstaculos visuales",
        companions=[GALE + "ESP_040770_1755_RED_C_01_ORTHO.LBL"],
        notes="JP2 de 25 MB; la clasificacion de obstaculos se calcula despues."
    ),
    "jezero_dtm": entry(
        "HiRISE Jezero Controlled DTM, 1 m/pixel", "file",
        JEZERO + "JEZ_hirise_soc_006_DTM_MOLAtopography_DeltaGeoid_1m_Eqc_latTs0_lon0_blend40.tif",
        region="jezero", purpose="Elevacion y pendientes locales",
        companions=[JEZERO + "JEZ_hirise_soc_006_DTM_MOLAtopography_DeltaGeoid_1m_Eqc_latTs0_lon0_blend40.xml"],
        notes="GeoTIFF grande: posible bloqueo por limite de descarga."
    ),
    "jezero_ortho": entry(
        "HiRISE Jezero Orthomosaic, 25 cm/pixel", "file",
        JEZERO + "JEZ_hirise_soc_006_orthoMosaic_25cm_Eqc_latTs0_lon0_first.tif",
        region="jezero", purpose="Imagen de alta resolucion para obstaculos",
        companions=[JEZERO + "JEZ_hirise_soc_006_orthoMosaic_25cm_Eqc_latTs0_lon0_first.xml"],
        notes="GeoTIFF grande; no contiene una mascara de rocas ya calculada."
    ),
    "rad_sample": entry(
        "MSL RAD RDR: ejemplo de radiacion (Curiosity sol 805)", "file",
        RAD + "RAD_RDR_2014_314_16_54_0805_V00.TXT", region="gale",
        purpose="Medidas locales de radiacion ionizante", level="base",
        companions=[RAD + "RAD_RDR_2014_314_16_54_0805_V00.LBL"],
        notes="Archivo y etiqueta PDS para un solo sol, no mapa global."
    ),
    "rems_pressure_sample": entry(
        "MSL REMS PA: presion calibrada (Curiosity sol 1)", "file",
        REMS + "RME_397535244RNV00010000000_______PA.TAB", region="gale",
        purpose="Presion atmosferica local en Gale", level="base",
        notes="Solo producto del sensor PA; no equivale a todos los sensores REMS."
    ),
    "meda_derived": entry(
        "Mars 2020 MEDA Derived Environmental Data", "file",
        "https://atmos.nmsu.edu/PDS/data/PDS4/Mars2020/meda_derived.tar.gz",
        region="jezero", level="science",
        purpose="Series meteorologicas en Jezero (archivo integral, ~2.9 GB)"
    ),
    "tes_albedo": entry(
        "TES global surface albedo numeric", "vicar",
        TES_WMS + "/alb32", level="science",
        purpose="Reflectividad solar superficial", notes="No mide radiacion ionizante.",
        companions=["TES_Lambert_Albedo_numeric"]
    ),
    "tes_dust": entry(
        "TES global dust abundance numeric", "vicar",
        TES_WMS + "/TES_Dust_numeric", level="science",
        purpose="Abundancia superficial de polvo (no tormentas atmosfericas)",
        companions=["TES_Dust_numeric"]
    ),
    "tes_pyroxene": entry(
        "TES high-calcium pyroxene abundance numeric", "vicar",
        TES_WMS + "/TES_Clinopyroxene_numeric", level="science",
        purpose="Mineralogia regional", companions=["TES_Clinopyroxene_numeric"]
    ),
    "tes_plagioclase": entry(
        "TES plagioclase abundance numeric", "vicar",
        TES_WMS + "/TES_Plagioclase_numeric", level="science",
        purpose="Mineralogia regional", companions=["TES_Plagioclase_numeric"]
    ),
    "tes_sheet_silicates": entry(
        "TES sheet silicates/high-Si glass abundance numeric", "vicar",
        TES_WMS + "/TES_Glass_Clay_numeric", level="science",
        purpose="Mineralogia regional, no humedad actual", companions=["TES_Glass_Clay_numeric"]
    ),
    "mro_spice": entry(
        "MRO SPICE spacecraft ephemeris", "file",
        "https://naif.jpl.nasa.gov/pub/naif/MRO/kernels/spk/mro_psp.bsp",
        level="full", purpose="Geometria orbital (requiere kernels y calculos adicionales)",
        notes="No proporciona por si solo cobertura de comunicaciones."
    ),
    "crism_example": entry(
        "CRISM MTRDR sample (NOT validated for Gale/Jezero region)", "file",
        "https://pds-geosciences.wustl.edu/mro/mro-m-crism-5-rdr-mptargeted-v1/"
        "mrocr_4001/mtrdr/2006/2006_273/hrs00002847/hrs00002847_07_if173j_mtr3.img",
        region="manual", level="manual",
        purpose="Cubo espectral de ejemplo; NO usar para la region sin verificar huella",
        notes="Buscar observaciones CRISM de su AOI en PDS ODE y descargar HDR/LBL antes de procesar."
    ),
}


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=2, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "HEAD"]))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "MarsSpaceAppsDatasetDownloader/1.0 (scientific educational use)",
                            "Accept": "*/*"})
    return session


def is_error_content(prefix: bytes, content_type: str, suffix: str) -> bool:
    sample = prefix[:2048].lstrip().lower()
    ctype = content_type.lower()
    if "text/html" in ctype or sample.startswith((b"<!doctype html", b"<html", b"<head")):
        return True
    if suffix not in (".xml", ".json", ".geojson", ".lbl"):
        if sample.startswith((b"<?xml", b"<serviceexception", b"<exceptionreport", b"{")):
            return True
    if b"serviceexception" in sample or b"accessdenied" in sample:
        return True
    return False


def file_signature_ok(path: Path) -> None:
    with path.open("rb") as f:
        head = f.read(2048)
    if is_error_content(head, "", path.suffix.lower()):
        raise ValueError("El archivo guardado contiene una pagina de error")
    if path.suffix.lower() == ".zip" and not zipfile.is_zipfile(path):
        raise ValueError("No es ZIP valido")
    if path.name.endswith(".tar.gz") and not head.startswith(b"\x1f\x8b"):
        raise ValueError("No es GZIP valido")
    if path.suffix.lower() in (".tif", ".tiff") and head[:4] not in (
            b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        raise ValueError("No es TIFF/BigTIFF valido")
    if path.suffix.lower() == ".jp2" and not (
            head.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n") or head.startswith(b"\xff\x4f\xff\x51")):
        raise ValueError("No es JPEG2000 valido")
    if path.suffix.lower() == ".vic" and not head.startswith(b"LBLSIZE="):
        raise ValueError("No parece VICAR; el servicio puede haber devuelto otro formato")
    if path.suffix.lower() == ".bsp" and not head.startswith(b"DAF/"):
        raise ValueError("No parece un kernel SPK (DAF)")


def download(session: requests.Session, url: str, dest: Path,
             cap_bytes: int, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Descarga HTTP con control de tamaño, contenido, reanudación y SHA256.

    Si no hay Range, reinicia de cero. Si el servidor devuelve HTML, NO conserva
    el archivo como si fuera raster o dato científico.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        try:
            file_signature_ok(dest)
            h = hashlib.sha256()
            with dest.open("rb") as previous:
                for block in iter(lambda: previous.read(1024 * 1024), b""):
                    h.update(block)
            return {"status": "exists", "path": str(dest), "bytes": dest.stat().st_size,
                    "sha256": h.hexdigest()}
        except Exception:
            dest.unlink()
    tmp = dest.with_name(dest.name + ".part")
    offset = tmp.stat().st_size if tmp.exists() else 0
    if offset >= cap_bytes:
        raise ValueError("Parcial previo mayor que --max-gb; aumentar el limite o borrar .part")
    headers = {"Range": f"bytes={offset}-"} if offset and not params else {}
    with session.get(url, params=params, headers=headers, stream=True, timeout=(20, 180)) as res:
        res.raise_for_status()
        append = offset > 0 and res.status_code == 206
        if not append:
            offset = 0
        content_len = int(res.headers.get("Content-Length") or 0)
        expected = offset + content_len if content_len else None
        if expected and expected > cap_bytes:
            raise ValueError(f"Archivo ~{expected/1024**3:.2f} GB supera --max-gb. "
                             "Usar --max-gb superior si se desea.")
        total = offset
        digest = hashlib.sha256()
        if append:
            with tmp.open("rb") as prev:
                for chunk in iter(lambda: prev.read(1024 * 1024), b""):
                    digest.update(chunk)
        with tmp.open("ab" if append else "wb") as out:
            prefix = b""
            last_report = time.monotonic()
            for chunk in res.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if len(prefix) < 2048:
                    prefix += chunk[:2048-len(prefix)]
                    if is_error_content(prefix, res.headers.get("Content-Type", ""), dest.suffix.lower()):
                        raise ValueError(f"El servidor entrego HTML/XML/error en lugar de datos: {url}")
                total += len(chunk)
                if total > cap_bytes:
                    raise ValueError("Limite --max-gb superado durante la descarga")
                out.write(chunk)
                digest.update(chunk)
                if time.monotonic() - last_report > 5:
                    print(f"    {total / 1024**2:.1f} MiB descargados...", flush=True)
                    last_report = time.monotonic()
        if total == 0:
            raise ValueError("Archivo vacio")
        if expected and not res.headers.get("Content-Encoding") and total != expected:
            raise ValueError(f"Descarga incompleta: {total} de {expected} bytes")
    # La validacion por extension se hace tras renombrar el parcial.
    tmp.replace(dest)
    try:
        file_signature_ok(dest)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return {"status": "ok", "path": str(dest), "bytes": total,
            "sha256": digest.hexdigest(), "url_final": res.url}


def arcgis_geology(session: requests.Session, dest: Path,
                   region: str) -> dict[str, Any]:
    """Extrae todos los polígonos USGS que intersecan la region, sin truncar en 100."""
    lon_w, lat_s, lon_e, lat_n = REGIONS[region]
    query = dict(where="1=1", geometry=f"{lon_w},{lat_s},{lon_e},{lat_n}",
                 geometryType="esriGeometryEnvelope", inSR=104971,
                 spatialRel="esriSpatialRelIntersects", f="json", returnIdsOnly="true")
    r = session.get(GEOLOGY_API, params=query, timeout=(20, 180))
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise ValueError(f"ArcGIS: {data['error']}")
    object_ids = sorted(data.get("objectIds") or [])
    if not object_ids:
        raise ValueError("ArcGIS no devolvio poligonos: revisar AOI/proyeccion")
    features: list[dict[str, Any]] = []
    for i in range(0, len(object_ids), 50):
        ids = object_ids[i:i+50]
        r = session.get(GEOLOGY_API, params={"objectIds": ",".join(map(str, ids)),
                    "outFields": "*", "returnGeometry": "true", "outSR": 104971,
                    "f": "geojson"}, timeout=(20, 180))
        r.raise_for_status()
        payload = r.json()
        if "error" in payload or payload.get("type") != "FeatureCollection":
            raise ValueError(f"Respuesta GIS inesperada: {str(payload)[:300]}")
        features.extend(payload["features"])
    if not features:
        raise ValueError("Sin geometria retornada")
    dest.parent.mkdir(parents=True, exist_ok=True)
    obj = {"type": "FeatureCollection", "features": features}
    dest.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return {"status": "ok", "path": str(dest), "features": len(features),
            "bytes": dest.stat().st_size, "spatial_reference": "Mars 104971, NO Earth EPSG:4326"}


def main() -> int:
    p = argparse.ArgumentParser(description="Descargar datasets cientificos de Marte")
    p.add_argument("--region", choices=REGIONS, default="gale")
    p.add_argument("--profile", choices=("base", "science", "full"), default="base")
    p.add_argument("--only", nargs="+", choices=sorted(CATALOG), help="Solo descargar estos IDs")
    p.add_argument("--list", action="store_true", help="Listar todos los productos")
    p.add_argument("--dry-run", action="store_true", help="Mostrar plan, sin descargar")
    p.add_argument("--out", default="datos_marte", help="Carpeta destino")
    p.add_argument("--max-gb", type=float, default=1.0,
                   help="Limite por archivo, en GiB (predeterminado 1)")
    args = p.parse_args()
    if args.list:
        print("ID                     Region    Nivel    Uso")
        for k, v in CATALOG.items():
            print(f"{k:22} {v['region']:9} {v['level']:8} {v['purpose']}")
        return 0
    if args.max_gb <= 0:
        p.error("--max-gb debe ser positivo")
    levels = {"base": 1, "science": 2, "full": 3}
    names = args.only or [k for k, v in CATALOG.items()
                          if v["region"] in ("all", args.region)
                          and v["level"] in levels
                          and levels[v["level"]] <= levels[args.profile]]
    root = Path(args.out) / args.region
    print(f"Region: {args.region} | Perfil: {args.profile} | Productos: {len(names)}")
    for name in names:
        v = CATALOG[name]
        print(f" - {name}: {v['purpose']} ({v['url']})")
    if args.dry_run:
        return 0
    s = make_session()
    manifest: dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "region": args.region,
        "bbox_lonlat_east": REGIONS[args.region],
        "reference": "NASA/PDS/USGS. Mars coordinates, NOT terrestrial WGS84.",
        "products": []
    }
    cap = round(args.max_gb * 1024**3)
    failed = 0
    for name in names:
        v = CATALOG[name]
        record = {"id": name, "name": v["name"], "use": v["purpose"],
                  "source_url": v["url"], "notes": v["notes"], "files": [], "status": "ok"}
        print(f"\n[{name}] {v['name']}", flush=True)
        try:
            if v["region"] not in ("all", args.region):
                print("  AVISO: producto de region diferente a la seleccionada")
            target_dir = root / name
            if v["kind"] == "arcgis":
                record["files"].append(arcgis_geology(s, target_dir / "geologia.geojson", args.region))
            else:
                urls = [v["url"]]
                params = None
                if v["kind"] == "vicar":
                    layer = v["companions"][0]
                    params = dict(SERVICE="WMS", REQUEST="GetMap", VERSION="1.1.1",
                                  LAYERS=layer, STYLES="", FORMAT="image/vicar",
                                  SRS="JMARS:4", BBOX="0,-90,360,90", WIDTH=1440, HEIGHT=720)
                    filename = name + ".vic"
                    urls = []
                    record["files"].append(download(s, v["url"], target_dir / filename,
                                                    cap, params=params))
                else:
                    urls += v["companions"]
                    for url in urls:
                        filename = url.rstrip("/").split("/")[-1]
                        print("  Descargando", filename, flush=True)
                        record["files"].append(download(s, url, target_dir / filename, cap))
            print("  OK")
        except (requests.RequestException, OSError, ValueError, json.JSONDecodeError) as e:
            failed += 1
            record["status"] = "error"
            record["error"] = str(e)
            print("  ERROR:", e, file=sys.stderr)
        manifest["products"].append(record)
        root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    print(f"\nFinalizado. OK={len(names)-failed}, errores={failed}")
    print("Manifest:", root / "manifest.json")
    print("IMPORTANTE: datos crudos. Calcular pendientes, riesgos y rutas en otro ETL.")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())