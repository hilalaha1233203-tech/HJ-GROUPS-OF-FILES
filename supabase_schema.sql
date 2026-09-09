-- Supabase schema for HJ GROUPS OF FILES Telegram bot
-- Run this once in Supabase SQL Editor.

create table if not exists public.users (
    id bigint primary key,
    join_date text not null,
    is_banned boolean not null default false,
    ban_duration bigint not null default 0,
    banned_on text not null,
    ban_reason text not null default ''
);

create index if not exists users_is_banned_idx on public.users (is_banned);

-- The bot runs server-side and should use the Supabase secret/service-role key.
-- RLS is enabled so the table is not exposed through a public client.
alter table public.users enable row level security;
