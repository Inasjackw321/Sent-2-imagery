// Reaction counts, and nothing else.
//
// Articles live in articles.json, in the repository. Reactions are the one
// thing on this site that cannot: a count everybody shares has to be kept
// somewhere central, and a static host has nowhere to keep it. So there is a
// single small table, and this file is all the code that talks to it.
//
// There are no accounts. Nobody signs up, nobody signs in. A reaction is
// anonymous, and a visitor is remembered only by a random id kept in their own
// browser -- enough to know which buttons to light up when they come back, and
// to stop a double-tap counting twice.
//
// With the two values below left empty the reaction bar simply does not appear.
// It never shows a count it cannot back up.

export const SUPABASE_URL = '';
export const SUPABASE_ANON_KEY = '';

const LIBRARY = 'https://esm.sh/@supabase/supabase-js@2';
const VISITOR_KEY = 'earthviewer.visitor';

/** The reactions on offer, in the order they are shown. */
export const REACTIONS = [
  { key: 'heart', emoji: '♥',  label: 'Like this' },
  { key: 'fire',  emoji: '🔥', label: 'This is good' },
  { key: 'wow',   emoji: '😮', label: 'Surprising' },
  { key: 'think', emoji: '🤔', label: 'Makes you think' },
];

export const configured = () => Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);

let clientPromise = null;

async function client() {
  if (!configured()) return null;
  if (!clientPromise) {
    clientPromise = import(/* @vite-ignore */ LIBRARY)
      .then(({ createClient }) => createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
        auth: { persistSession: false },
      }))
      .catch((err) => {
        // A library that will not load must not take the article down with it.
        clientPromise = null;
        throw new Error(`Reactions are not available: ${err.message}`);
      });
  }
  return clientPromise;
}

/**
 * A random id for this browser.
 *
 * Not an account and not an identity -- it says nothing about who anyone is.
 * It exists so the page knows which reactions to show as already given. A
 * browser that will not keep it still gets to react; it just will not remember
 * having done so.
 */
function visitor() {
  try {
    let id = localStorage.getItem(VISITOR_KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(VISITOR_KEY, id);
    }
    return id;
  } catch {
    return crypto.randomUUID();
  }
}

/** Counts for every reaction on an article, and which ones this browser gave. */
export async function reactionsFor(slug) {
  const db = await client();
  const blank = Object.fromEntries(REACTIONS.map((r) => [r.key, { count: 0, mine: false }]));
  if (!db) return blank;

  const { data, error } = await db
    .from('reactions')
    .select('emoji, visitor')
    .eq('article_slug', slug);
  if (error) throw new Error(error.message);

  const me = visitor();
  for (const row of data ?? []) {
    const entry = blank[row.emoji];
    if (!entry) continue; // A reaction that has since been retired.
    entry.count += 1;
    if (row.visitor === me) entry.mine = true;
  }
  return blank;
}

/** React, or take it back if this browser has already reacted that way. */
export async function react(slug, emoji) {
  const db = await client();
  if (!db) throw new Error('Reactions are not switched on.');
  if (!REACTIONS.some((r) => r.key === emoji)) throw new Error('Unknown reaction.');

  const me = visitor();
  const current = await reactionsFor(slug);

  if (current[emoji].mine) {
    const { error } = await db.from('reactions').delete()
      .eq('article_slug', slug).eq('emoji', emoji).eq('visitor', me);
    if (error) throw new Error(error.message);
  } else {
    const { error } = await db.from('reactions')
      .insert({ article_slug: slug, emoji, visitor: me });
    // Already there: the count was stale rather than wrong, so re-reading it
    // is the right answer instead of an error.
    if (error && error.code !== '23505') throw new Error(error.message);
  }
  return reactionsFor(slug);
}

/** Every article, newest first. They live in the repository, not a database. */
export async function listArticles() {
  const resp = await fetch('articles.json', { cache: 'no-cache' }).catch(() => null);
  if (!resp?.ok) return [];
  const data = await resp.json();
  const articles = Array.isArray(data) ? data : (data.articles ?? []);
  return [...articles].sort((a, b) => String(b.date).localeCompare(String(a.date)));
}
