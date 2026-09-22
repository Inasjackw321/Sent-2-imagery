// Where the sun is, and what that means for a place on the ground.
//
// This is the arithmetic behind the day/night layer and the sun times in the
// right-click panel. It is here on its own, with nothing about maps or the
// interface in it, because it is the part that can be wrong in ways nobody
// would notice by looking -- a sign error puts the terminator in the right
// shape on the wrong side of the world -- and because that makes it testable.
// tests/sun.test.mjs imports this file directly.
//
// The method is the standard low-precision solar position: mean longitude and
// anomaly from the day count, a two-term correction for the earth's elliptical
// orbit, then the usual rotation from ecliptic to equatorial coordinates. Good
// to about a hundredth of a degree for a century either side of 2000, which is
// far better than a map layer or a sunrise time to the minute needs.
//
// Everything here is UTC. Local time is the caller's problem, and the browser
// does that conversion better than this could.

const RAD = Math.PI / 180;
const DEG = 180 / Math.PI;

// The sun's apparent diameter puts its centre slightly below the horizon at
// the moment the disc's upper edge appears, and refraction lowers it further.
// This is the standard combined allowance, and it is why sunrise is a few
// minutes earlier than a flat geometric calculation says.
const HORIZON = -0.833;

// How far Web Mercator can actually go. The projection stretches latitude
// without limit towards the poles, so every map that uses it stops short of
// them; this is the usual cut, and anything drawn on such a map has to respect
// it whether or not the arithmetic behind it does.
export const MERCATOR_LIMIT = 85.0511;

// Twilight, by the usual definitions: how far below the horizon the sun is
// while there is still usable light of each kind.
export const TWILIGHT = { civil: -6, nautical: -12, astronomical: -18 };

// How fast the subsolar point travels west, in degrees of longitude per hour.
// Not 15: that is the sidereal rate. The sun's own motion along the ecliptic
// takes a little off it, and using 15 would misplace sunrise by ~4 minutes.
const DEG_PER_HOUR = 360 / 24.0657;

/** Days since J2000.0, the epoch every term below is written against. */
function days(at) {
  return at.getTime() / 86400000 - 10957.5;
}

const wrap180 = (deg) => ((((deg + 180) % 360) + 360) % 360) - 180;

/** How far a latitude falls outside the globe, zero if it is on it. */
const outside = (deg) => Math.max(0, Math.abs(deg) - 90);

/**
 * Where the sun is directly overhead, and how far it is tilted from the
 * equator, at one instant.
 *
 * The subsolar point is the whole of what the day/night layer needs: the lit
 * half of the earth is the hemisphere centred on it.
 */
export function subsolar(at = new Date()) {
  const d = days(at);
  const meanLon = 280.460 + 0.9856474 * d;
  const anomaly = (357.528 + 0.9856003 * d) * RAD;
  // The correction for the earth's orbit not being circular: the sun runs
  // ahead of its mean position in January and behind it in July.
  const ecliptic = (meanLon + 1.915 * Math.sin(anomaly)
    + 0.020 * Math.sin(2 * anomaly)) * RAD;
  const tilt = (23.439 - 0.0000004 * d) * RAD;

  const declination = Math.asin(Math.sin(tilt) * Math.sin(ecliptic)) * DEG;
  const rightAscension = Math.atan2(
    Math.cos(tilt) * Math.sin(ecliptic), Math.cos(ecliptic)) * DEG;

  // Sidereal time says which way the earth is facing; the difference between
  // that and the sun's right ascension is the longitude under the sun.
  const sidereal = (18.697374558 + 24.06570982441908 * d) % 24;
  return {
    lat: declination,
    lon: wrap180(rightAscension - sidereal * 15),
    declination,
  };
}

/** How high the sun is above the horizon at a place, in degrees. */
export function elevation(lat, lon, at = new Date()) {
  const sun = subsolar(at);
  const hourAngle = (lon - sun.lon) * RAD;
  const a = lat * RAD;
  const d = sun.declination * RAD;
  return Math.asin(
    Math.sin(a) * Math.sin(d) + Math.cos(a) * Math.cos(d) * Math.cos(hourAngle)) * DEG;
}

