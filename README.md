# EarthViewer — the project page

This branch is the public page only: `index.html` plus the example imagery it
shows. It is served by GitHub Pages and contains none of the application.

The app itself lives on the default branch. To publish this page, set
**Settings → Pages → Source** to *Deploy from a branch*, branch `gh-pages`,
folder `/ (root)`.

## What the example imagery is

Every example picture on the page is **applied-view**: existing high-detail
imagery of a place laid over new Sentinel-2 and other current data, registered
to the same ground. The fresh pass carries what is true now — colour, change,
radar, cloud, heat — and the existing layer carries the fine texture that
Sentinel-2's 10-metre pixels never held.

None of the frames is original imagery from a single free pass, and none is a
photograph taken on a stated date. The page says so at each picture rather than
only once, because a reader who scrolls past the explainer still has to be told
what they are looking at. Keep that labelling in place when editing the page.

The flagship example is Heathrow, two frames of the same ground:

- `assets/heathrow-wide.webp` — the airport at a scale its detail supports.
- `assets/heathrow-close.webp` — the boxed part of it magnified 4×.

The pairing is not asserted. The close frame was located inside the wide one
by normalised cross-correlation: 0.995 at 280×145 px offset (208, 356), against
0.783 for the next-best position. Measured at the *same ground scale* the two
carry the same fine structure — 0.251 against 0.255, stable across filter widths
1.0–3.0 — so the close frame is not worse imagery, it is the same data magnified
past what it holds. Do not rewrite that section to claim the wide frame resolves
more; the measurement says otherwise.

Two of the images are generated rather than photographed, and both are made by
the application's own code so the page shows the real thing:

- `assets/merge-one.jpg` / `assets/merge-six.jpg` — one sharp picture of Paris
  sampled coarsely six times with its grid landed a third of a pixel apart, then
  fused by `backend/superres.py`. Because the original is known, the result is
  scored against it.
- `assets/tone-old.jpg` / `assets/tone-new.jpg` — the same pixels brightened two
  ways by `backend/composite.py`: each channel curved separately, and the whole
  pixel curved at once.
