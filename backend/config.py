"""Static configuration: data source, band presets, spectral indices, colormaps."""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------

# Element 84's Earth Search is a free, no-signup STAC API in front of the
# Sentinel-2 Cloud-Optimised GeoTIFF archive on AWS Open Data.
STAC_URL = os.environ.get("STAC_URL", "https://earth-search.aws.element84.com/v1")
STAC_COLLECTION = os.environ.get("STAC_COLLECTION", "sentinel-2-l2a")

# Synthetic imagery instead of live downloads. Everything the app produces in
# this mode is watermarked and flagged as fake -- it exists so the UI can be
# exercised offline.
DEMO_MODE = os.environ.get("DEMO_MODE", "0").lower() in ("1", "true", "yes", "on")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Sent-2-imagery/1.0 (https://github.com/Inasjackw321/Sent-2-imagery)"

# GDAL tuning for reading COGs over HTTP range requests.
# Numeric options must be passed to rasterio.Env as ints, and the two cache
# sizes are in bytes -- a string here raises "an integer is required".
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_MAX_RETRY": 3,
    "GDAL_HTTP_RETRY_DELAY": 1,
    "VSI_CACHE": True,
    "VSI_CACHE_SIZE": 64 * 1024 * 1024,
    "GDAL_CACHEMAX": 512 * 1024 * 1024,
    "AWS_NO_SIGN_REQUEST": True,
}

MIN_SIZE, MAX_SIZE = 128, 4096
DEFAULT_SIZE = 1024

# Multi-frame super-resolution: how many times finer than the requested grid
# the fusion may sample. Past 4x there are never enough repeat passes of the
# same ground to support the extra pixels, so the limit is honest rather than
# arbitrary. The fused grid is still capped at MAX_SIZE.
MAX_SUPERRES = 4

# Sentinel-2 scenes processed with baseline 04.00+ carry a -1000 DN offset.
BOA_OFFSET_DATE = "2022-01-25"
BOA_OFFSET = -1000
REFLECTANCE_SCALE = 1e-4

# ---------------------------------------------------------------------------
# Bands. Keys are Earth Search asset names; values describe the physical band.
# ---------------------------------------------------------------------------

BANDS = {
    "coastal": {"s2": "B01", "res": 60, "nm": 443, "label": "Coastal aerosol"},
    "blue": {"s2": "B02", "res": 10, "nm": 490, "label": "Blue"},
    "green": {"s2": "B03", "res": 10, "nm": 560, "label": "Green"},
    "red": {"s2": "B04", "res": 10, "nm": 665, "label": "Red"},
    "rededge1": {"s2": "B05", "res": 20, "nm": 705, "label": "Red edge 1"},
    "rededge2": {"s2": "B06", "res": 20, "nm": 740, "label": "Red edge 2"},
    "rededge3": {"s2": "B07", "res": 20, "nm": 783, "label": "Red edge 3"},
    "nir": {"s2": "B08", "res": 10, "nm": 842, "label": "NIR"},
    "nir08": {"s2": "B8A", "res": 20, "nm": 865, "label": "NIR narrow"},
    "nir09": {"s2": "B09", "res": 60, "nm": 945, "label": "Water vapour"},
    "swir16": {"s2": "B11", "res": 20, "nm": 1610, "label": "SWIR 1"},
    "swir22": {"s2": "B12", "res": 20, "nm": 2190, "label": "SWIR 2"},
    # Bands that other satellites bring to the table.
    "cirrus": {"s2": "B10", "res": 60, "nm": 1375, "label": "Cirrus"},
    "pan": {"s2": None, "res": 15, "nm": 590, "label": "Panchromatic"},
    "lwir": {"s2": None, "res": 100, "nm": 10900, "label": "Thermal infrared"},
    "vv": {"s2": None, "res": 10, "nm": None, "label": "Radar VV"},
    "vh": {"s2": None, "res": 10, "nm": None, "label": "Radar VH"},
    "hh": {"s2": None, "res": 10, "nm": None, "label": "Radar HH"},
    "hv": {"s2": None, "res": 10, "nm": None, "label": "Radar HV"},
    "ratio": {"s2": None, "res": 10, "nm": None, "label": "VV / VH ratio"},
    "elevation": {"s2": None, "res": 30, "nm": None, "label": "Elevation"},
    "classification": {"s2": None, "res": 10, "nm": None, "label": "Land-cover class"},
}

SCL_ASSET = "scl"

# Scene classification layer classes worth masking out.
SCL_CLASSES = {
    0: "No data",
    1: "Saturated / defective",
    2: "Dark area pixels",
    3: "Cloud shadows",
    4: "Vegetation",
    5: "Bare soil",
    6: "Water",
    7: "Unclassified",
    8: "Cloud medium probability",
    9: "Cloud high probability",
    10: "Thin cirrus",
    11: "Snow / ice",
}
CLOUD_CLASSES = (3, 8, 9, 10)
SNOW_CLASS = 11

# ---------------------------------------------------------------------------
# RGB band combinations
# ---------------------------------------------------------------------------