/** True when the sun is above the horizon at a place. */
export const isDaylight = (lat, lon, at = new Date()) => elevation(lat, lon, at) > 0;

/**
 * The instant the sun crosses the meridian of a longitude.
 *
 * Found by walking rather than by formula: the subsolar longitude moves at a
 * known rate, so stepping the time by the distance still to go converges in
 * two or three passes, and it stays correct through the parts of the year
 * when the equation of time makes noon wander by a quarter of an hour.
 */
function solarNoon(lon, on) {
  let at = new Date(Date.UTC(
    on.getUTCFullYear(), on.getUTCMonth(), on.getUTCDate(), 12, 0, 0));
  for (let pass = 0; pass < 4; pass += 1) {
    const gap = wrap180(subsolar(at).lon - lon);
    at = new Date(at.getTime() + (gap / DEG_PER_HOUR) * 3600000);
  }
  return at;
}

/**
 * Sunrise, sunset and how long the sun is up, for one place on one UTC day.
 *
 * `polar` says which of the two ways there can be no sunrise it is: the sun
 * never rising, or never setting. Reporting "no sunrise" for both would be
 * true and useless -- in June above the arctic circle the answer people want
 * is that it is light the whole time.
 */
export function sunTimes(lat, lon, on = new Date(), horizon = HORIZON) {
  const noon = solarNoon(lon, on);
  const declination = subsolar(noon).declination * RAD;
  const a = lat * RAD;

  // The hour angle at which the sun sits at the given altitude. Outside ±1
  // there is no such moment: the sun's daily circle never crosses it.
  const cosHour = (Math.sin(horizon * RAD) - Math.sin(a) * Math.sin(declination))
    / (Math.cos(a) * Math.cos(declination));
  if (cosHour > 1) {
    return { polar: 'night', up: false, noon, sunrise: null, sunset: null, hours: 0 };
  }
  if (cosHour < -1) {
    return { polar: 'day', up: true, noon, sunrise: null, sunset: null, hours: 24 };
  }

  const half = (Math.acos(cosHour) * DEG) / DEG_PER_HOUR;
  const sunrise = settle(lat, lon, new Date(noon.getTime() - half * 3600000), horizon);
  const sunset = settle(lat, lon, new Date(noon.getTime() + half * 3600000), horizon);
  return {
    polar: null,
    noon,
    sunrise,
    sunset,
    hours: (sunset - sunrise) / 3600000,
  };
}

/**
 * Refine a rise or set time until the sun is genuinely at that altitude.
 *
 * The closed-form answer above uses one declination for the whole day, but the
 * sun moves nearly half a degree along the ecliptic in that time, which at
 * British latitudes is worth most of a minute of clock time -- more towards
 * the poles, where the sun meets the horizon at a shallow angle and a small
 * error in height is a large one in time.
 *
 * A couple of secant steps against the elevation calculation fixes it, and
 * costs nothing anybody will notice.
 */
function settle(lat, lon, guess, horizon) {
  let at = guess;
  for (let pass = 0; pass < 3; pass += 1) {
    const off = elevation(lat, lon, at) - horizon;
    // How fast the sun is climbing here, measured rather than assumed: near
    // the poles it can be a tenth of the rate it has at the equator.
    const minute = new Date(at.getTime() + 60000);
    const rate = elevation(lat, lon, minute) - elevation(lat, lon, at);
    if (Math.abs(rate) < 1e-9) break;
    at = new Date(at.getTime() - (off / rate) * 60000);
  }
  return at;
}

/**
 * Where the sun is, seen from the far side of the earth.
 *
 * The point with the sun straight down, which is the middle of the night and
 * the centre of every curve below.
 */
export function antisolar(at = new Date()) {
  const sun = subsolar(at);
  return { lat: -sun.lat, lon: wrap180(sun.lon + 180) };
}

