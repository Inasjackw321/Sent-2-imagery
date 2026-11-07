// Whether a picture can be redrawn by one of the networks -- SEN2SR or SR4RS.
// Kept apart from imagery.js, which needs a page and a map to load, so
// the rules can be tested on their own.

// The bands SEN2SR's light model and SR4RS both enhance, in their order. A
// picture needing any other band cannot be made from their output.
export const SEN2SR_BANDS = ['red', 'green', 'blue', 'nir'];

// What each button is called and does.
export const NETWORKS = {
  sen2sr: {
    name: 'SEN2SR', bands: SEN2SR_BANDS, metres: 2.5,
    does: 'Redraw this date at 2.5 m with the SEN2SR network (ESA OpenSR), on this computer',
  },
  sr4rs: {
    name: 'SR4RS', bands: SEN2SR_BANDS, metres: 2.5,
    does: 'Redraw this date at 2.5 m with the SR4RS network (R. Cresson, INRAE), on this computer',
  },
};

/**
 * Why the picture on screen cannot be redrawn by this network, or '' if it
 * can.
 *
 * Asked of the request that made the picture, not of the controls: the
 * buttons enhance what you are looking at.
 */
export function networkRefusal(key, request, config) {
  const net = NETWORKS[key];
  if (!net) return 'No such network.';
  const { name } = net;
  if (!request) return 'Show a Sentinel-2 date first.';
  if (request.satellite !== 'sentinel-2') return `${name} is trained on Sentinel-2 only.`;
  if (request.scenes?.length > 1) return `${name} enhances one date — show a single date.`;
  if (request.mode === 'change') return `${name} draws one date, not a difference.`;
  const spec = request.mode === 'index'
    ? config?.indices?.[request.index] : config?.composites?.[request.preset];
  const outside = (spec?.bands ?? []).filter((b) => !net.bands.includes(b));
  if (outside.length) {
    return `${name} enhances red, green, blue and near-infrared only — `
      + 'try True colour, the NIR false colour or NDVI.';
  }
  return '';
}

export function sen2srRefusal(request, config) {
  return networkRefusal('sen2sr', request, config);
}

/** The request with no network asked for: what each button starts from. */
export function plainRequest(request) {
  if (!request) return request;
  const { sen2sr, sr4rs, ...rest } = request;
  return rest;
}

/** Which network drew the picture this request made, or ''. */
export function networkOf(request) {
  return Object.keys(NETWORKS).find((key) => request?.[key]) ?? '';
}

