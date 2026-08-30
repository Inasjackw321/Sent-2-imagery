// Letting a click reach the thing that was actually clicked.
//
// Leaflet draws each canvas layer group onto its own <canvas>, and a canvas is
// opaque to the pointer across its whole rectangle -- unlike an SVG path,
// which Leaflet's stylesheet makes transparent everywhere it has not painted.
// So the topmost canvas over the map receives every click on it.
//
// That would be fine if it passed on what it did not want. It does not. Its
// click handler hit-tests its own features and, finding none, fires the event
// at the *map* -- the canvas underneath is never asked. Every canvas below the
// top one is therefore unclickable, however carefully it was drawn.
//
// In this app that is not a hypothetical. Ships are drawn in a pane above the
// overlay pane, so turning ships on made every seismograph unreachable; and
// within the overlay pane the station canvas is built after the earthquake
// one, so showing stations quietly took the earthquakes away. Both look
// exactly like a broken click, which is what they were reported as.
//
// The fix is to make a miss fall through. When a canvas is clicked and has
// nothing under the pointer, the canvases below it are asked in stacking
// order, and the first one with a feature there handles the click. Nothing
// else changes: a click that hits the top canvas behaves as it always did,
// and a click that hits nothing anywhere still reaches the map.

/**
 * The topmost interactive feature a renderer has under this event, if any.
 *
 * The same test Leaflet's own handler makes, including its refusal to count a
 * click that was really the end of a drag.
 */
function featureAt(renderer, event) {
  const map = renderer._map;
  if (!map) return null;
  const point = map.mouseEventToLayerPoint(event);
  let found = null;
  // Draw order, so the last one drawn -- the one visibly on top -- wins,
  // matching what the eye expects to be clicking.
  for (let order = renderer._drawFirst; order; order = order.next) {
    const layer = order.layer;
    if (!layer.options.interactive || !layer._containsPoint(point)) continue;
    if ((event.type === 'click' || event.type === 'preclick')
      && map._draggableMoved(layer)) continue;
    found = layer;
  }
  return found;
}

/**
 * The canvas renderers under the pointer, nearest the viewer first.
 *
 * `elementsFromPoint` already answers this the way the browser itself decides
 * what gets a click -- z-index, DOM order and pointer-events all accounted
 * for -- so there is no stacking logic here to disagree with the real one.
 */
function renderersUnder(event, map) {
  const out = [];
  for (const node of document.elementsFromPoint(event.clientX, event.clientY)) {
    const renderer = node.__leafletRenderer;
    if (renderer && renderer._map === map) out.push(renderer);
  }
  return out;
}

/**
 * Teach Leaflet's canvas renderer to pass on what it does not want.
 *
 * Applied to the prototype once, so every renderer -- including any added
 * later -- gets it without having to remember to ask.
 */
export function shareCanvasClicks() {
  const proto = L.Canvas.prototype;
  if (proto.__reachPatched) return;
  proto.__reachPatched = true;

  // The renderer is stamped on its own canvas so that an element found by
  // elementsFromPoint can be turned back into the renderer that owns it.
  const initContainer = proto._initContainer;
  proto._initContainer = function () {
    initContainer.call(this);
    this._container.__leafletRenderer = this;
  };

  const onClick = proto._onClick;
  proto._onClick = function (event) {
    if (!featureAt(this, event)) {
      for (const other of renderersUnder(event, this._map)) {
        if (other === this || !featureAt(other, event)) continue;
        other._onClick(event);
        return undefined;
      }
    }
    return onClick.call(this, event);
  };

  const onMouseMove = proto._onMouseMove;
  proto._onMouseMove = function (event) {
    const map = this._map;
    if (map && !map.dragging.moving() && !map._animatingZoom && !featureAt(this, event)) {
      for (const other of renderersUnder(event, map)) {
        if (other === this || !featureAt(other, event)) continue;
        // Whatever this renderer was hovering, the pointer has left it -- and
        // returning early means its own handler will not be the one to notice.
        if (this._hoveredLayer) this._handleMouseOut(event);
        other._onMouseMove(event);
        // The cursor is decided by the element the pointer is actually over,
        // which is this canvas and not the one that answered -- so without
        // this the pointer stays an open hand over something clickable, and
        // nothing invites the click that would work.
        this._container.classList.add('leaflet-interactive');
        return undefined;
      }
      this._container.classList.remove('leaflet-interactive');
    }
    return onMouseMove.call(this, event);
  };
}
