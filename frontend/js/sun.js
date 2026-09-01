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
 * The line around the earth where the sun is at a given altitude.
 *
 * At altitude 0 this is the terminator itself -- the edge of the lit half. The
 * twilight bands are the same curve computed a few degrees lower.
 *
 * Returned as [lat, lon] pairs from one edge of the map to the other, which is
 * the order a polyline wants. Near an equinox the declination approaches zero
 * and the curve straightens into the two meridians through the poles; the
 * clamp keeps that from becoming a division that runs away.
 */
export function terminator(at = new Date(), altitude = 0, step = 1) {
  const sun = subsolar(at);
  const declination = sun.declination * RAD;
  const wanted = Math.sin(altitude * RAD);

  const ring = [];
  for (let lon = -180; lon <= 180; lon += step) {
    const hourAngle = (lon - sun.lon) * RAD;
    // Solving sin(alt) = sin(lat)sin(dec) + cos(lat)cos(dec)cos(H) for lat.
    // Written as one sine of a shifted angle rather than as a tangent: the
    // tangent form divides by tan(dec), which runs away to infinity twice a
    // year at the equinoxes, and this form does not divide by it at all.
    const a = Math.sin(declination);
    const b = Math.cos(declination) * Math.cos(hourAngle);
    const size = Math.hypot(a, b);
    const phase = Math.atan2(b, a);
    // The altitude asked for is sometimes one the sun never reaches anywhere
    // along this meridian, and that is a fact about the sky rather than a
    // failure. At an equinox the meridian a quarter turn from the sun runs
    // pole to pole through sunrise: the sun sits within two degrees of the
    // horizon the whole way along it, and civil twilight never begins. The
    // band simply does not exist here, so its edge goes to the dark pole and
    // encloses nothing. Solving anyway returns the least-wrong latitude, which
    // is a line of night drawn across the middle of the daylight.
    if (size < 1e-9 || Math.abs(wanted) > size) {
      ring.push([sun.declination > 0 ? -90 : 90, lon]);
      continue;
    }
    const lift = Math.max(-1, Math.min(1, wanted / size));
    // A sine has two solutions per turn, and which one is the real latitude
    // changes with the season: taking the first every time throws the curve
    // past the pole for half the year, where it gets clamped into a straight
    // line along the top of the map that looks deliberate and is not.
    const candidates = [
      Math.asin(lift) - phase,
      Math.PI - Math.asin(lift) - phase,
    ].map((r) => wrap180(r * DEG));

    // Both roots solve the shifted sine, but only one is a latitude on this
    // meridian: past the pole you are on the far side of the world, at a
    // longitude half a turn away. So each is checked back against the altitude
    // it is supposed to have, and the one that is actually right wins.
    //
    // Sometimes neither lands on the globe, and that is an answer rather than
    // a failure: on the meridian under the sun in September the sun never gets
    // eighteen degrees down however far towards the pole you walk, because the
    // pole is only eight degrees short of it. The curve has run off the end of
    // the world, so it belongs against the pole it ran past -- the one it
    // misses by least. Anywhere else, the equator especially, draws a band of
    // night across the daylight.
    const lat = candidates
      .map((deg) => {
        const clamped = Math.max(-90, Math.min(90, deg));
        const a = clamped * RAD;
        const has = Math.sin(a) * Math.sin(declination)
          + Math.cos(a) * Math.cos(declination) * Math.cos(hourAngle);
        return { deg: clamped, off: outside(deg), miss: Math.abs(has - wanted) };
      })
      .sort((x, y) => (x.off - y.off) || (x.miss - y.miss))[0].deg;
    ring.push([lat, lon]);
  }
  return ring;
}

/**
 * The dark half of the earth as a closed ring, ready to be drawn.
 *
 * The terminator alone is an open curve; to shade the night it has to be
 * closed along whichever pole is in darkness, which is the one on the opposite
 * side of the equator from the sun. Getting that backwards shades the daylight
 * instead, and looks entirely plausible until you check it against a clock.
 */
export function nightRing(at = new Date(), altitude = 0, step = 1) {
  const sun = subsolar(at);
  const ring = terminator(at, altitude, step);
  const darkPole = sun.declination > 0 ? -90 : 90;
  return [
    ...ring,
    [darkPole, 180],
    [darkPole, -180],
  ];
}
