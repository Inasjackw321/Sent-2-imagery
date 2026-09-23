// What a Sentinel-1 pass is, in the words a reader needs.
//
// An optical scene is nearly self-describing: a date, a cloud figure, and the
// picture looks like the ground. A radar scene is not. Two passes over the
// same field a day apart can be unrecognisable as the same place, and nothing
// in the picture says why -- so the acquisition parameters are not trivia
// here, they are most of what makes the image readable.
//
// Its own file rather than part of the imagery panel, because the imagery
// panel reaches for Leaflet the moment it is loaded and none of this needs a
// map. A rule about which picture a pass can draw is worth being able to test
// without a browser.

/** The line under a radar pass in the date list.
 *
 * "Descending · track 36 · IW · VV+VH". Four facts, in the order somebody
 * choosing between passes wants them: which way it looked -- which decides
 * whether it is comparable with the one above -- which repeat track, which
 * mode, and which pictures it can be asked for.
 *
 * An optical scene gets nothing here and keeps its tile.
 */
export function sarLabel(scene) {
  if (!scene || scene.satellite !== 'sentinel-1') return '';
  const way = { ascending: 'Ascending', descending: 'Descending' }[
    String(scene.orbit_state ?? '').toLowerCase()] ?? '';
  const pol = sarPolarisations(scene);
  return [
    way,
    scene.orbit != null ? `track ${scene.orbit}` : '',
    scene.mode ?? '',
    pol.join('+'),
  ].filter(Boolean).join(' · ');
}

/** Which polarisations the scene currently chosen carries, upper-cased. */
export function sarPolarisations(scene) {
  const said = scene?.polarisations;
  if (Array.isArray(said) && said.length) {
    return said.map((p) => String(p).toUpperCase());
  }
  return [];
}

/** Whether a composite's bands can be made from the polarisations to hand. */
export function canMake(spec, have) {
  const NEEDS = { vv: 'VV', vh: 'VH', hh: 'HH', hv: 'HV',
                  vvvh: ['VV', 'VH'], hhhv: ['HH', 'HV'] };
  for (const band of spec.bands ?? []) {
    const need = NEEDS[band];
    if (!need) continue;
    for (const one of [need].flat()) {
      if (!have.has(one)) return false;
    }
  }
  return true;
}

// ── Comparing two passes ───────────────────────────────────────
//
// A change render subtracts one pass from the other, and the two sentences
// about it -- before, on the button, and after, over the picture -- are the
// only things on screen that say it is not an average. Both live here, as
// functions of their arguments, so they can be read back in a test rather
// than matched as text in the panel: a branch turned off still contains the
// words, and a test that only greps for them passes over a dead one.

/** Whole days between two scenes, however their dates were written. */
function daysApart(a, b) {
  const one = Date.parse(`${String(a ?? '').slice(0, 10)}T00:00:00Z`);
  const two = Date.parse(`${String(b ?? '').slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(one) || Number.isNaN(two)) return null;
  return Math.abs(Math.round((one - two) / 86400000));
}

/** The line above the button: what pressing it would compare. */
export function changePlan(dates, px) {
  if (dates.length !== 2) {
    return `Tick exactly two passes — <b>${dates.length} ticked</b>.`;
  }
  const apart = daysApart(dates[0].date, dates[1].date);
  return [`<b>2 passes</b>`, px, apart != null ? `${apart} days apart` : '',
          'what changed between them, in decibels'].filter(Boolean).join(' · ');
}

/**
 * The line over the picture: what actually came back.
 *
 * How much of the frame moved is the headline. A change picture that is a
 * pale wash could be a quiet fortnight or a render that failed, and they look
 * the same -- the percentage is what tells them apart. It is said with the
 * threshold it was measured against, because a bare percentage of "moved" is
 * not a measurement of anything.
 */
export function changeSaid(meta, showDate = (d) => d) {
  const ch = meta?.change;
  if (!ch) return '';
  const when = ch.days != null ? `over ${ch.days} days ` : '';
  const which = ch.older && ch.newer
    ? `(${showDate(ch.older)} → ${showDate(ch.newer)}) ` : '';
  const moved = ch.moved_pct != null
    ? `— ${ch.moved_pct}% of it moved by over ${ch.moved_above_db} dB `
    : '';
  return `${String(ch.band ?? '').toUpperCase()} change ${when}${which}${moved}`
    .trim();
}

/** What the pass was, for the panel under the picture. */
export function sarSaid(meta) {
  const said = meta?.sar;
  if (!said?.pair) return '';
  return [
    said.pass_name,
    said.track != null ? `track ${said.track}` : '',
    said.mode_name ? `${said.mode} — ${said.mode_name}` : said.mode,
    said.pair,
    said.product,
  ].filter(Boolean).join(' · ');
}
