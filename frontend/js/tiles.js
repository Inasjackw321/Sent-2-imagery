// Which tile services this app uses, and whether they are serving us.
//
// ── Which ─────────────────────────────────────────────────────
//
// Everything here is keyless, and that is a hard rule rather than a
// preference, because breaking it has now gone wrong twice in a row and both
// times it went wrong quietly.
//
// CARTO started requiring an API key and stamped "API KEY REQUIRED" diagonally
// across every tile. The map still drew. Nothing errored, nothing fell back,
// no message appeared -- it simply went wrong in public, on a map with city
// names on it, looking for all the world like a finished product that had not
// paid its bill.
//
// Then the default moved to OpenStreetMap's own servers, which are volunteer-
// run and donation-funded and whose usage policy says plainly that they are
// not there to be an application's basemap. They blocked the app and served a
// warning sign as the tile.
//
// Then -- and this is the one worth writing down -- the fix for the second
// went back to the first. The comment saying CARTO needed a key was four lines
// above the list being edited and was not read. Hence KEYLESS_HOSTS below and
// a test that fails on anything else: the rule is now enforced by something
// that does not rely on anybody reading a comment.
//
// ── Whether ───────────────────────────────────────────────────
//
// This module exists because of a failure that looked nothing like a failure.
//
// When OpenStreetMap blocks a client for breaching its tile usage policy, it
// does not withhold the tile. It answers 403 with a PNG in the body: a
// yellow-and-black warning sign reading "Access blocked -- app is not
// following the tile usage policy of OpenStreetMap's volunteer-run servers".
// A browser asked to put that into an <img> puts it there. The status code is
// never consulted, the picture is a valid picture, so the image fires load
// rather than error. Every tile "succeeds". The map fills edge to edge with a
// tiled refusal, and an app watching for tile errors sees a perfectly healthy
// basemap.
//
// fetch is the only way from a page to see the status a tile came with, so
// that is what this does: one tile, one status, read honestly.
//
// It does not catch everything, and it is worth being straight about the gap.
// A watermarked tile -- CARTO's "API KEY REQUIRED" -- comes back 200 with a
// perfectly valid picture in it. No status code is wrong, nothing is missing,
// and short of reading the pixels and guessing at diagonal grey text there is
// nothing here to detect. That failure is prevented rather than detected, by
// the keyless rule above and the test that enforces it.

// The tile hosts this app is allowed to use, and why each one is here.
//
// A host earns a place by serving tiles to anonymous clients as a stated
// offer, not by happening to work today. Anything not on this list is refused
// by a test, which is the only mechanism that has actually held.
export const KEYLESS_HOSTS = {
  // Esri's public ArcGIS Online basemaps: no key, no sign-up, served to
  // anonymous clients, and already relied on by this app's imagery and ocean
  // layers for long enough to be worth trusting for the rest.
  'server.arcgisonline.com': 'Esri public ArcGIS Online basemaps',
  // Volunteer-run like OpenStreetMap's own, but with a usage policy that
  // permits modest embedded use rather than forbidding it. It is not the
  // default, and if it is ever blocked the probe below will catch it, because
  // OpenTopoMap refuses with a status rather than with a picture.
  'tile.opentopomap.org': 'OpenTopoMap, CC-BY-SA, modest use permitted',
};

/** Every host a basemap URL would actually contact, {s} expanded away. */
export function hostsUsed(specs) {
  return [...new Set(specs.map((spec) => new URL(probeUrl(spec)).host))];
}

// One tile, at a zoom every provider certainly has cached, over land in the
// northern mid-latitudes so that no service can reasonably be missing it.
// Low zoom on purpose -- this asks a question, and it should cost whoever is
// answering as close to nothing as possible.
export const PROBE = { z: 3, x: 4, y: 2 };

/**
 * The probe tile's URL for a basemap, with Leaflet's template filled in.
 *
 * Leaflet substitutes these itself when it builds a tile; here it has to be
 * done by hand, because the point is to ask before any tile is built.
 */
export function probeUrl(spec) {
  const subs = spec.options?.subdomains;
  // Leaflet accepts subdomains as either a string of letters or an array, and
  // defaults to 'abc' when a URL has {s} in it and nothing was said.
  const first = typeof subs === 'string' ? subs[0] : subs?.[0] ?? 'a';
  return spec.url
    .replaceAll('{s}', first)
    .replaceAll('{z}', String(PROBE.z))
    .replaceAll('{x}', String(PROBE.x))
    .replaceAll('{y}', String(PROBE.y))
    // The retina suffix is '@2x' or nothing, and nothing is the safe one to
    // probe with: a service may serve the plain tile and not the retina one.
    .replaceAll('{r}', '');
}

/**
 * The status a service refused with, or null if it did not refuse.
 *
 * Null covers two quite different things, deliberately. It is returned when
 * the service answered OK -- no refusal -- and also when the fetch threw,
 * which means CORS, or no network, or an extension eating the request. None of
 * those is evidence about the service, and acting on them would move a user
 * off a basemap that was working. Tile errors already catch a service that has
 * genuinely gone away; this one is only here to catch the service that answers
 * and says no, so it stays silent whenever it cannot see the answer.
 */