/**
 * How far the dark region reaches from the antisolar point, in degrees.
 *
 * The sun's altitude at a place is ninety degrees minus that place's distance
 * from the subsolar point. So "darker than `altitude`" is "further than
 * 90 - altitude from the sun", which is the same as "within 90 + altitude of
 * the antisolar point". Night is a circle, and this is its radius: ninety
 * degrees for the terminator itself, seventy-two for astronomical twilight.
 */
export const darkRadius = (altitude = 0) => 90 + altitude;

/**
 * The line around the earth where the sun is at a given altitude.
 *
 * At altitude 0 this is the terminator itself -- the edge of the lit half. The
 * twilight bands are the same curve computed a few degrees lower.
 *
 * Walked around the antisolar point rather than solved meridian by meridian,
 * and that is the whole of it. The old version asked, for each longitude in
 * turn, "at what latitude is the sun this low here?" -- which has no answer on
 * some meridians and TWO on others, and neither case is rare:
 *
 *   Near an equinox the sun tracks the equator, so along the meridians near
 *   the terminator it skims the horizon from pole to pole and never reaches
 *   eighteen degrees down at all. There is no astronomical-twilight point on
 *   that meridian, and the old code answered with the pole -- a vertex
 *   thousands of miles from the curve it claimed to be on.
 *
 *   On the meridians where the band does exist it is a band: it has a northern
 *   edge and a southern one, and picking whichever the arcsine handed back
 *   first made the curve jump between them from one sample to the next.
 *
 * Measured on the equinox before this was rewritten: of 181 points on the
 * civil-twilight curve, 96 were not on it, the worst by 5.7 degrees; on the
 * astronomical curve, 109 of 181, the worst by 17.7. Drawn, those wrong
 * vertices are spikes from the curve to the pole and back, and a dozen bands
 * of them stack into the vertical stripes ruled down the night side of the
 * map.
 *
 * A circle has none of those cases. Every bearing from the centre gives
 * exactly one point, the curve closes on itself, and the arithmetic is the
 * same at an equinox as at a solstice.
 *
 * Returned as [lat, lon] pairs going round once, with longitudes running
 * continuously rather than wrapped at the date line -- so a curve that passes
 * behind the map's edge carries on past 180 instead of jumping back to -180
 * and drawing a line across the world.
 */
export function terminator(at = new Date(), altitude = 0, step = 1) {
  const centre = antisolar(at);
  const radius = darkRadius(altitude);
  return capRing(centre.lat, centre.lon, radius, step);
}

/**
 * A circle on the globe: every point a fixed angle from one centre.
 *
 * Longitudes come back unwrapped -- continuous, possibly outside -180..180 --
 * because that is what a polygon needs. Wrapping each point into range is what
 * puts a horizontal line across a map whenever a shape crosses the date line.
 */
export function capRing(centreLat, centreLon, radius, step = 1) {
  const lat0 = centreLat * RAD;
  const rho = Math.max(0, Math.min(180, radius)) * RAD;
  const turn = Math.max(0.1, Math.min(45, step));
  const out = [];
  let last = null;
  for (let bearing = 0; bearing <= 360 + 1e-9; bearing += turn) {
    const theta = bearing * RAD;
    const lat = Math.asin(
      Math.sin(lat0) * Math.cos(rho)
      + Math.cos(lat0) * Math.sin(rho) * Math.cos(theta));
    let lon = centreLon + Math.atan2(
      Math.sin(theta) * Math.sin(rho) * Math.cos(lat0),
      Math.cos(rho) - Math.sin(lat0) * Math.sin(lat)) * DEG;
    // Unwrapped: each point is put on the same turn as the one before it, so
    // the longitudes run on past 180 instead of jumping the width of the map.
    // A wrapped longitude here is a vertex on the far side of the world from
    // its neighbour, which draws as an edge straight across the map and a
    // fill to match.
    //
    // Both directions, though walking the bearings upwards only ever needs
    // the second: the arc tangent's jump comes at the far side of the circle
    // and always goes the same way. The first is there for a caller that
    // walks the other way round, and a mutation test will not kill it -- said
    // here so that nobody has to work that out twice.
    if (last !== null) {
      while (lon - last > 180) lon -= 360;
      while (last - lon > 180) lon += 360;
    }
    last = lon;
    out.push([lat * DEG, lon]);
  }
  return out;
}

