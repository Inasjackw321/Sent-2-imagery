// Asking a tile service whether it is willing to serve us.
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

/** How to put a refusal to somebody looking at a map that just changed. */
export function saidNo(status) {
  // 403 and 429 are a service stating who it serves and how often, and are
  // worth repeating exactly: it is the difference between "the internet is
  // broken" and "this app was asking too much of a charity".
  if (status === 403) return 'access blocked (403)';
  if (status === 429) return 'rate limited (429)';
  return `refused with ${status}`;
}