export async function refusal(spec, fetcher = fetch) {
  try {
    const res = await fetcher(probeUrl(spec), { mode: 'cors', cache: 'no-store' });
    return res.ok ? null : res.status;
  } catch {
    return null;
  }
}

const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';

// Where a basemap's place names come from. This is a field rather than a
// comment because getting it wrong is the one basemap fault that does not
// look like a fault.
//
// Esri's World Street Map was the default until it was found captioning the
// capital of Ukraine "Kiev" -- the Soviet-era transliteration, not the name
// the country uses or the one the rest of the map's labels are in. It draws
// beautifully. Roads, borders, rivers, shading, all correct. Only the names
// are from some years ago, and a name you cannot trust makes the whole map
// suspect: if that one is stale, which of the others are?
//
// Nothing automatic can catch that -- the tile is valid, the service is
// healthy, the label is simply out of date -- so it is recorded per basemap
// and the default is held to it by a test.
export const NAMES = {
  // Rendered from OpenStreetMap, whose names are edited by the people who live
  // there and corrected within days.
  OSM: 'openstreetmap',
  // No place names at all. Cannot be wrong, and for a backdrop under fires or
  // vessels that is often what you want.
  NONE: 'none',
  // The provider's own cartography, which may lag. Fine for a layer nobody
  // reads names off; never the default.
  VENDOR: 'vendor',
};

/**
 * The basemaps on offer, in the order they are fallen back through.
 *
 * No label overlays anywhere: a basemap's names are either drawn into the tile
 * by whoever made it or absent. Stacking a separate label layer on top is how
 * you get names that disagree with the map underneath, and Esri's reference
 * overlay is the same stale cartography as its street map.
 */
export const BASEMAPS = [
  {
    // Default, and the only one here with current place names on it.
    //
    // It is a topographic map, which is more ink than a plain street map --
    // contours, relief, paths -- but it is rendered from OpenStreetMap, so
    // Kyiv is Kyiv, and it is the one keyless raster service left that renders
    // OSM and permits this use. That is a thin field, and it is thin because
    // OpenStreetMap's own servers forbid it and every commercial renderer of
    // OSM data now wants an API key.
    key: 'topo', label: 'Topographic',
    names: NAMES.OSM,
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    options: {
      subdomains: 'abc', maxNativeZoom: 17, maxZoom: 19,
      attribution: '© OpenStreetMap contributors, SRTM · © OpenTopoMap (CC-BY-SA)',
    },
  },
  {
    key: 'satellite', label: 'Satellite',
    // Photographs. The only names on it are the ones painted on the ground.
    names: NAMES.NONE,
    url: `${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`,
    options: {
      maxNativeZoom: 19, maxZoom: 19,
      attribution: 'Esri, Maxar, Earthstar Geographics',
      // Taken down a little so the app's own overlays, markers and pins stay
      // the brightest thing on screen instead of competing with the backdrop.
      className: 'tiles-imagery',
    },
  },
  {
    // Esri's canvas "Base" layers carry no labels -- that is the whole point of
    // Esri splitting them from the matching "Reference" layers, which this app
    // deliberately never loads. So they cannot caption anything wrongly, and
    // they are the quiet grey backdrop you want under a layer that is itself
    // the subject: fires, vessels, cloud.
    key: 'plain', label: 'Plain',
    names: NAMES.NONE,
    url: `${ESRI}/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`,
    options: {
      maxNativeZoom: 16, maxZoom: 19,
      attribution: 'Esri, HERE, Garmin, © OpenStreetMap contributors',
    },
  },
  {
    key: 'dark', label: 'Dark',
    names: NAMES.NONE,
    url: `${ESRI}/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`,
    options: {
      maxNativeZoom: 16, maxZoom: 19,
      attribution: 'Esri, HERE, Garmin, © OpenStreetMap contributors',
      // Esri's dark canvas is really a mid grey. Deepened here so it reads as
      // a background rather than as the subject.
      className: 'tiles-dark',
    },
  },
  {
    // Bathymetry, which is what it is for. Its few labels are ocean features
    // and Esri's own, so it is not somewhere to read a city name off.
    key: 'ocean', label: 'Ocean',
    names: NAMES.VENDOR,
    url: `${ESRI}/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}`,
    options: {
      maxNativeZoom: 13, maxZoom: 19,
      attribution: 'Esri, GEBCO, NOAA, National Geographic',
    },
  },
];

/** Which one is on screen at the start. */
export const DEFAULT_BASEMAP = 'topo';

/** How to put a refusal to somebody looking at a map that just changed. */
export function saidNo(status) {
  // 403 and 429 are a service stating who it serves and how often, and are
  // worth repeating exactly: it is the difference between "the internet is
  // broken" and "this app was asking too much of a charity".
  if (status === 403) return 'access blocked (403)';
  if (status === 429) return 'rate limited (429)';
  return `refused with ${status}`;
}
