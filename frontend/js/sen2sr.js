// Whether a picture can be redrawn by SEN2SR. Kept apart from imagery.js,
// which needs a page and a map to load, so the rule can be tested on its own.

// The bands the SEN2SR light model enhances. A picture needing any other band
// cannot be made from its output.
export const SEN2SR_BANDS = ['red', 'green', 'blue', 'nir'];

/**
 * Why the picture on screen cannot be redrawn with SEN2SR, or '' if it can.
 *
 * Asked of the request that made the picture, not of the controls: the
 * button enhances what you are looking at.
 */
export function sen2srRefusal(request, config) {
  if (!request) return 'Show a Sentinel-2 date first.';
  if (request.satellite !== 'sentinel-2') return 'SEN2SR is trained on Sentinel-2 only.';
  if (request.scenes?.length > 1) return 'SEN2SR enhances one date — show a single date.';
  if (request.mode === 'change') return 'SEN2SR draws one date, not a difference.';
  const spec = request.mode === 'index'
    ? config?.indices?.[request.index] : config?.composites?.[request.preset];
  const outside = (spec?.bands ?? []).filter((b) => !SEN2SR_BANDS.includes(b));
  if (outside.length) {
    return 'SEN2SR enhances red, green, blue and near-infrared only — '
      + 'try True colour, the NIR false colour or NDVI.';
  }
  return '';
}