COMPOSITES = {
    "true_color": {
        "label": "True colour",
        "bands": ["red", "green", "blue"],
        "hint": "What the eye would see. Best all-round starting point.",
        "default_stretch": {"mode": "fixed", "vmin": 0.0, "vmax": 0.30, "gamma": 1.15},
    },
    "false_color": {
        "label": "Colour infrared",
        "bands": ["nir", "red", "green"],
        "hint": "Healthy vegetation glows red. Classic for plant vigour.",
        "default_stretch": {"mode": "fixed", "vmin": 0.0, "vmax": 0.45, "gamma": 1.1},
    },
    "agriculture": {
        "label": "Agriculture",
        "bands": ["swir16", "nir", "blue"],
        "hint": "Crop types and field boundaries stand out.",
        "default_stretch": {"mode": "percentile_linked", "low": 2, "high": 98, "gamma": 1.05},
    },
    "urban": {
        "label": "Urban / built-up",
        "bands": ["swir22", "swir16", "red"],
        "hint": "Concrete and bare rock separate from vegetation.",
        "default_stretch": {"mode": "percentile_linked", "low": 2, "high": 98, "gamma": 1.05},
    },
    "geology": {
        "label": "Geology",
        "bands": ["swir22", "swir16", "blue"],
        "hint": "Lithology, faults and mineral alteration.",
        "default_stretch": {"mode": "percentile_linked", "low": 2, "high": 98, "gamma": 1.05},
    },
    "healthy_vegetation": {
        "label": "Healthy vegetation",
        "bands": ["nir", "swir16", "blue"],
        "hint": "Vegetation stress and moisture at a glance.",
        "default_stretch": {"mode": "percentile_linked", "low": 2, "high": 98, "gamma": 1.05},
    },
    "swir": {
        "label": "Short-wave infrared",
        "bands": ["swir22", "nir08", "red"],
        "hint": "Burn scars, active fire and soil moisture.",
        "default_stretch": {"mode": "percentile_linked", "low": 2, "high": 98, "gamma": 1.05},
    },
    "bathymetric": {
        "label": "Bathymetric",
        "bands": ["red", "green", "coastal"],
        "hint": "Shallow sea-floor and sediment plumes.",
        "default_stretch": {"mode": "fixed", "vmin": 0.0, "vmax": 0.18, "gamma": 1.2},
    },
    "atmospheric": {
        "label": "Atmospheric penetration",
        "bands": ["swir22", "swir16", "nir08"],
        "hint": "Sees through haze; no visible light at all.",
        "default_stretch": {"mode": "percentile_linked", "low": 2, "high": 98, "gamma": 1.05},
    },
    "radar": {
        "label": "Radar (VV / VH)",
        "bands": ["vv", "vh", "ratio"],
        "hint": "Radar backscatter: smooth water is dark, buildings are bright.",
        "default_stretch": {"mode": "percentile", "low": 2, "high": 98, "gamma": 1.0},
    },
    "radar_water": {
        "label": "Radar flood view",
        "bands": ["vh", "vv", "vv"],
        "hint": "Tuned for open water and flooding under cloud.",
        "default_stretch": {"mode": "percentile", "low": 1, "high": 99, "gamma": 1.0},
    },
}

# Channels that are computed from other bands rather than read from a file.
DERIVED_BANDS = {"ratio": ("vv", "vh")}

# ---------------------------------------------------------------------------
# Spectral indices
# ---------------------------------------------------------------------------

INDICES = {
    "ndvi": {
        "label": "NDVI - vegetation",
        "bands": ["nir", "red"],
        "formula": "(nir - red) / (nir + red)",
        "range": [-0.2, 0.9],
        "colormap": "ndvi",
        "hint": "Green biomass and vigour.",
    },
    "ndwi": {
        "label": "NDWI - water",
        "bands": ["green", "nir"],
        "formula": "(green - nir) / (green + nir)",
        "range": [-0.6, 0.6],
        "colormap": "water",
        "hint": "Open water extent and flooding.",
    },
    "ndmi": {
        "label": "NDMI - moisture",
        "bands": ["nir", "swir16"],
        "formula": "(nir - swir16) / (nir + swir16)",
        "range": [-0.5, 0.6],
        "colormap": "brbg",
        "hint": "Canopy water content, drought stress.",
    },
    "ndbi": {
        "label": "NDBI - built-up",
        "bands": ["swir16", "nir"],
        "formula": "(swir16 - nir) / (swir16 + nir)",
        "range": [-0.5, 0.5],
        "colormap": "inferno",
        "hint": "Impervious surfaces and urban growth.",
    },
    "nbr": {
        "label": "NBR - burn ratio",
        "bands": ["nir", "swir22"],
        "formula": "(nir - swir22) / (nir + swir22)",
        "range": [-0.5, 0.9],
        "colormap": "rdylgn",
        "hint": "Fire severity; difference two dates for dNBR.",
    },
    "ndsi": {
        "label": "NDSI - snow",
        "bands": ["green", "swir16"],
        "formula": "(green - swir16) / (green + swir16)",
        "range": [-0.5, 0.9],
        "colormap": "blues",
        "hint": "Snow and ice cover.",
    },
    "evi": {
        "label": "EVI - enhanced vegetation",
        "bands": ["nir", "red", "blue"],
        "formula": "2.5 * (nir - red) / (nir + 6*red - 7.5*blue + 1)",
        "range": [-0.2, 1.0],
        "colormap": "ndvi",
        "hint": "Like NDVI but resists soil and haze effects.",
    },
    "surface_temp": {
        "label": "Surface temperature",
        "bands": ["lwir"],
        "formula": "thermal band, kelvin",
        "range": [280, 320],
        "colormap": "inferno",
        "hint": "Ground temperature in kelvin — urban heat and fire scars.",
        "unit": "K",
    },
    "elevation": {
        "label": "Elevation",
        "bands": ["elevation"],
        "formula": "height above sea level",
        "range": [0, 1500],
        "colormap": "terrain",
        "hint": "Terrain height in metres.",
        "unit": "m",
    },
    "backscatter": {
        "label": "Radar backscatter (VV)",
        "bands": ["vv"],
        "formula": "10·log10(VV)",
        "range": [-25, 0],
        "colormap": "gray",
        "hint": "Single-polarisation radar brightness in decibels.",
        "unit": "dB",
    },
    "savi": {
        "label": "SAVI - soil adjusted",
        "bands": ["nir", "red"],
        "formula": "1.5 * (nir - red) / (nir + red + 0.5)",
        "range": [-0.2, 1.0],
        "colormap": "ndvi",
        "hint": "Vegetation over sparse or bright soils.",
    },
}

