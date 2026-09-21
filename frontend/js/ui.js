// Small DOM helpers shared by every panel.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const child of children.flat()) {
    if (child != null) node.append(child.nodeType ? child : document.createTextNode(child));
  }
  return node;
}

/** A labelled range input that reports its value live. */
function slider(host, spec, onInput) {
  const { key, label, min, max, step = 1, value, unit = '', format } = spec;
  const readout = el('b');
  const input = el('input', { type: 'range', min, max, step, value });
  const show = (v) => { readout.textContent = format ? format(v) : `${v}${unit}`; };
  show(value);
  input.addEventListener('input', () => {
    const v = parseFloat(input.value);
    show(v);
    onInput?.(v, key);
  });
  host.append(el('div', { class: 'slider', dataset: { key } },
    el('div', { class: 'slider-head' }, el('span', {}, label), readout),
    input));
  return {
    get: () => parseFloat(input.value),
    set: (v) => { input.value = v; show(v); },
    input,
  };
}

/** Build a bank of sliders; returns {key: control}. */
export function sliderBank(host, specs, onInput) {
  host.innerHTML = '';
  const out = {};
  for (const spec of specs) out[spec.key] = slider(host, spec, onInput);
  out.values = () => Object.fromEntries(
    Object.entries(out).filter(([, c]) => c && c.get).map(([k, c]) => [k, c.get()])
  );
  return out;
}

let toastTimer = 0;
export function toast(message, kind = '') {
  const dock = $('#toasts');
  const node = el('div', { class: `toast ${kind}` }, message);
  dock.append(node);
  clearTimeout(toastTimer);
  setTimeout(() => {
    node.style.transition = 'opacity .3s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 320);
    // A caveat takes as long to read as an error and is just as easy to
    // miss, so it gets the same dwell.
  }, kind === 'err' || kind === 'warn' ? 7000 : 3800);
}

let busyDepth = 0;
function busy(on, text = 'Working…') {
  busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
  const node = $('#busy');
  $('#busyText').textContent = text;
  node.hidden = busyDepth === 0;
}

export async function withBusy(text, fn) {
  busy(true, text);
  try { return await fn(); }
  finally { busy(false); }
}

/**
 * Hand a file to the person, by whichever route their device actually has.
 *
 * A download is the wrong verb on a phone. An <a download> saves into Files,
 * which is not where anybody looks for a picture -- "the image doesn't save
 * to photos" is exactly what that feels like, and it is not a bug in the
 * picture. The share sheet is the route that offers "Save Image", and on iOS
 * it is the only one.
 *
 * So: share where sharing a file is possible, download where it is not.
 * Returns which happened, so the caller can say so rather than claiming a
 * save that went somewhere else.
 *
 * Called straight from the click handler's own promise chain. The share sheet
 * needs the user gesture that started it and a browser will refuse one that
 * arrives too long afterwards, which is why the picture is built before this
 * is reached rather than inside it.
 */
export async function handOver(blob, filename, title = '') {
  const type = blob.type || 'image/png';
  try {
    const file = new File([blob], filename, { type });
    if (navigator.share && navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file], title });
      return 'shared';
    }
  } catch (err) {
    // Cancelling the share sheet is not a failure and must not fall through
    // to a download -- the person said no.
    if (err?.name === 'AbortError') return 'cancelled';
  }
  download(blob, filename);
  return 'downloaded';
}

export function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('Image could not be decoded'));
    img.src = src;
  });
}

export const fmt = {
  area(km2) {
    if (km2 == null) return '—';
    if (km2 < 1) return `${(km2 * 1e6).toLocaleString(undefined, { maximumFractionDigits: 0 })} m²`;
    if (km2 < 100) return `${km2.toFixed(2)} km²`;
    return `${Math.round(km2).toLocaleString()} km²`;
  },
  distance(m) {
    return m >= 1000 ? `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km` : `${Math.round(m)} m`;
  },
  date(iso) {
    if (!iso) return '—';
    return new Date(iso + (iso.length === 10 ? 'T00:00:00Z' : '')).toLocaleDateString(undefined, {
      day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
    });
  },
  when(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleString(undefined, {
      weekday: 'short', day: 'numeric', month: 'short',
      hour: '2-digit', minute: '2-digit',
    });
  },
  // A countdown reads better in the units a person would use out loud.
  duration(hours) {
    const h = Math.abs(hours);
    if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
    if (h < 48) return `${h < 10 ? h.toFixed(1) : Math.round(h)} h`;
    const days = Math.floor(h / 24);
    const rest = Math.round(h - days * 24);
    return rest ? `${days} d ${rest} h` : `${days} d`;
  },
  coord(lon, lat) {
    const ns = lat >= 0 ? 'N' : 'S';
    const ew = lon >= 0 ? 'E' : 'W';
    return `${Math.abs(lat).toFixed(4)}° ${ns}, ${Math.abs(lon).toFixed(4)}° ${ew}`;
  },
  bytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1024 ** 2).toFixed(1)} MB`;
  },
};

export function debounce(fn, ms = 120) {
  let t = 0;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/**
 * The rectangle on screen, widened a little, as something an API will accept.
 *
 * Two things have to happen here. The padding is what stops an ordinary pan
 * from re-asking for data that is already drawn -- but zoomed out, padding a
 * world view pushes the edges past the poles and past the antimeridian, and a
 * service that validates its inputs rejects the whole request. That was a
 * layer that worked everywhere except zoomed out, which is exactly where you
 * would go looking for everything at once.
 */
export function askableBounds(map, margin = 0.35) {
  const box = map.getBounds().pad(margin);
  return {
    west: Math.max(box.getWest(), -180),
    south: Math.max(box.getSouth(), -90),
    east: Math.min(box.getEast(), 180),
    north: Math.min(box.getNorth(), 90),
  };
}