/**
 * Whether the dark region reaches over a pole, and which one.
 *
 * 1 for the north, -1 for the south, 0 for neither. The dark pole is the one
 * away from the sun, so this is also the answer to which way up the night is.
 */
export function poleInside(centreLat, radius) {
  if (90 - centreLat < radius) return 1;      // north
  if (90 + centreLat < radius) return -1;     // south
  return 0;
}

/**
 * How far round the world a curve travels, in whole turns.
 *
 * This, and not which pole is inside it, is what decides how the shape has to
 * be closed. The difference is not a nicety; both cases are ordinary:
 *
 *   AT A SOLSTICE the terminator is a circle tilted from the equator that
 *   goes right round the earth, through every longitude once. It winds. Drawn
 *   on a flat map it is not a loop at all, it is a line from one side to the
 *   other, and the night is everything between it and the pole -- so it has
 *   to be closed along that pole's edge.
 *
 *   AT AN EQUINOX the same curve runs up over one pole and back down the far
 *   side. It covers half the longitudes, twice each, and returns to where it
 *   started. It does not wind. Drawn as it is it closes itself and encloses
 *   exactly the night; closing it along a pole as well lays a hairline across
 *   the daylight and hangs a band of shading under it.
 *
 * Deciding by which pole is inside gets the second case wrong, because the
 * pole IS inside the dark region and the curve still does not wind around it.
 */
export function turns(ring) {
  if (ring.length < 2) return 0;
  // The `|| 0` is not decoration: rounding a small negative gives -0, which
  // is falsy like 0 but not equal to it, so a caller comparing turns to 0
  // gets different answers for the same shape depending on which way round
  // it was walked.
  return Math.round((ring[ring.length - 1][1] - ring[0][1]) / 360) || 0;
}

/**
 * The dark part of the earth as polygons, ready to be drawn.
 *
 * Returns a list of rings. Several, because at low zoom the map repeats
 * sideways and a shape that stops at the date line stops on every copy,
 * leaving hard vertical edges wherever one copy ends and the next begins.
 *
 * Every ring is held inside the latitudes Web Mercator can place. The
 * projection sends ninety degrees to infinity, so a polygon with a vertex
 * there is handed to the clipper as a coordinate it cannot hold; what comes
 * back is folded, and draws as seams across the shading.
 */
export function nightRings(at = new Date(), altitude = 0, step = 1,
                           span = { west: -180, east: 180 }) {
  const centre = antisolar(at);
  const radius = darkRadius(altitude);
  if (radius <= 0) return [];
  const ring = capRing(centre.lat, centre.lon, radius, step);
  const { west, east } = asSpan(span);
  const edgeLat = MERCATOR_LIMIT - 0.6;

  if (!turns(ring)) {
    // A curve that comes back to where it started encloses the night on its
    // own. Clamping is what turns the part that runs over a pole into a flat
    // top, which is right: everything between the curve and the pole is dark
    // as well, and none of it is drawable anyway.
    return tile(ring, west, east).map(clampRing);
  }

  // A curve that goes right round the world is a line across the map rather
  // than a loop, and the night is everything between it and the dark pole:
  // laid out in longitude order, tiled sideways, closed along that edge.
  const pole = poleInside(centre.lat, radius) || (centre.lat > 0 ? 1 : -1);
  const edge = pole > 0 ? edgeLat : -edgeLat;
  const walk = ring[ring.length - 1][1] > ring[0][1] ? ring : [...ring].reverse();
  const long = tile(walk, west, east).flat();

  // Split where the curve leaves the band the projection can draw, and keep
  // the stretches that enclose something.
  //
  // Near an equinox the terminator is very nearly the two meridians through
  // the poles: it crosses the drawable latitudes almost vertically and spends
  // the rest of its length beyond 85 degrees, off the map. Over those
  // longitudes there is no night to draw between the curve and the edge --
  // all of it is past the edge already. Clamping those vertices instead of
  // dropping them leaves a polygon of no height lying along the bottom of the
  // map, and a fill of no height still paints: it came out as a hairline
  // ruled across the daylight at eighty-four degrees south.
  const runs = [];
  let run = [];
  for (const [lat, lon] of long) {
    if (pole > 0 ? lat < edge : lat > edge) {
      run.push([Math.max(-edgeLat, Math.min(edgeLat, lat)), lon]);
    } else if (run.length) {
      runs.push(run);
      run = [];
    }
  }
  if (run.length) runs.push(run);

  return runs
    .filter((one) => one.length > 1)
    .map((one) => [...one, [edge, one[one.length - 1][1]], [edge, one[0][1]]]);
}

