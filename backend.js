// Talking to Supabase, when there is a Supabase to talk to.
//
// The whole site works without this: with nothing configured, `client()` hands
// back null and every page falls back to what it did before -- articles read
// from articles.json, no accounts, no comments, no likes. That is deliberate.
// A half-configured backend should degrade to the old site, not to a broken
// one.
//
// The anon key below is meant to be public. It identifies the project, it does
// not grant anything: what a signed-in person may actually do is decided by the
// row-level security policies in supabase-schema.sql, which run in Postgres
// where nobody browsing the page can reach them.

export const SUPABASE_URL = '';
export const SUPABASE_ANON_KEY = '';

const LIBRARY = 'https://esm.sh/@supabase/supabase-js@2';

export const configured = () => Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);

let clientPromise = null;

/** The Supabase client, or null if this site has no backend configured. */
export async function client() {
  if (!configured()) return null;
  if (!clientPromise) {
    clientPromise = import(/* @vite-ignore */ LIBRARY)
      .then(({ createClient }) => createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
        auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
      }))
      .catch((err) => {
        // A library that will not load must not take the page down with it.
        clientPromise = null;
        throw new Error(`Could not load the accounts library: ${err.message}`);
      });
  }
  return clientPromise;
}

// ── Who is signed in ────────────────────────────────────────────────────────

/** The signed-in person and their profile, or null. */
export async function me() {
  const db = await client();
  if (!db) return null;
  const { data: { user } } = await db.auth.getUser();
  if (!user) return null;

  const { data: profile } = await db
    .from('profiles')
    .select('id, handle, name, avatar_url, role')
    .eq('id', user.id)
    .maybeSingle();

  // The profile is made by a trigger on sign-up. If it is somehow missing,
  // carry on with what the account itself knows rather than showing nothing.
  return {
    id: user.id,
    email: user.email,
    name: profile?.name ?? user.user_metadata?.name ?? user.email,
    handle: profile?.handle ?? null,
    avatar: profile?.avatar_url ?? user.user_metadata?.avatar_url ?? null,
    role: profile?.role ?? 'reader',
    canPublish: ['author', 'admin'].includes(profile?.role),
    isAdmin: profile?.role === 'admin',
  };
}

export async function onAuthChange(handler) {
  const db = await client();
  if (!db) return () => {};
  const { data } = db.auth.onAuthStateChange(() => handler());
  return () => data.subscription.unsubscribe();
}

export async function signOut() {
  const db = await client();
  await db?.auth.signOut();
}

// ── Signing up and in ───────────────────────────────────────────────────────

export async function signUpWithEmail(email, password, name) {
  const db = await client();
  const { data, error } = await db.auth.signUp({
    email,
    password,
    options: { data: { name }, emailRedirectTo: `${location.origin}${location.pathname}` },
  });
  if (error) throw new Error(error.message);
  // With email confirmation switched on there is no session yet, and telling
  // someone they are signed in when they are not is worse than telling them to
  // check their email.
  return { needsConfirmation: !data.session };
}

export async function signInWithEmail(email, password) {
  const db = await client();
  const { error } = await db.auth.signInWithPassword({ email, password });
  if (error) throw new Error(error.message);
}

export async function signInWithGoogle() {
  const db = await client();
  const { error } = await db.auth.signInWithOAuth({
    provider: 'google',
    options: { redirectTo: `${location.origin}${location.pathname}` },
  });
  if (error) throw new Error(error.message);
}

export async function sendReset(email) {
  const db = await client();
  const { error } = await db.auth.resetPasswordForEmail(email, {
    redirectTo: `${location.origin}/account.html`,
  });
  if (error) throw new Error(error.message);
}

export async function updateProfile({ name, handle }) {
  const db = await client();
  const { data: { user } } = await db.auth.getUser();
  if (!user) throw new Error('Not signed in.');
  const { error } = await db.from('profiles')
    .update({ name: name || null, handle: handle || null })
    .eq('id', user.id);
  if (error) {
    // The one failure worth naming, because the fix is obvious once you know.
    throw new Error(error.code === '23505' ? 'That handle is taken.' : error.message);
  }
}

// ── Articles ────────────────────────────────────────────────────────────────

