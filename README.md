# EarthViewer — the project page

This branch is the public page only: `index.html` plus the example imagery it
shows. It is served by GitHub Pages and contains none of the application.

The app itself lives on the default branch. To publish this page, set
**Settings → Pages → Source** to *Deploy from a branch*, branch `gh-pages`,
folder `/ (root)`.

Two of the images are generated rather than photographed, and both are made by
the application's own code so the page shows the real thing:

- `assets/merge-one.jpg` / `assets/merge-six.jpg` — one sharp picture of Paris
  sampled coarsely six times with its grid landed a third of a pixel apart, then
  fused by `backend/superres.py`. Because the original is known, the result is
  scored against it.
- `assets/tone-old.jpg` / `assets/tone-new.jpg` — the same pixels brightened two
  ways by `backend/composite.py`: each channel curved separately, and the whole
  pixel curved at once.
