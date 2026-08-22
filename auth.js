// Signing in with Google, on a site that has no server.
//
// Google hands the browser a signed JWT. With no server to hand it to, this
// file verifies it here: it fetches Google's public keys, checks the signature
// against the one that signed the token, and then checks the claims -- who
// issued it, who it was issued for, whether it has expired, and whether the
// address inside it is one that is allowed.
//
// That is real verification, and it is worth doing properly: a token that fails
// any of these checks is a token that should not get in. What it is NOT is a
// security boundary. Every line of this runs in the visitor's browser and can
// be stepped over by editing it. The boundary that actually holds is that
// nothing here can publish -- writing to the site needs a GitHub token or a
// commit, neither of which signing in provides.

const GOOGLE_JWKS = 'https://www.googleapis.com/oauth2/v3/certs';
const GOOGLE_ISSUERS = ['accounts.google.com', 'https://accounts.google.com'];

// A little slack, because the clock in a browser is not always the clock at
// Google, and a few seconds of drift should not read as a forged token.
const CLOCK_SLACK = 60;

export class AuthError extends Error {}

const b64urlToBytes = (s) => {
  const padded = s.replace(/-/g, '+').replace(/_/g, '/')
    .padEnd(s.length + ((4 - (s.length % 4)) % 4), '=');
  const binary = atob(padded);
  return Uint8Array.from(binary, (c) => c.charCodeAt(0));
};

const b64urlToJson = (s) => JSON.parse(new TextDecoder().decode(b64urlToBytes(s)));

export async function sha256Hex(text) {
  if (!crypto?.subtle) throw new AuthError('This browser will not hash here — open the page over https.');
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

let keyCache = null;
async function googleKeys(jwksUrl) {
  // Cached for the life of the page: the keys rotate on the order of days, and
  // a fetch per sign-in buys nothing.
  if (keyCache?.url === jwksUrl) return keyCache.keys;
  const resp = await fetch(jwksUrl);
  if (!resp.ok) throw new AuthError(`Could not reach Google to check the signature (${resp.status}).`);
  const { keys } = await resp.json();
  keyCache = { url: jwksUrl, keys };
  return keys;
}

/**
 * Check a Google ID token and return the identity inside it.
 *
 * Throws AuthError with something a person can act on. Every failure path says
 * what failed rather than a single "sign-in failed", because the difference
 * between the wrong account and a misconfigured client id is the difference
 * between two very different fixes.
 */
export async function verifyGoogleToken(jwt, { clientId, allowedHashes, jwksUrl = GOOGLE_JWKS } = {}) {
  const parts = String(jwt).split('.');
  if (parts.length !== 3) throw new AuthError('That is not a Google token.');
  const [rawHeader, rawPayload, rawSignature] = parts;

  let header, claims;
  try {
    header = b64urlToJson(rawHeader);
    claims = b64urlToJson(rawPayload);
  } catch {
    throw new AuthError('That token is not readable.');
  }

  if (header.alg !== 'RS256') throw new AuthError(`Unexpected signing algorithm: ${header.alg}.`);

  const key = (await googleKeys(jwksUrl)).find((k) => k.kid === header.kid);
  if (!key) throw new AuthError('Google has no public key matching that token.');

  const publicKey = await crypto.subtle.importKey(
    'jwk',
    { kty: key.kty, n: key.n, e: key.e, alg: 'RS256', ext: true },
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['verify'],
  );

  const signed = new TextEncoder().encode(`${rawHeader}.${rawPayload}`);
  const ok = await crypto.subtle.verify(
    'RSASSA-PKCS1-v1_5', publicKey, b64urlToBytes(rawSignature), signed);
  if (!ok) throw new AuthError('That token was not signed by Google.');

  // Signature good. Now: is it the right token, for this site, still valid?
  if (!GOOGLE_ISSUERS.includes(claims.iss)) throw new AuthError(`Token issued by ${claims.iss}.`);
  if (clientId && claims.aud !== clientId) {
    throw new AuthError('That token was issued for a different site.');
  }
  const now = Math.floor(Date.now() / 1000);
  if (typeof claims.exp !== 'number' || claims.exp + CLOCK_SLACK < now) {
    throw new AuthError('That sign-in has expired — try again.');
  }
  if (typeof claims.iat === 'number' && claims.iat - CLOCK_SLACK > now) {
    throw new AuthError('That token is dated in the future.');
  }
  if (claims.email_verified === false) throw new AuthError('That Google account has no verified address.');
  if (!claims.email) throw new AuthError('That token carries no address.');

  // The allowed address is stored as a hash, so a public repository does not
  // also publish an email address for anything that scrapes them.
  if (allowedHashes?.length) {
    const hash = await sha256Hex(String(claims.email).trim().toLowerCase());
    if (!allowedHashes.includes(hash)) {
      throw new AuthError(`${claims.email} is not the account this editor belongs to.`);
    }
  }

  return { email: claims.email, name: claims.name, picture: claims.picture, expires: claims.exp };
}

/** Load Google's sign-in library, once, and only if it is going to be used. */
export function loadGoogle(src = 'https://accounts.google.com/gsi/client') {
  if (loadGoogle.pending) return loadGoogle.pending;
  loadGoogle.pending = new Promise((resolve, reject) => {
    if (globalThis.google?.accounts?.id) { resolve(globalThis.google); return; }
    const tag = Object.assign(document.createElement('script'), {
      src, async: true, defer: true,
      onload: () => (globalThis.google?.accounts?.id
        ? resolve(globalThis.google)
        : reject(new AuthError('Google’s sign-in library loaded but is not usable here.'))),
      onerror: () => reject(new AuthError('Could not load Google sign-in — check the connection, or use the passphrase.')),
    });
    document.head.appendChild(tag);
  });
  return loadGoogle.pending;
}
