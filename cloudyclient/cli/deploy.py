'''
``cloudy deploy`` implementation.
'''
import sys
import logging

import requests

from cloudyclient.api import run
from cloudyclient.exceptions import ConfigurationError
from cloudyclient.client import CloudyClient
from cloudyclient.cli.config import CliConfig


def deploy(args):
    '''
    Get or set a deployment's commit.
    '''
    # Check args
    if not args.group and not args.list:
        print 'You must specify at least one group name or --list'
        sys.exit(1)

    # Inhibit API logging 
    api_logger = logging.getLogger('cloudyclient.api.base')
    api_logger.setLevel(logging.WARNING)

    # Load CLI configuration
    try:
        config = CliConfig()
    except ConfigurationError as exc:
        print exc
        sys.exit(1)

    if not args.list:
        push_commits(args, config)
    else:
        list_groups(args, config)


def push_commits(args, config):
    # Get deployment groups definitions from configuration
    groups = {}
    for group_name in args.group:
        deployment_groups = config.get('deployment_groups', {})
        group = deployment_groups.get(group_name)
        if group is None:
            print 'No such deployment group "%s"' % group_name
            sys.exit(1)
        groups[group_name] = group
    
    # Retreive the branches to push
    current_git_branch = get_current_git_branch()
    branches = {}
    branches_to_push = set()
    push_tags = False
    for group_name, group in groups.items():
        branch = group.get('branch')
        if branch is None:
            print '"branch" key missing from deployment group "%s"' % \
                    group_name
            sys.exit(1)
        if branch == '__current__':
            branch = current_git_branch
        branches[group_name] = branch
        if group.get('push', False):
            branches_to_push.add(branch)
        if group.get('push_tags', False):
            push_tags = True

    for branch in branches_to_push:
        src_dst = '{0}:{0}'.format(branch)
        print 'git push origin %s' % src_dst
        run('git', 'push', 'origin', src_dst, no_pipes=True)
        print

    if push_tags:
        print 'git push --tags'
        run('git', 'push', '--tags', no_pipes=True)
        print

    for group_name, group in groups.items():
        # Retrieve the commit to deploy
        branch = branches[group_name]
        commit = run('git', 'rev-parse', branch)
        # Update deployments commits
        poll_urls = group.get('deployments', [])
        if not poll_urls:
            print 'Warning: deployment group "%s" defines no deployments' % \
                    group_name
        for url in poll_urls:
            client = CloudyClient(url, register_node=False)
            try:
                data = client.poll()
            except requests.HTTPError as exc:
                print 'error polling %s: %s' % (url, exc)
                continue
            if data['commit'] != commit:
                client.set_commit(commit)
                print '%s: %s (%s)' % (client.deployment_name, commit, branch)
            else:
                print '%s: already up-to-date' % client.deployment_name


def list_groups(args, config):
    '''
    List available deployment groups.
    '''
    deployment_groups = config.get('deployment_groups', {})
    for name, definition in deployment_groups.items():
        print '%s: branch %s' % (name, definition['branch'])


def get_current_git_branch():
    '''
    Return the current GIT branch.
    '''
    branches = run('git', 'branch')
    current_branch = [b for b in branches.split('\n') if b.startswith('*')][0]
    return current_branch.split()[1]