/**
 * Every published article, newest first.
 *
 * Reads from the database when there is one and falls back to articles.json,
 * so the newsletter keeps working through a setup that is half done or a
 * backend that is briefly down.
 */
export async function listArticles() {
  const db = await client();
  if (db) {
    const { data, error } = await db
      .from('articles')
      .select('slug, title, summary, body, author_name, date, published')
      .eq('published', true)
      .order('date', { ascending: false });
    if (!error && data) return data.map((a) => ({ ...a, author: a.author_name }));
  }
  const resp = await fetch('articles.json', { cache: 'no-cache' }).catch(() => null);
  if (!resp?.ok) return [];
  const data = await resp.json();
  return Array.isArray(data) ? data : (data.articles ?? []);
}

export async function saveArticle(article) {
  const db = await client();
  const { data: { user } } = await db.auth.getUser();
  if (!user) throw new Error('Not signed in.');

  const { error } = await db.from('articles').upsert({
    slug: article.slug,
    title: article.title,
    summary: article.summary || null,
    body: article.body,
    date: article.date,
    author_id: user.id,
    author_name: article.author || null,
    published: true,
  }, { onConflict: 'slug' });

  if (error) {
    // A policy refusing the write is the ordinary case for a reader account,
    // and deserves an explanation rather than a Postgres error code.
    if (error.code === '42501' || /row-level security/i.test(error.message)) {
      throw new Error('This account is not allowed to publish. An admin can change that on the account page.');
    }
    throw new Error(error.message);
  }
}

// ── Likes ───────────────────────────────────────────────────────────────────

export async function likeState(slug) {
  const db = await client();
  if (!db) return { count: 0, liked: false };
  const [{ count }, { data: { user } }] = await Promise.all([
    db.from('likes').select('*', { count: 'exact', head: true }).eq('article_slug', slug),
    db.auth.getUser(),
  ]);
  let liked = false;
  if (user) {
    const { data } = await db.from('likes')
      .select('article_slug').eq('article_slug', slug).eq('user_id', user.id).maybeSingle();
    liked = Boolean(data);
  }
  return { count: count ?? 0, liked };
}

export async function toggleLike(slug) {
  const db = await client();
  const { data: { user } } = await db.auth.getUser();
  if (!user) throw new Error('Sign in to like this.');

  const { liked } = await likeState(slug);
  const query = liked
    ? db.from('likes').delete().eq('article_slug', slug).eq('user_id', user.id)
    : db.from('likes').insert({ article_slug: slug, user_id: user.id });
  const { error } = await query;
  // A duplicate key means it was already liked -- the state was stale, not
  // wrong, so re-reading it is the right answer rather than an error.
  if (error && error.code !== '23505') throw new Error(error.message);
  return likeState(slug);
}

// ── Comments ────────────────────────────────────────────────────────────────

export async function listComments(slug) {
  const db = await client();
  if (!db) return [];
  const { data, error } = await db
    .from('comments')
    .select('id, body, created_at, edited_at, user_id, profiles ( name, handle, avatar_url )')
    .eq('article_slug', slug)
    .eq('deleted', false)
    .order('created_at', { ascending: true });
  if (error) throw new Error(error.message);
  return (data ?? []).map((c) => ({
    id: c.id,
    body: c.body,
    at: c.created_at,
    edited: c.edited_at,
    userId: c.user_id,
    name: c.profiles?.name ?? c.profiles?.handle ?? 'Someone',
    avatar: c.profiles?.avatar_url ?? null,
  }));
}

export async function addComment(slug, body) {
  const db = await client();
  const { data: { user } } = await db.auth.getUser();
  if (!user) throw new Error('Sign in to comment.');
  const text = String(body).trim();
  if (!text) throw new Error('Nothing to post.');
  if (text.length > 4000) throw new Error('That is longer than a comment can be.');

  const { error } = await db.from('comments')
    .insert({ article_slug: slug, user_id: user.id, body: text });
  if (error) throw new Error(error.message);
}

export async function deleteComment(id) {
  const db = await client();
  const { error } = await db.from('comments').delete().eq('id', id);
  if (error) throw new Error(error.message);
}
