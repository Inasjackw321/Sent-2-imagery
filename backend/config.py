"""Static configuration: the satellite, band presets, spectral indices, colormaps."""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------

# Element 84's Earth Search is a free, no-signup STAC API in front of the
# Sentinel Cloud-Optimised GeoTIFF archives on AWS Open Data.
STAC_URL = os.environ.get("STAC_URL", "https://earth-search.aws.element84.com/v1")
STAC_COLLECTION = os.environ.get("STAC_COLLECTION", "sentinel-2-l2a")
STAC_COLLECTION_S1 = os.environ.get("STAC_COLLECTION_S1", "sentinel-1-grd")
STAC_COLLECTION_LANDSAT = os.environ.get("STAC_COLLECTION_LANDSAT", "landsat-c2-l2")

# Where each satellite's pixels come from. Sentinel-2 has one obvious home;
# Sentinel-1 does not, and the difference is worth spelling out because it is
# the reason radar needs more than one.
#
# Sentinel-2 L2A on AWS Open Data is anonymous, cloud-optimised and already in
# a map projection: open the URL and read the window you want. Sentinel-1 GRD
# is none of those things by default. The products in the AWS `sentinel-s1-l1c`
# bucket are requester-pays, so an anonymous read is refused outright, and the
# measurement files inside them are georeferenced by ground control points
# rather than by a map transform -- warping one without honouring its GCPs does
# not fail, it quietly produces a smooth smear with no ground in it.
#
# Microsoft's Planetary Computer republishes the same acquisitions as projected
# cloud-optimised GeoTIFFs, readable by anyone who asks its token endpoint for
# a signature -- free, anonymous, no account. So that is tried first for radar,
# with Earth Search kept behind it in case a deployment prefers it or the
# Planetary Computer is unreachable.
SOURCES = {
    "planetary-computer": {
        "key": "planetary-computer",
        "label": "Microsoft Planetary Computer",
        "stac": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "sign": "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}",
        "projected": True,
    },
    "earth-search": {
        "key": "earth-search",
        "label": "Earth Search (AWS Open Data)",
        "stac": STAC_URL,
        "sign": None,
        # Raw GRD measurement files carry ground control points, not a map
        # transform, and live in a requester-pays bucket.
        "projected": False,
    },
}

# Overridable so a deployment that knows better can pin one.
S1_SOURCE = os.environ.get("S1_SOURCE", "").strip()

