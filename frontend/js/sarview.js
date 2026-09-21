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
