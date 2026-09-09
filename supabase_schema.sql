-- Supabase schema for the upstream TG-FileStore user database.
-- Run once in Supabase SQL Editor.

create table if not exists public.users (
    id bigint primary key,
    join_date text not null,
    ban_status jsonb not null default '{"is_banned":false,"ban_duration":0,"banned_on":"9999-12-31","ban_reason":""}'::jsonb
);

create index if not exists users_banned_idx
    on public.users ((coalesce((ban_status->>'is_banned')::boolean, false)));

alter table public.users enable row level security;
