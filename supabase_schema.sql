-- HJ GROUPS OF FILES - Supabase user database
-- Compatible with both the current Supabase adapter and an older cached build.

create table if not exists public.users (
    id bigint primary key,
    join_date text not null,
    is_banned boolean not null default false,
    ban_duration bigint not null default 0,
    banned_on text not null default '9999-12-31',
    ban_reason text not null default '',
    -- Compatibility field for an older cached database.py build.
    ban_status jsonb
);

alter table public.users add column if not exists join_date text;
alter table public.users add column if not exists is_banned boolean default false;
alter table public.users add column if not exists ban_duration bigint default 0;
alter table public.users add column if not exists banned_on text default '9999-12-31';
alter table public.users add column if not exists ban_reason text default '';
alter table public.users add column if not exists ban_status jsonb;

update public.users
set join_date = coalesce(join_date, current_date::text),
    is_banned = coalesce(is_banned, false),
    ban_duration = coalesce(ban_duration, 0),
    banned_on = coalesce(banned_on, '9999-12-31'),
    ban_reason = coalesce(ban_reason, ''),
    ban_status = coalesce(
        ban_status,
        jsonb_build_object(
            'is_banned', coalesce(is_banned, false),
            'ban_duration', coalesce(ban_duration, 0),
            'banned_on', coalesce(banned_on, '9999-12-31'),
            'ban_reason', coalesce(ban_reason, '')
        )
    );

alter table public.users alter column join_date set not null;
alter table public.users alter column is_banned set not null;
alter table public.users alter column ban_duration set not null;
alter table public.users alter column banned_on set not null;
alter table public.users alter column ban_reason set not null;

create index if not exists users_is_banned_idx on public.users (is_banned);

alter table public.users enable row level security;
