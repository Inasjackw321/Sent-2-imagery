"""Everything a change map can be saved as, beyond the original picture and
spot list: GeoJSON for any GIS, a GeoTIFF of the change itself, and one zip
holding the lot with a note saying what each file is."""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np

from . import export, render
from .engine import CRS, Result


def spots_geojson(result: Result, det) -> str:
    """Each detected spot as a point, with its size, change and dates."""
    found = render.detect(result.signed, result.change_db, det, len(result.params.bands))
    features = []
    for s in render.find_spots(result, found):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(s.lon, 6), round(s.lat, 6)]},
            "properties": {"area_m2": round(s.area_m2), "change_db": round(s.change_db, 2),
                           "direction": "decrease" if s.change_db < 0 else "increase",
                           "times_changed": s.times, "when": s.when},
        })
    p = result.params
    return json.dumps({"type": "FeatureCollection", "features": features,
                       "properties": {"label": p.label, "start": p.start, "end": p.end,
                                      "before": result.before_days, "after": result.after_days,
                                      "source": export.SENTINEL_CREDIT}}, indent=1)


def change_geotiff(result: Result, det) -> bytes:
    """A two-band GeoTIFF on the analysis's own 10 m grid (EPSG:3857):
    band 1 what was detected (1 decrease, 2 increase, 0 nothing, NaN no data),
    band 2 the change in radar signal, in dB."""
    import rasterio
    from rasterio.transform import Affine

    g = result.grid
    found = render.detect(result.signed, result.change_db, det, len(result.params.bands))
    valid = result.signed != -32768
    kind = np.zeros(result.signed.shape, np.float32)
    kind[found.decreased] = 1
    kind[found.increased] = 2
    kind[~valid] = np.nan
    db = np.where(valid, result.change_db.astype(np.float32) / 100, np.nan).astype(np.float32)
    with rasterio.MemoryFile() as mem:
        with mem.open(driver="GTiff", width=g.width, height=g.height, count=2, dtype="float32",
                      crs=CRS, transform=Affine(g.scale, 0, g.x0, 0, -g.scale, g.y0),
                      nodata=np.nan, compress="deflate", tiled=True) as dst:
            dst.write(kind, 1)
            dst.write(db, 2)
            dst.set_band_description(1, "detected change: 1 decrease, 2 increase, 0 none")
            dst.set_band_description(2, "change in radar signal (dB)")
            dst.update_tags(label=result.params.label, start=result.params.start,
                            end=result.params.end, credit=export.SENTINEL_CREDIT)
        return mem.read()


README = """Change map: {label}
Radar change {start} to {end}
Before images: {before}
After images: {after}
Detection: {det}

Files
  {stem}.png          the map, with numbered spots, legend, scale bar and credits
  {stem}_spots.csv    every changed spot: location, size, change in dB, times changed, dates
  {stem}_spots.geojson  the same spots as points, for QGIS, Google Earth or any GIS
  {stem}_change.tif   GeoTIFF on the 10 m analysis grid: band 1 = 1 decrease / 2 increase,
                      band 2 = change in radar signal in dB

Red: radar signal decreased. Cyan: increased. A flagged spot means the radar
signal changed, not necessarily damage.

{credit}
"""


def bundle(result: Result, name: str, det, stem: str, picture: bytes) -> bytes:
    """One zip of every file, with a note saying what each is."""
    p = result.params
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{stem}.png", picture)
        zf.writestr(f"{stem}_spots.csv", export.spots_csv(result, det))
        zf.writestr(f"{stem}_spots.geojson", spots_geojson(result, det))
        zf.writestr(f"{stem}_change.tif", change_geotiff(result, det))
        zf.writestr("README.txt", README.format(
            label=p.label, start=p.start, end=p.end, stem=stem,
            before=", ".join(result.before_days), after=", ".join(result.after_days),
            det=f"{name} (≥{det.min_db:g} dB, ≥{det.min_area_m2:,.0f} m², noise chance "
                f"< 1 in {1 / det.alpha:,.0f})",
            credit=export.SENTINEL_CREDIT))
    return buf.getvalue()
