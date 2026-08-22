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

## Accounts, comments and likes

`backend.js` holds the Supabase URL and anon key. **Left empty, the whole site
behaves as it did before**: the newsletter reads `articles.json`, the editor
uses the passphrase, and there are no accounts, comments or likes. Nothing on
the site depends on the backend being there.

With it configured:

- `account.html` — sign up, sign in, profile, and (for admins) setting roles.
- Likes and comments appear on each article.
- The editor opens for an account with the `author` or `admin` role and
  publishes straight to the database — no GitHub token.

### Setting it up

1. Create a project at supabase.com (the free tier is enough).
2. Run `supabase-schema.sql` once in its SQL editor.
3. Copy Project URL and the **anon** key into `backend.js`.
4. Sign up on `account.html`, then run the last query in the schema file with
   your address to make yourself an admin.
5. Optional: Authentication → Providers → Google, to allow Google sign-in.

The anon key belongs in the repository — it identifies the project and grants
nothing on its own. **Never** put the `service_role` key in this repo; it
bypasses every policy below.

### Where the rules live

All of them are in Postgres, in `supabase-schema.sql`, not in the pages:

- A comment's `user_id` must equal the caller's own id, so a comment cannot be
  signed with someone else's name however the request is made.
- Likes are keyed on `(article_slug, user_id)`, so a second like is impossible
  rather than merely discouraged.
- Publishing requires the `author` or `admin` role, checked by a policy.
- A `before update` trigger keeps anyone from changing their own role. The
  account page reads the row back after a role change rather than assuming the
  update did what it looked like.

Comment text is rendered with `textContent`, never as markup — tested by
posting `<img onerror>` and `<script>` and confirming they come out as visible
text.

## The newsletter

`newsletter.html` lists and reads articles; `write.html` is the editor behind a
passphrase; `articles.json` is the store; `render.js` turns article text into
HTML and `pages.css` styles both pages.

Articles are stored as **plain text, never HTML**. `render.js` escapes the text
before applying any formatting, so the only tags that ever reach the page are
the ones it puts there. Keep it that way — do not add a raw-HTML block type.

### Signing in with Google

`auth.js` verifies Google's ID token in the browser: it fetches Google's JWKS,
checks the RS256 signature against the key named in the token header, then
checks `iss`, `aud`, `exp`, `iat`, `email_verified` and the address against a
list of SHA-256 hashes. The allowed address is stored hashed so a public repo
does not also publish an email address for scrapers.

That verification is real and was tested against forged tokens — tampered
payload, `alg: none`, wrong audience, expired, future-dated, unknown key id,
wrong issuer, wrong address. All were rejected; only the correctly signed token
for the allowed address was accepted.

To turn it on, create an OAuth 2.0 Client ID (Web application) in the Google
Cloud console, add the site's origin (`https://inasjackw321.github.io`) as an
authorised JavaScript origin, and set `GOOGLE_CLIENT_ID` in `write.html`. Left
empty, the button is hidden and the passphrase remains the way in. The client ID
is not a secret — Google serves it to every visitor who loads the button.

### About the passphrase

The gate on `write.html` is a SHA-256 check that runs in the browser. Only the
hash is in the repository; the passphrase itself is not, and must not be added.

It is obfuscation, not access control, and the page says so in as many words. A
static host has nowhere to put a real check — and this applies equally to the
Google sign-in above: the token check is genuine, but it runs in the visitor's
browser and can be stepped over by editing the page. The security property that actually
holds is different: the editor cannot publish by itself. Writing to the site
needs either a commit or a GitHub token with write access to this repository, so
someone who read past the gate would get a text box and nothing more.

To change the passphrase, replace `PASS_HASH` in `write.html` with the SHA-256
of the new one:

    printf %s 'the-new-passphrase' | sha256sum
