-- EarthViewer accounts, comments, likes and articles.
--
-- Run this once in the Supabase SQL editor. It is written to be run again
-- safely: everything is created if it does not already exist.
--
-- The important thing here is that none of the rules below live in the browser.
-- Row-level security is enforced by Postgres, so a reader who edits the page,
-- forges a request or calls the API directly still cannot write a comment as
-- someone else, like a thing twice, or publish an article without the role for
-- it. This is the part the static site could never do.

-- ── Profiles ────────────────────────────────────────────────────────────────
-- One row per account, created automatically on sign-up.

create table if not exists public.profiles (
  id          uuid primary key references auth.users on delete cascade,
  handle      text unique,
  name        text,
  avatar_url  text,
  -- reader: comment and like. author: also publish. admin: also moderate.
  role        text not null default 'reader'
              check (role in ('reader', 'author', 'admin')),
  created_at  timestamptz not null default now()
);

alter table public.profiles enable row level security;

-- Asking "is this person an admin?" from inside a policy on profiles would send
-- the policy back through itself. A security-definer function reads the table
-- once, outside RLS, and breaks that loop.
create or replace function public.is_admin()
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.profiles where id = auth.uid() and role = 'admin');
$$;

create or replace function public.may_publish()
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.profiles
    where id = auth.uid() and role in ('author', 'admin'));
$$;

-- A new sign-up gets a profile without having to ask for one.
create or replace function public.on_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, name, avatar_url)
  values (
    new.id,
    coalesce(new.raw_user_meta_data ->> 'name',
             new.raw_user_meta_data ->> 'full_name',
             split_part(new.email, '@', 1)),
    new.raw_user_meta_data ->> 'avatar_url')
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.on_new_user();

-- Nobody promotes themselves. The role can only be changed by an admin; any
-- other update silently keeps the role it had, rather than failing in a way a
-- caller could learn from.
create or replace function public.guard_role()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.role is distinct from old.role and not public.is_admin() then
    new.role := old.role;
  end if;
  return new;
end;
$$;

drop trigger if exists profiles_guard_role on public.profiles;
create trigger profiles_guard_role
  before update on public.profiles
  for each row execute function public.guard_role();

drop policy if exists "profiles are public" on public.profiles;
create policy "profiles are public"
  on public.profiles for select using (true);

drop policy if exists "own profile insert" on public.profiles;
create policy "own profile insert"
  on public.profiles for insert with check (auth.uid() = id);

drop policy if exists "own profile update" on public.profiles;
create policy "own profile update"
  on public.profiles for update using (auth.uid() = id or public.is_admin());

-- ── Articles ────────────────────────────────────────────────────────────────

create table if not exists public.articles (
  slug        text primary key,
  title       text not null,
  summary     text,
  body        text not null,
  author_id   uuid references public.profiles on delete set null,
  author_name text,
  published   boolean not null default true,
  date        date not null default current_date,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table public.articles enable row level security;

drop policy if exists "published articles are public" on public.articles;
create policy "published articles are public"
  on public.articles for select
  using (published or author_id = auth.uid() or public.is_admin());

-- Publishing is the role check, done here rather than in the page.
drop policy if exists "authors publish" on public.articles;
create policy "authors publish"
  on public.articles for insert
  with check (public.may_publish() and author_id = auth.uid());

drop policy if exists "authors edit their own" on public.articles;
create policy "authors edit their own"
  on public.articles for update
  using ((public.may_publish() and author_id = auth.uid()) or public.is_admin());

drop policy if exists "authors delete their own" on public.articles;
create policy "authors delete their own"
  on public.articles for delete
  using ((public.may_publish() and author_id = auth.uid()) or public.is_admin());

-- ── Comments ────────────────────────────────────────────────────────────────

create table if not exists public.comments (
  id           bigint generated always as identity primary key,
  article_slug text not null,
  user_id      uuid not null references public.profiles on delete cascade,
  body         text not null check (length(trim(body)) between 1 and 4000),
  created_at   timestamptz not null default now(),
  edited_at    timestamptz,
  deleted      boolean not null default false
);

create index if not exists comments_by_article
  on public.comments (article_slug, created_at desc);

alter table public.comments enable row level security;

drop policy if exists "comments are public" on public.comments;
create policy "comments are public"
  on public.comments for select using (not deleted or public.is_admin());

-- user_id has to be the caller's own id, so a comment cannot be signed with
-- somebody else's name however the request is built.
drop policy if exists "signed-in people comment" on public.comments;
create policy "signed-in people comment"
  on public.comments for insert with check (auth.uid() = user_id);

drop policy if exists "edit own comment" on public.comments;
create policy "edit own comment"
  on public.comments for update using (auth.uid() = user_id or public.is_admin());

drop policy if exists "delete own comment" on public.comments;
create policy "delete own comment"
  on public.comments for delete using (auth.uid() = user_id or public.is_admin());

-- ── Likes ───────────────────────────────────────────────────────────────────
-- The primary key is what makes a second like from the same person impossible,
-- rather than a check in the page that a determined caller could skip.

create table if not exists public.likes (
  article_slug text not null,
  user_id      uuid not null references public.profiles on delete cascade,
  created_at   timestamptz not null default now(),
  primary key (article_slug, user_id)
);

alter table public.likes enable row level security;

drop policy if exists "likes are public" on public.likes;
create policy "likes are public"
  on public.likes for select using (true);

drop policy if exists "like as yourself" on public.likes;
create policy "like as yourself"
  on public.likes for insert with check (auth.uid() = user_id);

drop policy if exists "unlike your own" on public.likes;
create policy "unlike your own"
  on public.likes for delete using (auth.uid() = user_id);

-- Counting likes without handing out the list of who liked what.
create or replace view public.like_counts as
  select article_slug, count(*)::int as likes
  from public.likes group by article_slug;

-- ── Making yourself an admin ────────────────────────────────────────────────
-- Sign up through the site first, so the account exists, then run this once
-- with your own address. From then on you can set anyone else's role from the
-- account page.
--
--   update public.profiles set role = 'admin'
--   where id = (select id from auth.users where email = 'you@example.com');
