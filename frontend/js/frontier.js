// Which stretch of a NATO or Moldovan border has something near it.
//
// Ukraine's western neighbours are drawn on this map as one unbroken red line
// each, which says where the border is and nothing about tonight. On a night
// when a Shahed is tracking up the Polesian corridor, the question anybody
// looking at that line is actually asking is "how close, and where" -- and a
// line that looks identical whether the nearest drone is four kilometres away
// or four hundred cannot answer it.
//
// So the stretch with something near it is drawn hot: the same border, lit
// where it matters. Everything else about the line is unchanged, which is the
// point -- it is a warning ON the border rather than a new thing on the map
// competing with the marks.
//
// Three deliberate limits.
//
//   Only Poland, Romania and Moldova. Belarus and Russia are the other side
//   of this war; a drone near the Belarusian border is not the same fact as a
//   drone near Poland's and must not be drawn as though it were.
//
//   Only things that fly and are somewhere. A warning covers a province and
//   has no position finer than that, so measuring it against a border would
//   be measuring the middle of an oblast -- see the region checks below.
//
//   Distance to the border, not to the nearest drawn vertex. These rings are
//   thinned before they are sent, so consecutive points can be kilometres
//   apart, and a drone sitting between two of them would read as far from
//   both while being right on the line.

// Whose borders are watched, by the country code the backend files them under.
export const WATCHED = ['pl', 'ro', 'md'];

// How close is close.
export const NEAR_KM = 25;

// What counts as something in the air. A guided bomb and an aircraft are not
// what was asked about, and an air alert has no position to measure.
export const FLYING = ['drone', 'jet_drone', 'fpv', 'missile'];

const EARTH_KM = 6371;
const RAD = Math.PI / 180;

/** Kilometres between two points, near enough at this scale.
 *
 * Equirectangular rather than haversine: over the tens of kilometres this is
 * asked about it differs from the great circle by centimetres, and it is the
 * projection the segment test below needs anyway.
 */
export function apart(aLat, aLon, bLat, bLon) {
  const mid = ((aLat + bLat) / 2) * RAD;
  const x = (bLon - aLon) * RAD * Math.cos(mid);
  const y = (bLat - aLat) * RAD;
  return Math.hypot(x, y) * EARTH_KM;
}

/** Kilometres from a point to the SEGMENT between two others.
 *
 * Not to its ends. A border thinned to a thousand points has segments
 * kilometres long, and a drone halfway along one is as close to the border as
 * it is possible to be while being far from either drawn point.
 */
export function toSegment(lat, lon, aLat, aLon, bLat, bLon) {
  const mid = ((aLat + bLat) / 2) * RAD;
  const px = (lon - aLon) * RAD * Math.cos(mid);
  const py = (lat - aLat) * RAD;
  const bx = (bLon - aLon) * RAD * Math.cos(mid);
  const by = (bLat - aLat) * RAD;
  const len = bx * bx + by * by;
  // A zero-length segment is a point, and a ring can carry repeated points.
  const along = len > 0 ? Math.max(0, Math.min(1, (px * bx + py * by) / len)) : 0;
  return Math.hypot(px - bx * along, py - by * along) * EARTH_KM;
}

/** Whether a mark is a flying thing with a position worth measuring.
 *
 * A warning, a region-only track and a drone raised from a region warning all
 * sit on the arithmetic centre of a province. Measuring those against a
 * border would light up a stretch of Poland because a warning was declared
 * two hundred kilometres away, which is worse than saying nothing.
 */
export function flying(event) {
  return Boolean(event)
    && FLYING.includes(event.kind)
    && event.placed !== false
    && !event.area_only
    && !event.from_warning
    && event.region_scope !== 'located'
    && Number.isFinite(event.lat) && Number.isFinite(event.lon);
}

/** Whether an outline is one of the borders being watched. */
export function watched(outline) {
  return outline?.level === 'country' && WATCHED.includes(outline.in);
}

function rings(shape) {
  const kind = shape?.type;
  const coords = shape?.coordinates;
  if (kind === 'Polygon' && Array.isArray(coords)) {
    return coords.filter(Array.isArray);
  }
  if (kind === 'MultiPolygon' && Array.isArray(coords)) {
    return coords.filter(Array.isArray).flat().filter(Array.isArray);
  }
  return [];
}

/**
 * The stretches of watched border with something flying near them.
 *
 * Returns one entry per stretch: the country it belongs to, the points to
 * draw, and how far away the nearest thing actually was -- because "a drone
 * is near the Polish border" and "a drone is four kilometres from the Polish
 * border" are different statements and the second one is the one worth
 * putting on a picture.
 *
 * A stretch is a RUN of consecutive border points, so a drone flying along
 * the border lights the length it is flying along rather than one dot.
 */
export function hotSegments(outlines, events, km = NEAR_KM) {
  const near = (events ?? []).filter(flying);
  if (!near.length) return [];
  const out = [];
  for (const outline of outlines ?? []) {
    if (!watched(outline)) continue;
    for (const ring of rings(outline.shape)) {
      out.push(...hotRuns(ring, near, km, outline));
    }
  }
  return out;
}

/** The runs of one ring that are within reach of something. */
function hotRuns(ring, near, km, outline) {
  if (!Array.isArray(ring) || ring.length < 2) return [];
  // Measured per SEGMENT, then attributed to both of its ends, so a run
  // always has two points in it and is drawable as a line.
  const lit = new Array(ring.length).fill(Infinity);
  for (let i = 0; i < ring.length - 1; i += 1) {
    const a = ring[i];
    const b = ring[i + 1];
    if (!pair(a) || !pair(b)) continue;
    let closest = Infinity;
    for (const event of near) {
      const gap = toSegment(event.lat, event.lon, a[1], a[0], b[1], b[0]);
      if (gap < closest) closest = gap;
      if (closest === 0) break;
    }
    if (closest > km) continue;
    lit[i] = Math.min(lit[i], closest);
    lit[i + 1] = Math.min(lit[i + 1], closest);
  }

  const runs = [];
  let from = -1;
  for (let i = 0; i <= ring.length; i += 1) {
    const on = i < ring.length && lit[i] < Infinity;
    if (on && from < 0) from = i;
    if (!on && from >= 0) {
      // Always at least two points, and not by luck: a lit SEGMENT lights
      // both of its ends, so a lit point can never have two unlit neighbours.
      // A length check here was measured unreachable and taken out rather
      // than left as a guard nobody can trip.
      runs.push({
        in: outline.in,
        name: outline.name,
        points: ring.slice(from, i),
        km: Math.min(...lit.slice(from, i)),
      });
      from = -1;
    }
  }
  return runs;
}

function pair(point) {
  return Array.isArray(point) && Number.isFinite(point[0])
    && Number.isFinite(point[1]);
}

/** One line for a panel or a picture caption, or '' when nothing is near. */
export function hotSaid(segments, names = {}) {
  if (!segments?.length) return '';
  const closest = new Map();
  for (const run of segments) {
    const had = closest.get(run.in);
    if (had == null || run.km < had) closest.set(run.in, run.km);
  }
  const said = [...closest.entries()]
    .sort((a, b) => a[1] - b[1])
    .map(([code, km]) => `${names[code] ?? code.toUpperCase()} ${Math.round(km)} km`);
  return `Nearest border: ${said.join(', ')}`;
}
