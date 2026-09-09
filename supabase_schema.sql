-- Supabase schema for the upstream TG-FileStore user database.
-- Uses the same logical fields as the original Mongo user document.
-- Safe to run against an existing users table.

create table if not exists public.users (
    id bigint primary key,
    join_date text not null default current_date::text,
    is_banned boolean not null default false,
    ban_duration bigint not null default 0,
    banned_on text not null default '9999-12-31',
    ban_reason text not null default ''
);

alter table public.users add column if not exists join_date text;
alter table public.users add column if not exists is_banned boolean;
alter table public.users add column if not exists ban_duration bigint;
alter table public.users add column if not exists banned_on text;
alter table public.users add column if not exists ban_reason text;

update public.users set join_date = coalesce(join_date, current_date::text);
update public.users set is_banned = coalesce(is_banned, false);
update public.users set ban_duration = coalesce(ban_duration, 0);
update public.users set banned_on = coalesce(banned_on, '9999-12-31');
update public.users set ban_reason = coalesce(ban_reason, '');

alter table public.users alter column join_date set default current_date::text;
alter table public.users alter column is_banned set default false;
alter table public.users alter column ban_duration set default 0;
alter table public.users alter column banned_on set default '9999-12-31';
alter table public.users alter column ban_reason set default '';

alter table public.users alter column join_date set not null;
alter table public.users alter column is_banned set not null;
alter table public.users alter column ban_duration set not null;
alter table public.users alter column banned_on set not null;
alter table public.users alter column ban_reason set not null;

create index if not exists users_is_banned_idx on public.users (is_banned);
alter table public.users enable row level security;
