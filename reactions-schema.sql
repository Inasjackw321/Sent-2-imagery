-- Reaction counts for EarthViewer. This is the whole database.
--
-- Paste it into the Supabase SQL editor and press Run, once. There are no
-- accounts, no profiles and no comments -- just a count of how many people
-- pressed each button on each article.

create table if not exists public.reactions (
  article_slug text not null,
  emoji        text not null check (emoji in ('heart', 'fire', 'wow', 'think')),
  -- A random id from the visitor's own browser. Not an account, and not a
  -- person: it exists so a page can light up the buttons you already pressed.
  visitor      text not null,
  created_at   timestamptz not null default now(),
  -- Pressing the same button twice cannot count twice, whatever the page does.
  primary key (article_slug, emoji, visitor)
);

create index if not exists reactions_by_article on public.reactions (article_slug);

alter table public.reactions enable row level security;

-- Anyone may read the counts, add a reaction, or take their own back. That is
-- the point of an anonymous counter, and it is worth being clear-eyed about
-- what it means: without accounts there is no way to prove a reaction came
-- from a different person, so the numbers are a rough measure of interest and
-- not a poll anybody should be relying on.
drop policy if exists "counts are public" on public.reactions;
create policy "counts are public" on public.reactions for select using (true);

drop policy if exists "anyone may react" on public.reactions;
create policy "anyone may react" on public.reactions for insert with check (true);

drop policy if exists "anyone may unreact" on public.reactions;
create policy "anyone may unreact" on public.reactions for delete using (true);
