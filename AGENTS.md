# google-admin-scripts

Scripts for administering a small community's Google Workspace domain
(likely legacy free G Suite edition — Email Log Search returns no data, so
diagnose delivery via the APIs and Groups UI instead).

The domain and other local specifics live in `config.json` (gitignored;
see `config.example.json`). Never commit real names, email addresses, or
the domain — the repo is public.

## Environment

- Run everything with the repo venv: `.venv/bin/python <script>`
- Plain `requests`-style calls via `google.auth` `AuthorizedSession`; no
  Google client libraries.

## Auth model (multiple tokens, do not mix)

- `client_secrets.json` (gitignored): OAuth desktop client. Lives in a GCP
  project owned by a domain admin account. APIs enabled there: Admin SDK,
  Groups Settings, People.
- `token.pickle`: READ-ONLY group scopes; used by `groups.py`. Keep it
  read-only so listing scripts stay safe.
- `token_admin.pickle`: write scopes (directory user + group, groups
  settings); used by `migrate.py` and `set_group_settings.py`. Log in with
  a super-admin account.
- `contacts_token.pickle`: personal-gmail contacts scopes; used by
  `export_contacts.py` (log in with a personal account).
- Deleting a token file and rerunning a script triggers a fresh browser
  OAuth flow with that script's scopes.

## Scripts

- `groups.py` — lists groups and flattened memberships; output is saved as
  `groupsN.txt` snapshots (gitignored).
- `migrate.py` — migrates a user account to a group-based forwarding
  address (see below). Dry-run by default; `--execute` to act; idempotent.
- `set_group_settings.py` — normalizes spamModerationLevel / isArchived
  across all groups and can add moderators to lists. Dry-run by default.
- `export_contacts.py` — exports the logged-in personal account's contacts
  (including "Other contacts", which the Contacts web UI can't export).

## Domain conventions

- Two kinds of groups:
  - **personal**: 1–3 members, all external addresses — a pass-through
    email address for one person. Should have `spamModerationLevel:
    ALLOW`, `isArchived: false`, anyone-can-post.
  - **list**: everything else. Keep `spamModerationLevel: MODERATE` and
    make sure a manager exists to see held mail.
- Migrated people's old accounts are renamed with a `_user` suffix and
  kept.
- `migration_plan.csv` (gitignored) is the source of truth for the
  account→group migration: action + forward_to per account.
- New personal groups copy their settings from the group named by
  `template_group` in `config.json` (an existing known-good personal
  group).

## API gotchas (hard-won)

- `members.list` does NOT return `delivery_settings`; only per-member
  `members.get` does. `DISABLED` delivery = Google silently stopped
  delivering (usually after bounces) — invisible in the UI member list.
- Renaming a user auto-keeps the old address as a user alias; delete it
  before a group can be created at that address, then retry creation while
  the address release propagates (can take ~a minute).
- A group cannot be OWNER/MANAGER of another group; use user accounts
  (external gmail accounts work — several already own groups here).
- Groups with `allowExternalMembers: false` reject external members with
  HTTP 400.
- `users.list` needs a user scope the read-only token lacks; the domain
  user inventory in scripts here is inferred from group memberships, so
  accounts in no groups are invisible to it.
- A group can contain the entire organization via a special CUSTOMER-type
  member; such groups reach only real accounts, NOT migrated (group-based)
  people. The domain's `everyone@` group worked this way and was deleted
  July 2026 for that reason.
- Gmail ignores dots in addresses: `a.b@gmail.com` == `ab@gmail.com`.

## Background: why the migration

Users whose domain accounts auto-forward to external mail silently lose
group-list messages: Gmail never forwards mail it classifies as spam, and
list traffic (DKIM broken by Groups footer) is disproportionately
spam-flagged. Group-based addresses skip the forward hop entirely, so
everyone confirmed to forward is being migrated to a personal group.