# The two satellites, in one place. They fly over the same ground and answer
# different questions: Sentinel-2 photographs it in daylight when there is no
# cloud, Sentinel-1 measures it with radar through cloud and at night. Every
# band, composite and index below says which one it belongs to, so the rest of
# the app only ever has to ask.
SATELLITES = {
    "sentinel-2": {
        "key": "sentinel-2",
        "short": "Sentinel-2",
        "label": "Sentinel-2 · surface reflectance",
        "kind": "optical",
        "platform": "Sentinel-2 A/B/C",
        "collection": STAC_COLLECTION,
        "sources": ("earth-search",),
        "resolution": 10,
        "since": "2015-06-23",
        "revisit": "≈5 days",
        # One satellite retraces the same ground track every this many days.
        # Both spacecraft share the track numbering half a cycle apart, which
        # is where the ≈5 day revisit comes from.
        "repeat_days": 10,
        "swath_hint": "10 m visible and near-infrared, 20 m red-edge and short-wave infrared",
        "attribution": "Contains modified Copernicus Sentinel data",
        "provider": "Earth Search (AWS Open Data)",
        "notes": "Level-2A surface reflectance, atmospherically corrected.",
        "units": "surface reflectance",
        "default_composite": "true_color",
        "cloud_filter": True,
        # Sentinel-2 samples at 10 m with a footprint about that wide, so
        # detail genuinely hides between its samples and merging dates can
        # recover it.
        "can_superres": True,
        "colour": "#4cc2ff",
    },
    "sentinel-1": {
        "key": "sentinel-1",
        "short": "Sentinel-1",
        "label": "Sentinel-1 · radar backscatter",
        "kind": "radar",
        "platform": "Sentinel-1 A/C",
        "collection": STAC_COLLECTION_S1,
        # Tried in order until one answers: see SOURCES above for why radar
        # needs more than one and optical does not.
        "sources": ("planetary-computer", "earth-search"),
        # GRD is delivered on a 10 m grid but resolves about 20 m: the grid is
        # finer than the radar's own detail, not the other way round.
        "resolution": 20,
        "pixel_spacing": 10,
        "since": "2014-10-03",
        "revisit": "≈6–12 days",
        "repeat_days": 12,
        "swath_hint": "C-band radar, VV and VH polarisation, 10 m pixels at ~20 m detail",
        "attribution": "Contains modified Copernicus Sentinel data",
        "provider": "Earth Search (AWS Open Data)",
        "notes": "Ground Range Detected amplitude, shown in decibels. Sees through "
                 "cloud and works at night; not radiometrically terrain-corrected.",
        "units": "dB (uncalibrated amplitude)",
        "default_composite": "radar_color",
        "cloud_filter": False,
        # The 10 m grid over-samples a 20 m resolution cell, so nothing is
        # hiding between the samples and merging dates cannot sharpen. What it
        # does do -- and radar needs badly -- is average the speckle away.
        "can_superres": False,
        "colour": "#ffb347",
    },
    # Landsat is the long memory. Sentinel-2 starts in 2015; Landsat has been
    # photographing the same ground since 1982, which is the difference between
    # seeing a place change and seeing what it used to be.
    "landsat": {
        "key": "landsat",
        "short": "Landsat",
        "label": "Landsat 8/9 · surface reflectance",
        "kind": "optical",
        "platform": "Landsat 8 and 9",
        "collection": STAC_COLLECTION_LANDSAT,
        "sources": ("planetary-computer", "earth-search"),
        "resolution": 30,
        "since": "1982-08-22",
        "revisit": "≈8 days",
        # One satellite repeats its ground track every 16 days; the two are
        # offset by eight, which is where the ≈8 day revisit comes from.
        "repeat_days": 16,
        "swath_hint": "30 m visible, near-infrared and short-wave infrared; 100 m thermal",
        "attribution": "Contains USGS Landsat data",
        "provider": "USGS / NASA, via the Planetary Computer",
        "notes": "Collection 2 Level-2 surface reflectance. Coarser than "
                 "Sentinel-2 but reaches back four decades, and carries a "
                 "thermal band that Sentinel-2 has nothing to match.",
        "units": "surface reflectance",
        "default_composite": "true_color",
        "cloud_filter": True,
        # Landsat products are resampled onto a fixed grid, so every date lands
        # on the same pixel boundaries. There is no sub-pixel diversity between
        # passes for merging to exploit -- unlike Sentinel-2, where there is.
        "can_superres": False,
        # Collection 2 Level-2 stores reflectance scaled and shifted, rather
        # than Sentinel-2's plain ten-thousandths.
        "scale": 0.0000275,
        "offset": -0.2,
        "colour": "#8fd98f",
    },
}

DEFAULT_SATELLITE = "sentinel-2"

# Kept as the plain name for the satellite the app opens on, so anything that
# only ever cared about Sentinel-2 still reads the same.
SATELLITE = SATELLITES[DEFAULT_SATELLITE]


def satellite(key: str | None = None) -> dict:
    """The satellite record for a key, defaulting to Sentinel-2."""
    return SATELLITES.get(key or DEFAULT_SATELLITE, SATELLITES[DEFAULT_SATELLITE])


def satellite_for_collection(collection: str | None) -> str:
    """Which satellite a STAC collection belongs to."""
    for key, spec in SATELLITES.items():
        if spec["collection"] == collection:
            return key
    return DEFAULT_SATELLITE