/**
 * The longitudes a drawing has to cover.
 *
 * A number is the old way of asking -- so many copies of the world either
 * side of the meridian -- and is kept because it reads well at a call site
 * that has no map to ask. A map should pass what it can actually see: it can
 * be panned to any longitude at all, and a shape tiled around zero runs out
 * partway across the screen.
 */
function asSpan(span) {
  if (typeof span === 'number') {
    const copies = Math.max(1, Math.min(5, Math.round(span)));
    return { west: -180 * copies, east: 180 * copies };
  }
  return { west: Number(span?.west ?? -180), east: Number(span?.east ?? 180) };
}

/**
 * One copy of a shape per turn of longitude, enough to cover a span.
 *
 * Tiled from the shape's own position rather than from the meridian, so a
 * curve that happens to sit at longitude 100 still covers a view at -170.
 */
export function tile(ring, west, east) {
  if (!ring.length) return [];
  const lons = ring.map(([, lon]) => lon);
  const low = Math.min(...lons);
  const high = Math.max(...lons);
  const from = Math.floor((west - high) / 360);
  const to = Math.ceil((east - low) / 360);
  const out = [];
  // A guard rather than a limit anybody should reach: a view spanning more
  // than a few dozen turns is a bug somewhere else, and this stops it
  // becoming an unbounded loop here.
  for (let n = from; n <= Math.min(to, from + 40); n += 1) {
    out.push(ring.map(([lat, lon]) => [lat, lon + n * 360]));
  }
  return out;
}

/**
 * A curve cut into the pieces of it a Mercator map can show.
 *
 * Latitudes beyond about 85 degrees are not drawable, and Leaflet pins
 * anything past them to the edge -- so a curve that spends most of its length
 * near a pole, which is what the terminator does near an equinox, comes out
 * as a line ruled along the bottom of the map instead of disappearing off it.
 * Dropping those stretches is the honest version: the curve is not there.
 */
export function withinMap(curve, limit = MERCATOR_LIMIT - 0.6) {
  const out = [];
  let run = [];
  for (const point of curve) {
    if (Math.abs(point[0]) <= limit) run.push(point);
    else if (run.length) { out.push(run); run = []; }
  }
  if (run.length) out.push(run);
  return out.filter((one) => one.length > 1);
}

/** Held inside the latitudes Web Mercator can actually place. */
function clampRing(ring) {
  const edge = MERCATOR_LIMIT - 0.6;
  return ring.map(([lat, lon]) => [Math.max(-edge, Math.min(edge, lat)), lon]);
}

/**
 * The dark half of the earth as one ring. Kept for callers that want a single
 * shape; the layer itself uses nightRings, which can say "two separate
 * pieces" when that is what the sky is doing.
 */
export function nightRing(at = new Date(), altitude = 0, step = 1, wraps = 1) {
  return nightRings(at, altitude, step, wraps)[0] ?? [];
}
