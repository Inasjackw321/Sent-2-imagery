"""Active fire, found pixel by pixel in Sentinel-2 and Landsat imagery.

The satellites built for fire -- Meteosat every ten minutes, VIIRS at 375 m --
see a fire as part of a pixel a few hundred metres to kilometres across. Sentinel-2
(20 m) and Landsat (30 m) see the burning ground itself, a few times a week,
in daylight and clear sky. When they have a pass, they are the most exact
picture of where a fire is that free data offers.

How a fire shows: burning vegetation is at 800-1200 K, and a body that hot
radiates strongly at 2.2 microns (SWIR 2), less at 1.6 (SWIR 1), and hardly at
all at 0.86 (near infrared). Ordinary ground does nearly the opposite --
vegetation is bright in the near infrared and dark in SWIR. So the ratio of
SWIR 2 to near infrared, which is well under 1 almost everywhere, jumps far
above it over flames. That is what the tests below look for.

The tests are those of Schroeder et al. (2016), "Active fire detection using
Landsat-8/OLI data", Remote Sensing of Environment 185, 210-220 -- the
algorithm behind the operational Landsat fire product -- applied to the same
wavelengths on Sentinel-2 (B1, B8A, B11, B12):

  * UNAMBIGUOUS fire: SWIR 2 over two and a half times the near infrared,
    at least 0.3 above it and over 0.5 reflectance; or SWIR 1 so bright
    (over 0.8) that the sensor is near saturation, with the coastal band
    dark (so it is not cloud) and the near infrared either bright or SWIR 2
    folded over -- the signature of the hottest fires.
  * POTENTIAL fire: the same ratios at lower levels (SWIR2/NIR over 1.8,
    0.17 above, SWIR2/SWIR1 over 1.6), kept only where it also stands out
    from the ground around it: the ratio at least 0.8 (or three standard
    deviations) above the neighbourhood's, and SWIR 2 at least 0.08 (or three
    deviations) above. That contextual test is what keeps sun-glinting roofs
    and bright soils out.

The paper tuned these on top-of-atmosphere reflectance; this app reads
surface reflectance. In the short-wave infrared the two differ by a few
hundredths, small against thresholds of 0.17 to 0.8.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

# The bands the tests read, by this app's names.
BANDS = ("coastal", "nir08", "swir16", "swir22", "red")

# The neighbourhood of the contextual test: 61 Landsat pixels, 1.83 km.
CONTEXT_M = 1830.0
LEAST_WINDOW = 15
MOST_WINDOW = 151

# Colours a fire is painted in, over a dimmed short-wave infrared picture.
UNAMBIGUOUS = (255, 238, 120)
POTENTIAL = (255, 130, 0)
HALO = (225, 25, 20)
DIM = 0.55
MOST_CLUSTERS = 12


def _filled(band) -> np.ndarray:
    return np.ma.filled(band.astype("float32"), np.nan)


def window_for(ground_res_m: float) -> int:
    """The contextual window in pixels: odd, about CONTEXT_M across."""
    px = int(round(CONTEXT_M / max(float(ground_res_m), 1e-3)))
    px = min(MOST_WINDOW, max(LEAST_WINDOW, px))
    return px if px % 2 else px + 1


def detect(bands: dict, ground_res_m: float = 20.0) -> dict:
    """Which pixels are burning. Returns boolean arrays 'unambiguous' and
    'potential' (confirmed by context), and 'fire', their union."""
    r1 = _filled(bands["coastal"])
    r4 = _filled(bands["red"])
    r5 = _filled(bands["nir08"])
    r6 = _filled(bands["swir16"])
    r7 = _filled(bands["swir22"])
    valid = np.isfinite(r1) & np.isfinite(r4) & np.isfinite(r5) & np.isfinite(r6) \
        & np.isfinite(r7)
    with np.errstate(divide="ignore", invalid="ignore"):
        r75 = np.where(r5 > 0, r7 / r5, np.inf)
        r76 = np.where(r6 > 0, r7 / r6, np.inf)

        unambiguous = valid & (
            ((r75 > 2.5) & (r7 - r5 > 0.3) & (r7 > 0.5))
            | ((r6 > 0.8) & (r1 < 0.2) & ((r5 > 0.4) | (r7 < 0.1))))
        candidate = valid & ~unambiguous & (r75 > 1.8) & (r7 - r5 > 0.17) & (r76 > 1.6)

    # The ground a candidate is compared with: everything valid that is not
    # itself a fire or candidate, and not water (whose SWIR is near zero and
    # whose ratios are noise).
    water = (r4 > r5) & (r5 > r6) & (r6 > r7)
    ground = valid & ~unambiguous & ~candidate & ~water
    potential = np.zeros_like(candidate)
    if candidate.any():
        size = window_for(ground_res_m)
        weight = ground.astype("float64")
        seen = ndimage.uniform_filter(weight, size=size, mode="nearest")

        def stats(values):
            v = np.where(ground, values, 0.0).astype("float64")
            v = np.where(np.isfinite(v), v, 0.0)
            mean = ndimage.uniform_filter(v, size=size, mode="nearest") / np.maximum(seen, 1e-9)
            sq = ndimage.uniform_filter(v * v, size=size, mode="nearest") / np.maximum(seen, 1e-9)
            return mean, np.sqrt(np.maximum(sq - mean * mean, 0.0))

        m75, s75 = stats(np.where(np.isfinite(r75), r75, 0.0))
        m7, s7 = stats(r7)
        # A neighbourhood with hardly any ground in it says nothing.
        enough = seen > 0.25
        potential = candidate & enough \
            & (r75 > m75 + np.maximum(3 * s75, 0.8)) \
            & (r7 > m7 + np.maximum(3 * s7, 0.08))
    return {"unambiguous": unambiguous, "potential": potential,
            "fire": unambiguous | potential}


def clusters(fire: np.ndarray, r7: np.ndarray | None = None) -> list[dict]:
    """Groups of touching fire pixels (diagonals count), biggest first:
    their centre in pixels, their size, and their brightest SWIR 2."""
    labels, count = ndimage.label(fire, structure=np.ones((3, 3), bool))
    if not count:
        return []
    found = []
    for k, (rows, cols) in enumerate(ndimage.find_objects(labels), start=1):
        member = labels[rows, cols] == k
        ys, xs = np.nonzero(member)
        peak = None
        if r7 is not None:
            vals = r7[rows, cols][member]
            vals = vals[np.isfinite(vals)]
            peak = round(float(vals.max()), 3) if vals.size else None
        found.append({"row": float(ys.mean() + rows.start), "col": float(xs.mean() + cols.start),
                      "pixels": int(member.sum()), "peak_swir22": peak})
    found.sort(key=lambda c: -c["pixels"])
    return found


def paint(rgb: np.ndarray, found: dict) -> np.ndarray:
    """The picture dimmed, fires painted over it: yellow-white for
    unambiguous, orange for potential, a red ring round both so a fire one
    pixel across still shows at the size of the screen."""
    out = (rgb.astype("float32") * DIM).astype("uint8")
    fire = found["fire"]
    halo = ndimage.binary_dilation(fire, structure=np.ones((3, 3), bool), iterations=2) & ~fire
    out[halo] = HALO
    out[found["potential"]] = POTENTIAL
    out[found["unambiguous"]] = UNAMBIGUOUS
    return out


def report(found: dict, bands: dict, grid) -> dict:
    """What was found, for the page: counts, area, and where the biggest are."""
    fire = found["fire"]
    groups = clusters(fire, _filled(bands["swir22"]))
    pixel_ha = grid.ground_res_m ** 2 / 1e4
    where = []
    for c in groups[:MOST_CLUSTERS]:
        x, y = grid.transform * (c["col"] + 0.5, c["row"] + 0.5)
        lon, lat = _to_lonlat(x, y, grid.crs)
        where.append({"lat": round(lat, 5), "lon": round(lon, 5), "pixels": c["pixels"],
                      "area_ha": round(c["pixels"] * pixel_ha, 2),
                      "peak_swir22": c["peak_swir22"]})
    return {
        "pixels": int(fire.sum()),
        "unambiguous": int(found["unambiguous"].sum()),
        "potential": int(found["potential"].sum()),
        "clusters": len(groups),
        "area_ha": round(float(fire.sum()) * pixel_ha, 2),
        "where": where,
        "method": "Schroeder et al. (2016) Landsat-8 active fire tests",
    }


def _to_lonlat(x: float, y: float, crs) -> tuple[float, float]:
    from rasterio.warp import transform
    lons, lats = transform(crs, "EPSG:4326", [x], [y])
    return lons[0], lats[0]