# Synthetic imagery instead of live downloads. Everything the app produces in
# this mode is flagged as fake -- it exists so the UI can be exercised offline.
DEMO_MODE = os.environ.get("DEMO_MODE", "0").lower() in ("1", "true", "yes", "on")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Sent-2-imagery/1.0 (https://github.com/Inasjackw321/Sent-2-imagery)"

# GDAL tuning for reading COGs over HTTP range requests.
# Numeric options must be passed to rasterio.Env as ints, and the two cache
# sizes are in bytes -- a string here raises "an integer is required".
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    # No extension allowlist: a signed URL ends in its signature rather than in
    # .tif, and an allowlist would refuse to open one.
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

# How much finer a merge samples, given the number of dates it has to work
# with: (dates needed, multiplier), most demanding first. More dates mean more
# differently-phased looks at the same ground, and so more detail that can
# honestly be recovered -- nobody should have to work that out for themselves,
# so the app picks from this table and says what it picked.
SUPERRES_STEPS = ((9, 4), (5, 3), (2, 2))

# Sentinel-2 scenes processed with baseline 04.00+ carry a -1000 DN offset.
BOA_OFFSET_DATE = "2022-01-25"
BOA_OFFSET = -1000
REFLECTANCE_SCALE = 1e-4

# ---------------------------------------------------------------------------
# Bands. The key is the name used everywhere in the app; "asset" is what the
# band is called in the catalogue, and "s2" is its name on the satellite.
# "sat" says which satellite carries it -- there is one table because a render
# only ever draws on one satellite's bands, and tagging is cheaper than two.
# ---------------------------------------------------------------------------

BANDS = {
    "coastal": {"asset": "coastal", "s2": "B01", "res": 60, "nm": 443,
                "label": "Coastal aerosol"},
    "blue": {"asset": "blue", "s2": "B02", "res": 10, "nm": 490, "label": "Blue"},
    "green": {"asset": "green", "s2": "B03", "res": 10, "nm": 560, "label": "Green"},
    "red": {"asset": "red", "s2": "B04", "res": 10, "nm": 665, "label": "Red"},
    "rededge1": {"asset": "rededge1", "s2": "B05", "res": 20, "nm": 705,
                 "label": "Red edge 1"},
    "rededge2": {"asset": "rededge2", "s2": "B06", "res": 20, "nm": 740,
                 "label": "Red edge 2"},
    "rededge3": {"asset": "rededge3", "s2": "B07", "res": 20, "nm": 783,
                 "label": "Red edge 3"},
    "nir": {"asset": "nir", "s2": "B08", "res": 10, "nm": 842, "label": "NIR"},
    "nir08": {"asset": "nir08", "s2": "B8A", "res": 20, "nm": 865, "label": "NIR narrow"},
    "nir09": {"asset": "nir09", "s2": "B09", "res": 60, "nm": 945,
              "label": "Water vapour"},
    "swir16": {"asset": "swir16", "s2": "B11", "res": 20, "nm": 1610, "label": "SWIR 1"},
    "swir22": {"asset": "swir22", "s2": "B12", "res": 20, "nm": 2190, "label": "SWIR 2"},
    # Sentinel-1. Backscatter, not reflectance: how much of the radar pulse the
    # ground sent back. VV bounces off flat and rough surfaces alike, VH only
    # comes back from things that scatter in a volume -- foliage, mostly -- so
    # the pair separates vegetation from bare ground and water on its own.
    "vv": {"asset": "vv", "res": 10, "label": "VV backscatter",
           "sat": "sentinel-1", "unit": "dB"},
    "vh": {"asset": "vh", "res": 10, "label": "VH backscatter",
           "sat": "sentinel-1", "unit": "dB"},
    # Not a band the satellite measures: the difference between the two, which
    # in decibels is their ratio. Read as one so a composite can use it.
    "vvvh": {"derive": ("vv", "vh"), "res": 10, "label": "VV − VH ratio",
             "sat": "sentinel-1", "unit": "dB"},
}

# Landsat carries most of the same wavelengths under different asset names, so
# a band says where it lives on each satellite that has it rather than being
# duplicated per mission.
_LANDSAT_ASSETS = {
    "coastal": "coastal", "blue": "blue", "green": "green", "red": "red",
    # Landsat has no 10 m broad NIR: its near-infrared is the narrow one, which
    # is what "nir" resolves to when the satellite is Landsat.
    "nir": "nir08", "nir08": "nir08",
    "swir16": "swir16", "swir22": "swir22",
}

for _name, _band in BANDS.items():
    _band.setdefault("sat", ["sentinel-2"])
    if isinstance(_band["sat"], str):
        _band["sat"] = [_band["sat"]]
    if _name in _LANDSAT_ASSETS and "sentinel-2" in _band["sat"]:
        _band["sat"].append("landsat")
        _band.setdefault("assets", {})["landsat"] = _LANDSAT_ASSETS[_name]

# Landsat's thermal band has no Sentinel-2 counterpart at all: nothing on
# Sentinel-2 measures emitted heat.
BANDS["lwir11"] = {
    # Sampled onto the 30 m grid, but the sensor itself resolves about 100 m.
    "asset": "lwir11", "res": 100, "nm": 10900, "label": "Surface temperature",
    "sat": ["landsat"], "unit": "K",
    # Not reflectance, so not the satellite's reflectance scaling. Collection 2
    # stores surface temperature in kelvin under its own scale and offset, and
    # applying the wrong one would turn 300 K into a number near zero without
    # anything looking broken.
    "scale": {"landsat": 0.00341802},
    "offset": {"landsat": 149.0},
}

SCL_ASSET = "scl"

# Sentinel-1 GRD stores amplitude as raw digital numbers. Squared it is power,
# and ten times its log is the decibel figure every radar image is shown in.
S1_FLOOR_DN = 1.0        # a DN of zero is no data, not silence

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

    # Sentinel-1. Radar has no colour of its own, so these are the conventional
    # ways of giving it one -- and each channel gets a fixed decibel window
    # rather than a percentile.
    #
    # That is not a stylistic choice, it is the difference between a radar
    # picture and a kaleidoscope. VV and VH measure the same ground twice and
    # agree to within about a percent, so red and green move together; all the
    # colour is carried by the ratio, whose real spread is only two or three
    # decibels. Stretch each channel to its own percentiles and that two-decibel
    # wiggle is amplified to full scale, collapsing every scene onto one garish
    # red-to-cyan axis and inventing structure out of noise. Fixed windows keep
    # the channels in their true proportions, so water comes out black, towns
    # white, vegetation green -- and two dates are comparable.
    "radar_color": {
        "label": "Radar colour",
        "sat": "sentinel-1",
        "bands": ["vv", "vh", "vvvh"],
        "hint": "The standard radar false colour. Towns white, vegetation green, water black.",
        "default_stretch": {"mode": "fixed", "gamma": 1.0},
        # Linear power, not decibels -- see composite.from_decibels. VH returns
        # roughly a quarter of what VV does, and the ratio is a ratio, so all
        # three windows differ.
        "from_db": True,
        "windows": [[0.0, 0.35], [0.0, 0.08], [1.0, 8.0]],
    },
    "radar_grey": {
        "label": "Radar (VV only)",
        "sat": "sentinel-1",
        "bands": ["vv", "vv", "vv"],
        "hint": "Plain backscatter. Bright is rough or metal, black is smooth water.",
        "default_stretch": {"mode": "fixed", "gamma": 1.0},
        "from_db": True,
        "windows": [[0.0, 0.35]] * 3,
    },
    # The interference view, and the reason it is a picture rather than a
    # number. Ground radars transmitting in Sentinel-1's band put energy
    # straight into the receiver, and it lands hardest on VH -- the channel
    # whose genuine return is weakest. So VV is sent to red and blue and VH
    # to green: ordinary ground, with almost no cross-polarised return, comes
    # out violet, and a streak of interference lifts the green until the band
    # blazes white across the swath.
    "radar_interference": {
        "label": "Radar interference",
        "sat": ["sentinel-1"],
        "bands": ["vv", "vh", "vv"],
        "hint": "Violet is ordinary ground. Bright bands across the swath are "
                "a ground radar transmitting in Sentinel-1's band.",
        "default_stretch": {"mode": "fixed", "gamma": 1.0},
        "from_db": True,
        # The green window is the one that matters, and it is set from what
        # the two channels actually read. Cross-polarised return from ordinary
        # ground runs about -22 dB over bare soil to -13 dB over forest, which
        # is 0.006 to 0.045 in power; interference lands nearer -10 dB, which
        # is 0.1. Topping green out at 0.15 leaves forest a dark tint and lets
        # a streak saturate. Tighter than that -- an earlier try used 0.045 --
        # and every field saturates too, and the whole scene comes out green
        # with nothing standing out of it.
        "windows": [[0.0, 0.30], [0.0, 0.15], [0.0, 0.18]],
    },
    "radar_water": {
        "label": "Radar water & flood",
        "sat": "sentinel-1",
        "bands": ["vh", "vv", "vv"],
        "hint": "Cross-polarised first: still water goes to near black, so floods stand out.",
        "default_stretch": {"mode": "fixed", "gamma": 1.0},
        "from_db": True,
        "windows": [[0.0, 0.06], [0.0, 0.35], [0.0, 0.35]],
    },
}

