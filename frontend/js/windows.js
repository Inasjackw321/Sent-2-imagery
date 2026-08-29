// Floating windows: several cameras and traces open at once.
//
// These used to be one panel each, so opening a second camera closed the
// first. That is the wrong shape for the job -- the reason to put cameras and
// seismographs on a map of satellite imagery is to compare them, and comparing
// means seeing two things at the same time.
//
// New windows cascade up and to the left from the bottom-right corner rather
// than landing on top of each other, and each can be dragged by its title bar.
// Clicking one brings it forward.

import { $, el } from './ui.js';

// How far each new window is offset from the one before, so a stack of them
// reads as a stack rather than as one window.
const STEP = 26;

// After this many steps the cascade would walk off the top of the screen, so
// it starts again from the corner.
const CASCADE = 6;

const open = new Map();      // id -> { node, spec }
let opened = 0;              // how many have been opened, for the cascade
let front = 40;              // the z-index of the frontmost window

function layer() {
  let node = $('#windowLayer');
  if (!node) {
    node = el('div', { class: 'win-layer', id: 'windowLayer' });
    document.body.append(node);
  }
  return node;
}

/**
 * Open a window, or bring an already-open one forward.
 *
 * `body` is built by the caller and owned by it. `onClose` is called when the
 * window goes away by any route -- the button, `closeWindow`, or `closeAll` --
 * so whatever the body left running can be stopped in one place instead of
 * three.
 */
export function openWindow({ id, title, where, badge, link, body, foot, onClose }) {
  const already = open.get(id);
  if (already) {
    raise(already.node);
    return already.node;
  }

  const node = el('div', { class: 'win', dataset: { win: id } },
    el('div', { class: 'win-bar' },
      badge ? el('span', { class: `win-badge ${badge.className ?? ''}` }, badge.text) : null,
      el('b', { class: 'win-title' }, title),
      el('span', { class: 'win-where' }, where ?? ''),
      link ? el('a', {
        class: 'win-out', href: link, target: '_blank', rel: 'noopener noreferrer',
        title: 'Open the source in a new tab',
      }, '↗') : null,
      el('button', {
        class: 'win-close', title: 'Close', onclick: () => closeWindow(id),
      }, '×')),
    el('div', { class: 'win-body' }, body),
    foot ? el('div', { class: 'win-foot' }, foot) : null);

  place(node);
  node.addEventListener('pointerdown', () => raise(node));
  drag(node);

  open.set(id, { node, onClose });
  layer().append(node);
  raise(node);
  return node;
}

export function closeWindow(id) {
  const held = open.get(id);
  if (!held) return;
  open.delete(id);
  held.node.remove();
  // After the node is gone, so a handler that inspects the document sees the
  // state it is being told about.
  held.onClose?.();
  if (!open.size) opened = 0;
}

export function closeAll(match = () => true) {
  for (const id of [...open.keys()]) {
    if (match(id)) closeWindow(id);
  }
}

export const isOpen = (id) => open.has(id);
export const openIds = () => [...open.keys()];

/** Put a new window on the cascade, clear of the corner it starts from. */
function place(node) {
  const step = opened % CASCADE;
  opened += 1;
  node.style.right = `${18 + step * STEP}px`;
  node.style.bottom = `${18 + step * STEP}px`;
}

function raise(node) {
  front += 1;
  node.style.zIndex = String(front);
}

/**
 * Drag by the title bar.
 *
 * Pointer capture rather than listeners on the document: a fast drag that
 * leaves the window behind still delivers its moves here, and the browser
 * takes the listener away when the pointer is released even if the pointer-up
 * lands somewhere unexpected.
 */
function drag(node) {
  const bar = node.querySelector('.win-bar');
  bar.addEventListener('pointerdown', (event) => {
    // Not the close button or the link: those are for pressing, not dragging.
    if (event.target.closest('button, a')) return;
    event.preventDefault();

    const box = node.getBoundingClientRect();
    // Switch from the bottom-right anchoring to explicit coordinates, so the
    // window does not jump the moment it is picked up.
    node.style.left = `${box.left}px`;
    node.style.top = `${box.top}px`;
    node.style.right = 'auto';
    node.style.bottom = 'auto';

    const grabX = event.clientX - box.left;
    const grabY = event.clientY - box.top;
    bar.setPointerCapture(event.pointerId);
    node.classList.add('is-dragging');

    const move = (e) => {
      // Kept on screen: a window dragged off the edge cannot be dragged back,
      // and its title bar is the only handle it has.
      const x = Math.min(Math.max(e.clientX - grabX, 8 - box.width + 90), window.innerWidth - 90);
      const y = Math.min(Math.max(e.clientY - grabY, 0), window.innerHeight - 34);
      node.style.left = `${x}px`;
      node.style.top = `${y}px`;
    };
    const drop = () => {
      bar.removeEventListener('pointermove', move);
      node.classList.remove('is-dragging');
    };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', drop, { once: true });
    bar.addEventListener('pointercancel', drop, { once: true });
  });
}
