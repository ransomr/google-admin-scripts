#!/usr/bin/python
"""Migrate domain user accounts to group-based forwarding addresses.

For each row in migration_plan.csv with action 'migrate':
  1. rename the user account to <local>_user@<domain>
  2. delete the freed address and any other aliases from the user
     (a rename auto-keeps the old address as a user alias, which would
     otherwise block step 3)
  3. create a group at the original address, copying its settings from
     TEMPLATE_GROUP (an already-migrated, known-good group)
  4. add the person's external address as the sole member
  5. add the old aliases as group aliases
  6. add the new group to every group the account was a member of
     (pass --remove-user to also remove the renamed account from them)

Usage:
  migrate.py list
  migrate.py migrate <account-email> [<account-email> ...] [--execute] [--remove-user]
  migrate.py migrate --all [--execute] [--remove-user]

Dry run by default: prints what it would do. Nothing changes without
--execute. Steps are idempotent, so rerunning a partially-failed
migration is safe.

Uses its own token file (token_admin.pickle) with write scopes; the
read-only token.pickle used by groups.py is untouched. Requires the
Admin SDK API and the Groups Settings API to be enabled on the project
in client_secrets.json, and login with a super-admin account.
"""

import argparse
import csv
import json
import os
import pickle
import sys
import time

from google.auth.transport.requests import AuthorizedSession, Request
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_SECRETS = 'client_secrets.json'
TOKEN_FILE = 'token_admin.pickle'
PLAN_FILE = 'migration_plan.csv'
with open('config.json') as _f:
  _config = json.load(_f)
DOMAIN = _config['domain']
TEMPLATE_GROUP = _config['template_group']

DIR = 'https://admin.googleapis.com/admin/directory/v1'
GSET = 'https://www.googleapis.com/groups/v1/groups'

SCOPES = ['https://www.googleapis.com/auth/admin.directory.user',
          'https://www.googleapis.com/auth/admin.directory.group',
          'https://www.googleapis.com/auth/apps.groups.settings']

# settings identity/read-only fields that must not be copied to a new group
SETTINGS_SKIP = {'kind', 'etag', 'email', 'name', 'description'}


def get_session():
  creds = None
  if os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, 'rb') as f:
      creds = pickle.load(f)
  if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
      creds.refresh(Request())
    else:
      flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, scopes=SCOPES)
      creds = flow.run_local_server()
    with open(TOKEN_FILE, 'wb') as f:
      pickle.dump(creds, f)
  return AuthorizedSession(creds)


def load_plan():
  with open(PLAN_FILE) as f:
    return list(csv.DictReader(f))


def split_list(value):
  return [v.strip().lower() for v in value.split(';') if v.strip()]


