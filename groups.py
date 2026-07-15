#!/usr/bin/python

import enum
import json
import os
import pickle
import sys

with open('config.json') as _f:
  DOMAIN = json.load(_f)['domain']

import google_auth_oauthlib
from google.auth.transport.requests import AuthorizedSession, Request
from google_auth_oauthlib.flow import InstalledAppFlow
from requests_oauthlib import OAuth2Session

# CLIENT_SECRETS, name of a file containing the OAuth 2.0 information for this
# application, including client_id and client_secret, which are found
# on the API Access tab on the Google APIs
# Console <http://code.google.com/apis/console>
CLIENT_SECRETS = 'client_secrets.json'

def get_creds():
  scopes = ['https://www.googleapis.com/auth/admin.directory.group.member.readonly',
            'https://www.googleapis.com/auth/admin.directory.group.readonly']

  creds = None
  if os.path.exists('token.pickle'):
    with open('token.pickle', 'rb') as token:
      creds = pickle.load(token)
  if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
      creds.refresh(Request())
    else:
      flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, scopes=scopes)
      creds = flow.run_local_server()
    # Save the credentials for the next run
    with open('token.pickle', 'wb') as token:
      pickle.dump(creds, token)
  return creds

class Group:
  class GroupType(enum.Enum):
    Unknown = 0
    Group = 1
    Alias = 2
    User = 3
  def __init__(self, group_json):
    self.name = group_json['name']
    self.email = group_json['email']
    self.description = group_json['description']
    self.type = Group.GroupType.Unknown
    self.emails = {self.email}
    if 'aliases' in group_json:
      self.add_aliases(group_json['aliases'])
    self.members = set()

  def add_aliases(self, aliases: list[str]):
    self.emails.update(aliases)

def create_groups(session):
  r = session.get('https://admin.googleapis.com/admin/directory/v1/groups',
                  params={'domain': DOMAIN, 'maxResults': 5000})
  json_groups = r.json()['groups']

  groups = {}
  for g in json_groups:
    if g['name'] == 'everyone':
      continue
    group = Group(g)
    direct_members = int(g['directMembersCount'])
    if direct_members > 1:
      group.type = Group.GroupType.Group
    elif direct_members == 1:
      r = session.get('https://admin.googleapis.com/admin/directory/v1/groups/{group_id}/members'.format(group_id=g['email']))
      json_member = r.json()['members'][0]
      if DOMAIN in json_member['email']:
        group.type = Group.GroupType.Alias
        group.members = {json_member['email']}
      else:
        group.type = Group.GroupType.User
    groups[g['email']] = group
  return groups

def handle_aliases(groups):
  for g in [g for g in groups.values() if g.type == Group.GroupType.Alias]:
    assert(len(g.members) == 1)
    target_email = next(iter(g.members))
    if not target_email in groups:
      # Reference to a user - this is actually a one member group
      g.type = Group.GroupType.Group
      continue

    target = groups[target_email]
    if target.type == Group.GroupType.Alias:
      raise Exception('Alias to Alias not supported')
    elif target.type == Group.GroupType.Group:
      target.add_aliases(g.emails)
    else:
      # Target is a user - this is a one member group
      g.type = Group.GroupType.Group

def list_group_members(session, groups, group):
  if group.members:
    # members already listed
    return
  r = session.get('https://admin.googleapis.com/admin/directory/v1/groups/{group_id}/members'.format(group_id=group.email))
  json_members = r.json()['members']
  for member in json_members:
    member_email = member['email']
    if member_email in groups:
      target_group = groups[member_email]
      if target_group.type == Group.GroupType.Group:
        list_group_members(session, groups, target_group)
        group.members.update(target_group.members)
        continue
    group.members.add(member_email.lower())

def list_members(session, groups):
  for g in [g for g in groups.values() if g.type == Group.GroupType.Group]:
    list_group_members(session, groups, g)

def print_groups(groups):
  for g in [g for g in groups.values() if g.type == Group.GroupType.Group]:
    print(g.name)
    if g.description:
      print(g.description)
    print(sorted(g.emails))
    for member in sorted(g.members):
      print(member)
    print()

def main(argv):
  session = AuthorizedSession(get_creds())
  groups = create_groups(session)
  handle_aliases(groups)
  list_members(session, groups)
  print_groups(groups)

if __name__ == '__main__':
  main(sys.argv)
