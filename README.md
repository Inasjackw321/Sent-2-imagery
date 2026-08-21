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

Two of the pairs are demonstrations produced by the application's own code, and
they are applied-view too — the source they start from is existing high-detail
imagery:

- `assets/merge-one.jpg` / `assets/merge-six.jpg` — one sharp picture of Paris
  sampled coarsely six times with its grid landed a third of a pixel apart, then
  fused by `backend/superres.py`. A test of the fusion step on data whose answer
  is known — not a measurement of what six real passes would give.
- `assets/tone-old.jpg` / `assets/tone-new.jpg` — the same pixels brightened two
  ways by `backend/composite.py`: each channel curved separately, and the whole
  pixel curved at once.

## Numbers on the page

Only two remain, both in the Heathrow section, and both re-derivable from the
assets in this repo with the correlation described above.

Several figures were removed rather than carried forward, because they could not
be reproduced from anything checked in:

- *35% of the detail recovered* and *16% closer to the real ground* (merge
  section) — these were scored against a ground-truth original that is not in
  this repo, so there is nothing here to verify them against.
- *hue drift 2.4° / 0.0° / 21.9°* (tone section) — measuring the two published
  JPEGs gives a mean difference of 4.6°, not 2.4°, and JPEG chroma subsampling
  alone perturbs it. The section now argues the point from the arithmetic
  instead: brightening the whole pixel scales R, G and B by the same factor, so
  the ratios that define hue are unchanged by construction.
- *18% darker than the roof around them* (hangar section) — measuring the ringed
  regions directly gives roughly 58% and 36%, so 18% was wrong whatever it
  originally referred to. The count of two marks is stated in prose instead.

Do not reintroduce a statistic that cannot be recomputed from this repo.