class Migrator:
  def __init__(self, session, execute, remove_user):
    self.s = session
    self.execute = execute
    self.remove_user = remove_user
    self.template_settings = None

  def write(self, desc, method, url, json=None, ok_statuses=(200, 201, 204),
            already_statuses=()):
    """Perform (or in dry-run, print) a mutating API call."""
    if not self.execute:
      print(f'    would: {desc}')
      return True
    r = self.s.request(method, url, json=json)
    if r.status_code in ok_statuses:
      print(f'    done:  {desc}')
      return True
    if r.status_code in already_statuses:
      print(f'    skip:  {desc} (already done: HTTP {r.status_code})')
      return True
    print(f'    FAIL:  {desc} -> HTTP {r.status_code}: {r.text[:300]}')
    return False

  def get_template_settings(self):
    if self.template_settings is None:
      r = self.s.get(f'{GSET}/{TEMPLATE_GROUP}', params={'alt': 'json'})
      r.raise_for_status()
      self.template_settings = {k: v for k, v in r.json().items()
                                if k not in SETTINGS_SKIP}
    return self.template_settings

  def create_group_with_retry(self, email, name, description):
    """The freed address can take a while to release after the alias
    delete; retry group creation until it sticks."""
    if not self.execute:
      print(f'    would: create group {email} ("{name}")')
      return True
    body = {'email': email, 'name': name, 'description': description}
    for attempt in range(12):
      r = self.s.post(f'{DIR}/groups', json=body)
      if r.status_code in (200, 201):
        print(f'    done:  create group {email}')
        return True
      if r.status_code == 409 and 'already exists' in r.text.lower():
        # either the address hasn't been released yet, or the group
        # already exists from a previous run
        g = self.s.get(f'{DIR}/groups/{email}')
        if g.status_code == 200:
          print(f'    skip:  group {email} already exists')
          return True
        print(f'    ...address not released yet, retrying ({attempt + 1}/12)')
        time.sleep(10)
        continue
      print(f'    FAIL:  create group {email} -> HTTP {r.status_code}: {r.text[:300]}')
      return False
    print(f'    FAIL:  create group {email}: address never became available')
    return False

  def migrate(self, row):
    acct = row['account'].lower()
    local = acct.split('@')[0]
    new_email = f'{local}_user@{DOMAIN}'
    aliases = split_list(row['aliases'])
    target = row['forward_to (fill in)'].strip()
    parents = split_list(row['group_memberships'])
    print(f'\n=== {acct} -> group, member {target}, '
          f'account renamed to {new_email} ===')
    if not target or ' ' in target or '@' not in target:
      print(f'    ABORT: forward_to is not a single valid address: {target!r}')
      return False

    # find the user account (under old or, if already renamed, new address)
    r = self.s.get(f'{DIR}/users/{acct}')
    renamed_already = False
    if r.status_code == 404:
      r = self.s.get(f'{DIR}/users/{new_email}')
      if r.status_code == 200:
        renamed_already = True
        print(f'    note: account already renamed to {new_email}')
      else:
        print(f'    ABORT: no user account found at {acct} or {new_email}')
        return False
    elif r.status_code != 200:
      print(f'    ABORT: error fetching user {acct}: HTTP {r.status_code}')
      return False
    user = r.json()
    uid = user['id']
    full_name = user.get('name', {}).get('fullName', local.replace('_', ' ').title())

    # merge the plan's aliases with the account's actual aliases (the plan
    # only knows aliases that appeared as group members somewhere)
    live_aliases = [a.lower() for a in user.get('aliases', [])]
    for a in live_aliases:
      if a not in aliases and a not in (acct, new_email):
        print(f'    note: found extra user alias {a}')
        aliases.append(a)

    # refuse to run if a non-group entity holds the address unexpectedly
    g = self.s.get(f'{DIR}/groups/{acct}')
    if g.status_code == 200 and not renamed_already:
      print(f'    ABORT: a group already exists at {acct} but the user '
            f'account was not renamed - resolve manually')
      return False

    ok = True
    # 1. rename the account
    if renamed_already:
      print(f'    skip:  rename (already {new_email})')
    else:
      ok &= self.write(f'rename user {acct} -> {new_email}', 'PUT',
                       f'{DIR}/users/{uid}', json={'primaryEmail': new_email})

    # 2. strip the freed address + old aliases from the user
    for a in [acct] + aliases:
      ok &= self.write(f'delete user alias {a}', 'DELETE',
                       f'{DIR}/users/{uid}/aliases/{a}',
                       already_statuses=(404,))
    if not ok:
      print('    stopping: account not cleanly renamed/freed')
      return False

    # 3. create the group and copy settings from the template
    if not self.create_group_with_retry(acct, full_name, ''):
      return False
    if self.execute:
      settings = self.get_template_settings()
      ok &= self.write(f'apply template settings from {TEMPLATE_GROUP}', 'PUT',
                       f'{GSET}/{acct}?alt=json', json=settings)
    else:
      print(f'    would: apply template settings from {TEMPLATE_GROUP}')

    # 4. add the external address as the member
    ok &= self.write(f'add member {target}', 'POST',
                     f'{DIR}/groups/{acct}/members',
                     json={'email': target, 'role': 'MEMBER'},
                     already_statuses=(409,))

    # 5. add old aliases to the group
    for a in aliases:
      ok &= self.write(f'add group alias {a}', 'POST',
                       f'{DIR}/groups/{acct}/aliases', json={'alias': a},
                       already_statuses=(409,))

    # 6. subscribe the group everywhere the account was subscribed
    for p in parents:
      ok &= self.write(f'add {acct} to {p}', 'POST',
                       f'{DIR}/groups/{p}/members',
                       json={'email': acct, 'role': 'MEMBER'},
                       already_statuses=(409,))
      if self.remove_user:
        ok &= self.write(f'remove renamed account from {p}', 'DELETE',
                         f'{DIR}/groups/{p}/members/{uid}',
                         already_statuses=(404,))

    if self.execute and ok:
      self.verify(acct, target, aliases)
    return ok

  def verify(self, group_email, target, aliases):
    r = self.s.get(f'{DIR}/groups/{group_email}/members')
    members = [m.get('email', '?') for m in r.json().get('members', [])]
    g = self.s.get(f'{DIR}/groups/{group_email}').json()
    print(f'    verify: members={members} aliases={g.get("aliases", [])}')
    if [m.lower() for m in members] != [target.lower()]:
      print('    verify: WARNING - members are not exactly the external address')
    missing = set(aliases) - {a.lower() for a in g.get('aliases', [])}
    if missing:
      print(f'    verify: WARNING - missing aliases: {sorted(missing)}')


def main(argv):
  p = argparse.ArgumentParser(description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument('command', choices=['list', 'migrate'])
  p.add_argument('accounts', nargs='*', help='account emails to migrate')
  p.add_argument('--all', action='store_true', help='migrate every plan row')
  p.add_argument('--execute', action='store_true',
                 help='actually make changes (default is dry run)')
  p.add_argument('--remove-user', action='store_true',
                 help='also remove the renamed _user account from its groups')
  args = p.parse_args(argv[1:])

  plan = load_plan()
  todo = {r['account'].lower(): r for r in plan if r['action'] == 'migrate'}

  if args.command == 'list':
    for acct, row in sorted(todo.items()):
      print(f"{acct:42s} -> {row['forward_to (fill in)']}")
    return

  if args.all:
    selected = sorted(todo)
  else:
    selected = [a.lower() for a in args.accounts]
    if not selected:
      p.error('give one or more account emails, or --all')
    unknown = [a for a in selected if a not in todo]
    if unknown:
      p.error(f'not in the migration plan (action=migrate): {unknown}')

  session = get_session()
  m = Migrator(session, execute=args.execute, remove_user=args.remove_user)
  if not args.execute:
    print('DRY RUN - re-run with --execute to make changes')
  results = {a: m.migrate(todo[a]) for a in selected}
  failed = [a for a, ok in results.items() if not ok]
  print(f'\n{len(results) - len(failed)}/{len(results)} succeeded')
  if failed:
    print('failed:', ', '.join(failed))
    sys.exit(1)


if __name__ == '__main__':
  main(sys.argv)
