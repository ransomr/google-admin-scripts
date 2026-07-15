#!/usr/bin/python
"""Export Gmail contacts (including "Other contacts") to CSV via the People API.

Run it, log in with your PERSONAL account in the browser window, and it writes
contacts_export.csv with name,email,source rows. Uses its own token file
(contacts_token.pickle) so it won't touch the admin token.pickle.
"""

import csv
import os
import pickle
import sys

from google.auth.transport.requests import AuthorizedSession, Request
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_SECRETS = 'client_secrets.json'
TOKEN_FILE = 'contacts_token.pickle'
OUTPUT = 'contacts_export.csv'

SCOPES = ['https://www.googleapis.com/auth/contacts.readonly',
          'https://www.googleapis.com/auth/contacts.other.readonly']


def get_creds():
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
  return creds


def fetch_people(session, url, list_key, extra_params):
  people = []
  page_token = None
  while True:
    params = dict(extra_params, pageSize=1000)
    if page_token:
      params['pageToken'] = page_token
    r = session.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    people.extend(data.get(list_key, []))
    page_token = data.get('nextPageToken')
    if not page_token:
      return people


def rows_from(people, source):
  for p in people:
    names = p.get('names', [])
    name = names[0].get('displayName', '') if names else ''
    for e in p.get('emailAddresses', []):
      if e.get('value'):
        yield name, e['value'].strip(), source


def main(argv):
  session = AuthorizedSession(get_creds())

  contacts = fetch_people(
      session, 'https://people.googleapis.com/v1/people/me/connections',
      'connections', {'personFields': 'names,emailAddresses'})
  other = fetch_people(
      session, 'https://people.googleapis.com/v1/otherContacts',
      'otherContacts', {'readMask': 'names,emailAddresses'})

  seen = set()
  count = 0
  with open(OUTPUT, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['name', 'email', 'source'])
    for name, email, source in list(rows_from(contacts, 'contacts')) + \
                               list(rows_from(other, 'other_contacts')):
      key = email.lower()
      if key in seen:
        continue
      seen.add(key)
      w.writerow([name, email, source])
      count += 1
  print(f'{len(contacts)} contacts, {len(other)} other contacts')
  print(f'wrote {count} unique addresses to {OUTPUT}')


if __name__ == '__main__':
  main(sys.argv)