for _name, _preset in COMPOSITES.items():
    _preset.setdefault("sat", ["sentinel-2"])
    if isinstance(_preset["sat"], str):
        _preset["sat"] = [_preset["sat"]]
    # An optical preset works on Landsat too when every band it asks for is
    # one Landsat carries -- which is checked here rather than assumed.
    if "sentinel-2" in _preset["sat"] and all(
            "landsat" in BANDS[_b]["sat"] for _b in _preset["bands"]):
        _preset["sat"].append("landsat")

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
    "savi": {
        "label": "SAVI - soil adjusted",
        "bands": ["nir", "red"],
        "formula": "1.5 * (nir - red) / (nir + red + 0.5)",
        "range": [-0.2, 1.0],
        "colormap": "ndvi",
        "hint": "Vegetation over sparse or bright soils.",
    },
    "radar_ratio": {
        "label": "VV/VH - radar ratio",
        "sat": "sentinel-1",
        "bands": ["vv", "vh"],
        "formula": "vv - vh  (decibels, so a ratio)",
        "range": [0.0, 14.0],
        "colormap": "magma",
        "hint": "Low where the ground scatters in a volume: forest, dense crops.",
    },
}

# Radio-frequency interference in the radar.
#
# Sentinel-1 listens on C-band, and it is not the only thing transmitting
# there. Ground radars -- air surveillance, naval, some marine sets -- put
# energy straight into the satellite's receiver, which is a very different
# thing from the ground scattering a pulse back. It arrives without having
# made the round trip, so it lands far above what the surface itself returns
# and shows up as long bright streaks across the swath.
#
# The cross-polarised channel is where to look. Genuine VH return is weak,
# so interference stands proudest against it -- and over water, where real
# backscatter is near the noise floor, a streak is unmistakable.
INDICES["rfi"] = {
    # Named apart from the composite of the same subject. That one is the
    # picture you look at; this is the number that says how strongly, and
    # having both called "Radar interference" in one menu helped nobody.
    "label": "Interference strength",
    "bands": ["vh", "vv"],
    "formula": "how far VH stands above its own surroundings, where the "
               "brightness runs in a line",
    "range": [0.0, 12.0],
    "colormap": "inferno",
    "hint": "How far a streak stands above the ground around it, in decibels. "
            "Use the interference view to see them; use this to measure one.",
    "sat": ["sentinel-1"],
    "unit": "dB above local",
}

# Not a ratio like the rest: a measurement in its own units, shown in degrees
# rather than kelvin because nobody thinks in kelvin about a car park.
INDICES["surface_temp"] = {
    "label": "Surface temperature",
    "bands": ["lwir11"],
    "formula": "lwir11 - 273.15",
    "range": [-5.0, 55.0],
    "colormap": "inferno",
    "hint": "How hot the ground itself is, in °C. Cities read far above the "
            "fields around them.",
    "sat": ["landsat"],
    "unit": "°C",
}

for _name, _index in INDICES.items():
    _index.setdefault("sat", ["sentinel-2"])
    if isinstance(_index["sat"], str):
        _index["sat"] = [_index["sat"]]
    if "sentinel-2" in _index["sat"] and all(
            "landsat" in BANDS[_b]["sat"] for _b in _index["bands"]):
        _index["sat"].append("landsat")

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
}