# ---------------------------------------------------------------------------
# Colormaps: control points as (position, r, g, b)
# ---------------------------------------------------------------------------

COLORMAPS = {
    "ndvi": [
        (0.00, 12, 60, 120), (0.28, 168, 140, 96), (0.42, 214, 200, 140),
        (0.58, 150, 190, 100), (0.78, 52, 140, 52), (1.00, 8, 68, 24),
    ],
    "rdylgn": [
        (0.00, 165, 0, 38), (0.25, 244, 109, 67), (0.50, 255, 255, 191),
        (0.75, 102, 189, 99), (1.00, 0, 104, 55),
    ],
    "water": [
        (0.00, 120, 90, 40), (0.42, 232, 222, 192), (0.56, 120, 190, 230),
        (1.00, 5, 30, 90),
    ],
    "viridis": [
        (0.00, 68, 1, 84), (0.13, 72, 40, 120), (0.25, 62, 74, 137),
        (0.38, 49, 104, 142), (0.50, 38, 130, 142), (0.63, 31, 158, 137),
        (0.75, 53, 183, 121), (0.88, 109, 205, 89), (1.00, 253, 231, 37),
    ],
    "magma": [
        (0.00, 0, 0, 4), (0.13, 28, 16, 68), (0.25, 79, 18, 123),
        (0.38, 129, 37, 129), (0.50, 181, 54, 122), (0.63, 229, 80, 100),
        (0.75, 251, 135, 97), (0.88, 254, 194, 135), (1.00, 252, 253, 191),
    ],
    "inferno": [
        (0.00, 0, 0, 4), (0.25, 87, 16, 110), (0.50, 188, 55, 84),
        (0.75, 249, 142, 9), (1.00, 252, 255, 164),
    ],
    "turbo": [
        (0.000, 48, 18, 59), (0.125, 70, 107, 227), (0.250, 40, 175, 235),
        (0.375, 66, 228, 147), (0.500, 143, 251, 63), (0.625, 215, 224, 40),
        (0.750, 251, 151, 32), (0.875, 224, 74, 7), (1.000, 122, 4, 3),
    ],
    "spectral": [
        (0.00, 158, 1, 66), (0.25, 253, 174, 97), (0.50, 255, 255, 191),
        (0.75, 102, 194, 165), (1.00, 94, 79, 162),
    ],
    "blues": [
        (0.00, 247, 251, 255), (0.50, 107, 174, 214), (1.00, 8, 48, 107),
    ],
    "brbg": [
        (0.00, 84, 48, 5), (0.25, 191, 129, 45), (0.50, 245, 245, 245),
        (0.75, 53, 151, 143), (1.00, 0, 60, 48),
    ],
    "rdbu": [
        (0.00, 103, 0, 31), (0.25, 214, 96, 77), (0.50, 247, 247, 247),
        (0.75, 67, 147, 195), (1.00, 5, 48, 97),
    ],
    "gray": [(0.00, 0, 0, 0), (1.00, 255, 255, 255)],
    "terrain": [
        (0.00, 26, 82, 118), (0.06, 60, 130, 90), (0.20, 118, 168, 92),
        (0.42, 200, 190, 120), (0.62, 160, 128, 96), (0.82, 130, 110, 105),
        (1.00, 250, 250, 252),
    ],
    # Diverging map for change detection: loss (red) - stable - gain (green).
    "change": [
        (0.00, 120, 10, 20), (0.25, 214, 96, 77), (0.50, 245, 245, 240),
        (0.75, 90, 175, 90), (1.00, 10, 90, 30),
    ],
}
