// Turning what you typed into what the page shows.
//
// Articles are stored as plain text, never as HTML. The text is escaped before
// any formatting is applied, so a stored article cannot introduce markup of its
// own -- the only tags that ever reach the page are the ones this file puts
// there. That keeps the newsletter safe to render even if articles.json is ever
// edited by something other than the editor page.

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const escape = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

/** Bold, italic, code and links, applied to already-escaped text. */
function inline(text) {
  return text
    // Code first: whatever is inside it should not then be read as emphasis.
    .replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*]+)\*/g, '$1<em>$2</em>')
    // [text](url), and only to somewhere a link can safely go.
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, href) => {
      if (!/^(https?:\/\/|mailto:|#|\/)/i.test(href)) return whole;
      const external = /^https?:/i.test(href);
      const rel = external ? ' target="_blank" rel="noopener noreferrer"' : '';
      return `<a href="${href}"${rel}>${label}</a>`;
    });
}

/**
 * Render an article body.
 *
 * Blank lines separate blocks. A block may be a heading (## or ###), a quote
 * (>), a list (- or 1.), a rule (---), a picture (!(url) with optional caption)
 * or a paragraph. Anything unrecognised is a paragraph, so nothing a writer
 * types can fail to appear.
 */
export function renderBody(source) {
  const blocks = String(source ?? '').replace(/\r\n?/g, '\n').split(/\n{2,}/);
  const out = [];

  for (const raw of blocks) {
    const block = raw.trim();
    if (!block) continue;
    const lines = block.split('\n');

    if (/^---+$/.test(block)) { out.push('<hr>'); continue; }

    if (/^###\s+/.test(block)) { out.push(`<h3>${inline(escape(block.replace(/^###\s+/, '')))}</h3>`); continue; }
    if (/^##\s+/.test(block))  { out.push(`<h2>${inline(escape(block.replace(/^##\s+/, '')))}</h2>`); continue; }

    if (lines.every((l) => /^>\s?/.test(l))) {
      const inner = inline(escape(lines.map((l) => l.replace(/^>\s?/, '')).join(' ')));
      out.push(`<blockquote>${inner}</blockquote>`);
      continue;
    }

    if (lines.every((l) => /^[-*]\s+/.test(l))) {
      const items = lines.map((l) => `<li>${inline(escape(l.replace(/^[-*]\s+/, '')))}</li>`).join('');
      out.push(`<ul>${items}</ul>`);
      continue;
    }
    if (lines.every((l) => /^\d+[.)]\s+/.test(l))) {
      const items = lines.map((l) => `<li>${inline(escape(l.replace(/^\d+[.)]\s+/, '')))}</li>`).join('');
      out.push(`<ol>${items}</ol>`);
      continue;
    }

    // !(url) or !(url) caption -- a picture, with the caption below it.
    const picture = block.match(/^!\(([^)\s]+)\)\s*(.*)$/s);
    if (picture && /^(https?:\/\/|assets\/|\.\/|\/)/i.test(picture[1])) {
      const alt = escape(picture[2].trim());
      out.push(`<img src="${escape(picture[1])}" alt="${alt}" loading="lazy">`);
      if (alt) out.push(`<p class="fine">${inline(alt)}</p>`);
      continue;
    }

    out.push(`<p>${inline(escape(block)).replace(/\n/g, '<br>')}</p>`);
  }

  return out.join('\n');
}

/** The first bit of an article, for a list that has no summary to show. */
export function excerpt(source, limit = 190) {
  const flat = String(source ?? '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .filter((l) => !/^\s*(#{2,3}\s|>|!\(|---+$)/.test(l))
    .join(' ')
    .replace(/[*`]/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
  if (flat.length <= limit) return flat;
  const cut = flat.slice(0, limit);
  return cut.slice(0, cut.lastIndexOf(' ')) + '…';
}

/** 2026-08-22 -> 22 August 2026, and never a crash on something unparseable. */
export function prettyDate(iso) {
  const d = new Date(`${iso}T12:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso ?? '';
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
}

/** A title becomes the address it lives at. */
export function slugify(title) {
  return String(title).toLowerCase().trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 70) || 'article';
}
