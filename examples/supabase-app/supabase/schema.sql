-- Maps a Supabase auth user to the account in the MongoDB `users` collection they are.
--
-- OPTIONAL. The one-time-code sign-in path does not use this table at all: it resolves an address
-- through the gateway's /v1/identity/lookup, which reads `users.email` and returns the user id and
-- the role that `users.usertype` already encodes. This table exists for the Supabase *password*
-- sign-in path, where the Supabase account and the Mongo account are two different records that
-- have to be tied together.
--
-- It is deliberately a table this application controls rather than `auth.users.user_metadata`: a
-- signed-in user can write their own user_metadata through Supabase's API, so using it as the
-- source of truth for identity would let any user re-point themselves at another account.
--
-- Run this in the Supabase SQL editor.

create table if not exists public.app_users (
  id           uuid primary key references auth.users (id) on delete cascade,
  app_user_id  text,
  role         text check (role in ('admin', 'vendor', 'customer')),
  display_name text,
  created_at   timestamptz not null default now()
);

comment on column public.app_users.app_user_id is
  'The `users.user_id` in MongoDB this Supabase account is. NULL until an operator links them.';
comment on column public.app_users.role is
  'admin | vendor | customer. Asserted to the chat gateway, which turns it into a forced filter
   on every query. NULL means this account cannot use the assistant yet.';

alter table public.app_users enable row level security;

-- Users may read their own row and nothing else. Note there is no insert/update/delete policy at
-- all: with RLS on, that means no client-side write is possible under any circumstances. The
-- mapping is administrative data, changed by an operator or a service-role process -- a user who
-- can edit their own vendor_id can read another vendor's orders.
create policy "read own mapping"
  on public.app_users for select
  using (auth.uid() = id);

-- Give every new sign-up a row with NO role. Granting access should be a deliberate act, and a
-- NULL role means the assistant refuses rather than guessing -- the default is deny, the same way
-- it is in app/security/roles.py.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.app_users (id, display_name)
  values (new.id, coalesce(new.raw_user_meta_data ->> 'display_name', new.email))
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Then link a test account to an account that exists in your MongoDB `users` collection:
--   update public.app_users
--      set app_user_id = 'USR-00031', role = 'vendor'
--    where id = '<auth user uuid>';
