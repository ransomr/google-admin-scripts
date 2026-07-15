#!/usr/bin/python
"""Normalize spam moderation and archiving settings across all groups.

Classifies every group in the domain:
  personal - 1-3 members, all of them external (out-of-domain) addresses;
             these are the pass-through address groups for individuals
  list     - everything else

Then applies (only where the current value differs):
  personal groups: spamModerationLevel = --personal-spam, isArchived = --personal-archived
  list groups:     spamModerationLevel = --list-spam

Dry run by default: prints the classification and every change it would
make. Re-run with --execute to apply. Lists with no owner/manager are
flagged, since MODERATE held-mail notifications have nowhere to go.

Usage:
  set_group_settings.py [--execute]
      [--list-spam MODERATE] [--personal-spam ALLOW]
      [--personal-archived false]
"""

import argparse
import json
import os
import pickle
import sys

from google.auth.transport.requests import AuthorizedSession, Request
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_SECRETS = 'client_secrets.json'
TOKEN_FILE = 'token_admin.pickle'
with open('config.json') as _f:
  _config = json.load(_f)
DOMAIN = _config['domain']
DIR = 'https://admin.googleapis.com/admin/directory/v1'
GSET = 'https://www.googleapis.com/groups/v1/groups'

SCOPES = ['https://www.googleapis.com/auth/admin.directory.user',
          'https://www.googleapis.com/auth/admin.directory.group',
          'https://www.googleapis.com/auth/apps.groups.settings']

SPAM_LEVELS = ['ALLOW', 'MODERATE', 'SILENTLY_MODERATE', 'REJECT']


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


def list_all(session, url, key, **params):
  out, token = [], None
  while True:
    p = dict(params, maxResults=200)
    if token:
      p['pageToken'] = token
    r = session.get(url, params=p)
    r.raise_for_status()
    d = r.json()
    out += d.get(key, [])
    token = d.get('nextPageToken')
    if not token:
      return out


def classify(members):
  """personal = small group of only external member addresses."""
  emails = [m.get('email', '').lower() for m in members]
  if not emails or len(emails) > 3:
    return 'list'
  if any(e.endswith('@' + DOMAIN) for e in emails):
    return 'list'
  if any(m.get('type') != 'USER' for m in members):
    return 'list'
  return 'personal'


def ensure_manager(session, group_email, mod_email, execute):
  """Make mod_email a MANAGER of the group with mail delivery off.
  Returns 1 if a change was made (or would be), else 0."""
  r = session.get(f'{DIR}/groups/{group_email}/members/{mod_email}')
  if r.status_code == 200:
    member = r.json()
    if member.get('role') in ('MANAGER', 'OWNER'):
      return 0
    if not execute:
      print(f'{group_email}  [list]  promote {mod_email} to MANAGER')
      return 1
    pr = session.patch(f'{DIR}/groups/{group_email}/members/{mod_email}',
                       json={'role': 'MANAGER'})
    ok = pr.status_code == 200
    print(f'{group_email}  [list]  promote {mod_email} to MANAGER'
          f'{"" if ok else f" FAILED: HTTP {pr.status_code}: {pr.text[:200]}"}')
    return 1 if ok else 0
  if not execute:
    print(f'{group_email}  [list]  add {mod_email} as MANAGER (no email delivery)')
    return 1
  pr = session.post(f'{DIR}/groups/{group_email}/members',
                    json={'email': mod_email, 'role': 'MANAGER',
                          'delivery_settings': 'NONE'})
  if pr.status_code in (200, 201):
    print(f'{group_email}  [list]  add {mod_email} as MANAGER')
    return 1
  if pr.status_code == 400 and 'external' in pr.text.lower():
    print(f'{group_email}  [list]  skip {mod_email}: group does not allow '
          f'external members')
    return 0
  print(f'{group_email}  [list]  add {mod_email} as MANAGER FAILED: '
        f'HTTP {pr.status_code}: {pr.text[:200]}')
  return 1


def main(argv):
  p = argparse.ArgumentParser(description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument('--execute', action='store_true',
                 help='apply changes (default is dry run)')
  p.add_argument('--list-spam', choices=SPAM_LEVELS, default='MODERATE',
                 help='spamModerationLevel for list groups (default MODERATE)')
  p.add_argument('--personal-spam', choices=SPAM_LEVELS, default='ALLOW',
                 help='spamModerationLevel for personal groups (default ALLOW)')
  p.add_argument('--personal-archived', choices=['true', 'false'], default='false',
                 help='isArchived for personal groups (default false)')
  p.add_argument('--add-moderator', action='append', default=[], metavar='EMAIL',
                 help='ensure this address is a MANAGER (delivery off) on every '
                      'list group, so held-mail notifications reach someone; '
                      'repeatable. Must be a user account, not a group - '
                      'Google does not allow groups as managers.')
  args = p.parse_args(argv[1:])

  session = get_session()
  groups = list_all(session, f'{DIR}/groups', 'groups', domain=DOMAIN)
  print(f'{len(groups)} groups in {DOMAIN}; '
        f'{"APPLYING" if args.execute else "DRY RUN"}\n')

  changed = unchanged = failed = 0
  unmoderated_lists = []
  for g in sorted(groups, key=lambda g: g['email'].lower()):
    email = g['email'].lower()
    members = list_all(session, f'{DIR}/groups/{email}/members', 'members')
    kind = classify(members)

    if kind == 'personal':
      want = {'spamModerationLevel': args.personal_spam,
              'isArchived': args.personal_archived}
    else:
      want = {'spamModerationLevel': args.list_spam}
      for mod in args.add_moderator:
        changed += ensure_manager(session, email, mod, args.execute)
      if (want['spamModerationLevel'] != 'ALLOW' and not args.add_moderator
          and not any(m.get('role') in ('OWNER', 'MANAGER') for m in members)):
        unmoderated_lists.append(email)

    r = session.get(f'{GSET}/{email}', params={'alt': 'json'})
    if r.status_code != 200:
      print(f'!! {email}: cannot read settings (HTTP {r.status_code})')
      failed += 1
      continue
    current = r.json()
    delta = {k: v for k, v in want.items() if current.get(k) != v}
    if not delta:
      unchanged += 1
      continue

    desc = ', '.join(f'{k}: {current.get(k)} -> {v}' for k, v in delta.items())
    if args.execute:
      pr = session.patch(f'{GSET}/{email}?alt=json', json=delta)
      if pr.status_code == 200:
        print(f'{email}  [{kind}]  {desc}')
        changed += 1
      else:
        print(f'!! {email}  [{kind}]  FAILED: HTTP {pr.status_code}: {pr.text[:200]}')
        failed += 1
    else:
      print(f'{email}  [{kind}]  {desc}')
      changed += 1

  verb = 'changed' if args.execute else 'would change'
  print(f'\n{verb}: {changed}, already correct: {unchanged}, failed: {failed}')
  if unmoderated_lists:
    print(f'\nWARNING: {len(unmoderated_lists)} list groups get '
          f'{args.list_spam} but have NO owner/manager to see held mail:')
    for e in unmoderated_lists:
      print(f'  {e}')
  if failed:
    sys.exit(1)


if __name__ == '__main__':
  main(sys.argv)
