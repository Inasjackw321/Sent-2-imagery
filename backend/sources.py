"""The satellite catalogue: every freely available source the app can read.

Each entry says where the imagery lives (which STAC API and collection), how to
turn stored numbers into physical units, which asset carries each spectral band,
and how to find the cloudy pixels. Everything downstream -- composites, indices,
enhancement, rendering -- works off the aliases, so adding a satellite is a
matter of adding a row here.

Band aliases used across the app:
    coastal blue green red rededge1 rededge2 rededge3
    nir nir08 swir16 swir22 pan lwir vv vh elevation classification
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Providers ──────────────────────────────────────────────────

PROVIDERS = {
    "earth-search": {
        "label": "Earth Search (AWS Open Data)",
        "url": "https://earth-search.aws.element84.com/v1",
        "signing": None,
        "note": "Free and anonymous.",
    },
    "planetary-computer": {
        "label": "Microsoft Planetary Computer",
        "url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        # Assets need a short-lived token, handed out anonymously and free.
        "signing": "https://planetarycomputer.microsoft.com/api/sas/v1/token",
        "note": "Free; assets are signed with an anonymous token.",
    },
}


# ── Unit conversion ────────────────────────────────────────────
# how stored DNs become physical values


SCALES = {
    # Sentinel-2 L2A/L1C: reflectance x 10000, with a -1000 offset from
    # processing baseline 04.00 onwards (handled per item).
    "sentinel2": {"kind": "sentinel2", "mul": 1e-4, "add": 0.0, "unit": "reflectance"},
    # Landsat Collection 2 Level-2 surface reflectance.
    "landsat_c2_sr": {"kind": "linear", "mul": 2.75e-5, "add": -0.2, "unit": "reflectance"},
    # Landsat Collection 2 Level-2 surface temperature (Kelvin).
    "landsat_c2_st": {"kind": "linear", "mul": 0.00341802, "add": 149.0, "unit": "kelvin"},
    "hls": {"kind": "linear", "mul": 1e-4, "add": 0.0, "unit": "reflectance"},
    "modis": {"kind": "linear", "mul": 1e-4, "add": 0.0, "unit": "reflectance"},
    "byte": {"kind": "linear", "mul": 1 / 255.0, "add": 0.0, "unit": "reflectance"},
    "raw": {"kind": "linear", "mul": 1.0, "add": 0.0, "unit": "value"},
    # SAR amplitude -> decibels.
    "amplitude_db": {"kind": "db", "unit": "dB"},
    "power_db": {"kind": "db", "unit": "dB"},
    "metres": {"kind": "linear", "mul": 1.0, "add": 0.0, "unit": "m"},
}


# ── Cloud masking ──────────────────────────────────────────────

CLOUD_MASKS = {
    # Sentinel-2 scene classification layer.
    "scl": {"kind": "classes", "asset": "scl", "cloudy": (3, 8, 9, 10), "snow": 11},
    # Landsat Collection 2 QA_PIXEL bitfield.
    "qa_pixel": {
        "kind": "bits",
        "asset": "qa_pixel",
        "cloudy_bits": (1, 3, 4),   # dilated cloud, cloud, cloud shadow
        "cirrus_bit": 2,
        "snow_bit": 5,
    },
    # Harmonized Landsat-Sentinel Fmask.
    "fmask": {
        "kind": "bits",
        "asset": "Fmask",
        "cloudy_bits": (1, 2, 3),   # cloud, adjacent, shadow
        "snow_bit": 4,
    },
}


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    provider: str
    collection: str
    platform: str
    kind: str                       # optical | sar | dem | landcover | thermal
    resolution: float               # native ground sample distance, metres
    bands: dict[str, str]           # alias -> asset (optionally "asset:index")
    scale: str = "raw"
    cloud_mask: str | None = None
    cloud_property: str | None = "eo:cloud_cover"
    pan: str | None = None          # alias of a panchromatic band, if any
    pan_resolution: float | None = None
    since: str = "2015-01-01"
    revisit: str = ""
    swath_hint: str = ""
    attribution: str = ""
    default_composite: str = "true_color"
    default_size_hint: int = 1024
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has(self) -> set[str]:
        return set(self.bands)


# ── The catalogue ──────────────────────────────────────────────

_S2_BANDS = {
    "coastal": "coastal", "blue": "blue", "green": "green", "red": "red",
    "rededge1": "rededge1", "rededge2": "rededge2", "rededge3": "rededge3",
    "nir": "nir", "nir08": "nir08", "swir16": "swir16", "swir22": "swir22",
}

_LANDSAT_BANDS = {
    "coastal": "coastal", "blue": "blue", "green": "green", "red": "red",
    "nir": "nir08", "nir08": "nir08", "swir16": "swir16", "swir22": "swir22",
    "pan": "pan", "lwir": "lwir11",
}

_HLS_BANDS = {
    "coastal": "B01", "blue": "B02", "green": "B03", "red": "B04",
    "nir": "B8A", "nir08": "B8A", "swir16": "B11", "swir22": "B12",
}

_MODIS_BANDS = {
    "red": "sur_refl_b01", "nir": "sur_refl_b02", "blue": "sur_refl_b03",
    "green": "sur_refl_b04", "nir08": "sur_refl_b02",
    "swir16": "sur_refl_b06", "swir22": "sur_refl_b07",
}

SOURCES: dict[str, Source] = {}


def _add(source: Source) -> None:
    SOURCES[source.key] = source


_add(Source(
    key="sentinel-2-l2a",
    label="Sentinel-2 · surface reflectance",
    provider="earth-search",
    collection="sentinel-2-l2a",
    platform="Sentinel-2 A/B/C",
    kind="optical",
    resolution=10,
    bands=_S2_BANDS,
    scale="sentinel2",
    cloud_mask="scl",
    since="2015-06-23",
    revisit="≈5 days",
    swath_hint="10 m visible and near-infrared, 20 m red-edge and short-wave infrared",
    attribution="Contains modified Copernicus Sentinel data",
    notes="The best free optical imagery for most purposes.",
))

_add(Source(
    key="sentinel-2-l1c",
    label="Sentinel-2 · top of atmosphere",
    provider="earth-search",
    collection="sentinel-2-l1c",
    platform="Sentinel-2 A/B/C",
    kind="optical",
    resolution=10,
    bands=dict(_S2_BANDS, cirrus="cirrus"),
    scale="sentinel2",
    cloud_mask=None,
    since="2015-06-23",
    revisit="≈5 days",
    swath_hint="Uncorrected radiance — useful when the L2A correction misbehaves",
    attribution="Contains modified Copernicus Sentinel data",
))

_add(Source(
    key="landsat-c2-l2",
    label="Landsat 8/9 · surface reflectance",
    provider="planetary-computer",
    collection="landsat-c2-l2",
    platform="Landsat 4–9",
    kind="optical",
    resolution=30,
    bands=_LANDSAT_BANDS,
    scale="landsat_c2_sr",
    cloud_mask="qa_pixel",
    pan="pan",
    pan_resolution=15,
    since="1982-08-22",
    revisit="≈8 days with two satellites",
    swath_hint="30 m multispectral, 15 m panchromatic, plus thermal",
    attribution="Landsat imagery courtesy of the U.S. Geological Survey",
    notes="Four decades of history. Pan-sharpening doubles the detail.",
))

_add(Source(
    key="landsat-c2-l2-aws",
    label="Landsat 8/9 · surface reflectance (AWS)",
    provider="earth-search",
    collection="landsat-c2-l2",
    platform="Landsat 4–9",
    kind="optical",
    resolution=30,
    bands=_LANDSAT_BANDS,
    scale="landsat_c2_sr",
    cloud_mask="qa_pixel",
    pan="pan",
    pan_resolution=15,
    since="1982-08-22",
    revisit="≈8 days with two satellites",
    attribution="Landsat imagery courtesy of the U.S. Geological Survey",
    notes="Same imagery via AWS; the bucket is requester-pays, so this one may "
          "need AWS credentials. Prefer the Planetary Computer entry.",
))

_add(Source(
    key="landsat-thermal",
    label="Landsat 8/9 · surface temperature",
    provider="planetary-computer",
    collection="landsat-c2-l2",
    platform="Landsat 8–9",
    kind="thermal",
    resolution=30,
    bands={"lwir": "lwir11", "red": "red", "green": "green", "blue": "blue",
           "nir": "nir08", "swir16": "swir16"},
    scale="landsat_c2_sr",
    cloud_mask="qa_pixel",
    since="2013-03-18",
    revisit="≈8 days",
    swath_hint="Ground temperature — urban heat, fires, geothermal",
    attribution="Landsat imagery courtesy of the U.S. Geological Survey",
    default_composite="thermal",
))

_add(Source(
    key="hls-sentinel",
    label="Harmonised Landsat–Sentinel · S30",
    provider="planetary-computer",
    collection="hls2-s30",
    platform="Sentinel-2 (harmonised)",
    kind="optical",
    resolution=30,
    bands=_HLS_BANDS,
    scale="hls",
    cloud_mask="fmask",
    since="2015-11-28",
    revisit="2–3 days combined with L30",
    swath_hint="Landsat and Sentinel-2 on one consistent 30 m grid",
    attribution="NASA HLS, USGS and Copernicus",
))

_add(Source(
    key="hls-landsat",
    label="Harmonised Landsat–Sentinel · L30",
    provider="planetary-computer",
    collection="hls2-l30",
    platform="Landsat 8/9 (harmonised)",
    kind="optical",
    resolution=30,
    bands=dict(_HLS_BANDS, coastal="B01", lwir="B10"),
    scale="hls",
    cloud_mask="fmask",
    since="2013-04-11",
    revisit="2–3 days combined with S30",
    attribution="NASA HLS, USGS and Copernicus",
))

_add(Source(
    key="naip",
    label="NAIP · aerial (United States)",
    provider="planetary-computer",
    collection="naip",
    platform="US aerial survey",
    kind="optical",
    resolution=0.6,
    # One four-band file, so the bands are indices into a single asset.
    bands={"red": "image:1", "green": "image:2", "blue": "image:3", "nir": "image:4"},
    scale="byte",
    cloud_mask=None,
    cloud_property=None,
    since="2010-01-01",
    revisit="Every 2–3 years",
    swath_hint="Sub-metre detail — individual trees and vehicles",
    attribution="USDA National Agriculture Imagery Program",
    default_size_hint=2048,
    notes="United States only, flown in summer.",
))

_add(Source(
    key="sentinel-1-rtc",
    label="Sentinel-1 · radar (terrain corrected)",
    provider="planetary-computer",
    collection="sentinel-1-rtc",
    platform="Sentinel-1 A/B/C",
    kind="sar",
    resolution=10,
    bands={"vv": "vv", "vh": "vh"},
    scale="power_db",
    cloud_mask=None,
    cloud_property=None,
    since="2014-10-10",
    revisit="≈6–12 days",
    swath_hint="Radar: sees through cloud and works at night",
    attribution="Contains modified Copernicus Sentinel data",
    default_composite="radar",
    notes="Ideal for floods and persistent cloud.",
))

_add(Source(
    key="sentinel-1-grd",
    label="Sentinel-1 · radar (AWS)",
    provider="earth-search",
    collection="sentinel-1-grd",
    platform="Sentinel-1 A/B/C",
    kind="sar",
    resolution=10,
    bands={"vv": "vv", "vh": "vh", "hh": "hh", "hv": "hv"},
    scale="amplitude_db",
    cloud_mask=None,
    cloud_property=None,
    since="2014-10-10",
    revisit="≈6–12 days",
    attribution="Contains modified Copernicus Sentinel data",
    default_composite="radar",
))

_add(Source(
    key="modis-surface-reflectance",
    label="MODIS · daily surface reflectance",
    provider="planetary-computer",
    collection="modis-09A1-061",
    platform="Terra / Aqua",
    kind="optical",
    resolution=500,
    bands=_MODIS_BANDS,
    scale="modis",
    cloud_mask=None,
    cloud_property=None,
    since="2000-02-24",
    revisit="Daily, composited over 8 days",
    swath_hint="Coarse but near-daily, and back to 2000",
    attribution="NASA MODIS, LP DAAC",
    default_size_hint=512,
    notes="Best for continent-scale change.",
))

_add(Source(
    key="aster",
    label="ASTER · multispectral",
    provider="planetary-computer",
    collection="aster-l1t",
    platform="Terra",
    kind="optical",
    resolution=15,
    bands={"green": "VNIR_Band1", "red": "VNIR_Band2", "nir": "VNIR_Band3N",
           "swir16": "SWIR_Band4", "swir22": "SWIR_Band8", "lwir": "TIR_Band13"},
    scale="raw",
    cloud_mask=None,
    since="2000-03-04",
    revisit="On request",
    swath_hint="15 m visible/near-infrared with thermal, from 2000",
    attribution="NASA/METI ASTER, LP DAAC",
))

_add(Source(
    key="cop-dem-30",
    label="Copernicus DEM · elevation",
    provider="earth-search",
    collection="cop-dem-glo-30",
    platform="TanDEM-X derived",
    kind="dem",
    resolution=30,
    bands={"elevation": "data"},
    scale="metres",
    cloud_mask=None,
    cloud_property=None,
    since="2010-01-01",
    revisit="Static",
    swath_hint="Global 30 m terrain — hillshade and elevation tints",
    attribution="Copernicus DEM © ESA",
    default_composite="elevation",
))

_add(Source(
    key="esa-worldcover",
    label="ESA WorldCover · land cover",
    provider="planetary-computer",
    collection="esa-worldcover",
    platform="Sentinel-1 + Sentinel-2 derived",
    kind="landcover",
    resolution=10,
    bands={"classification": "map"},
    scale="raw",
    cloud_mask=None,
    cloud_property=None,
    since="2020-01-01",
    revisit="Yearly",
    swath_hint="Eleven land-cover classes at 10 m",
    attribution="ESA WorldCover project",
    default_composite="landcover",
))


# ── Lookups ────────────────────────────────────────────────────

DEFAULT_SOURCE = "sentinel-2-l2a"


def get(key: str | None) -> Source:
    return SOURCES.get(key or DEFAULT_SOURCE, SOURCES[DEFAULT_SOURCE])


def resolve_band(source: Source, alias: str) -> tuple[str, int] | None:
    """Alias -> (asset name, band index within that asset)."""
    spec = source.bands.get(alias)
    if not spec:
        return None
    if ":" in spec:
        asset, index = spec.rsplit(":", 1)
        return asset, int(index)
    return spec, 1


def supports(source: Source, aliases) -> bool:
    return all(alias in source.bands for alias in aliases)


# ESA WorldCover class table, for the legend.
WORLDCOVER_CLASSES = {
    10: ("Tree cover", "#006400"),
    20: ("Shrubland", "#ffbb22"),
    30: ("Grassland", "#ffff4c"),
    40: ("Cropland", "#f096ff"),
    50: ("Built-up", "#fa0000"),
    60: ("Bare / sparse", "#b4b4b4"),
    70: ("Snow and ice", "#f0f0f0"),
    80: ("Permanent water", "#0064c8"),
    90: ("Herbaceous wetland", "#0096a0"),
    95: ("Mangroves", "#00cf75"),
    100: ("Moss and lichen", "#fae6a0"),
}
