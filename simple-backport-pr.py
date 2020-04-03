"""
Simple workflow for backporting Ceph PRs

Usage:
  simple-backport-pr.py backport options <backport-title> <pr_id>...
  simple-backport-pr.py search
  simple-backport-pr.py create-backport-pr [--push] options <backport-title> <pr_id>
  simple-backport-pr.py -h | --help

Options:
  --ignore-pr-not-merged
  --ignore-tracker
  -h --help                Show this screen.
"""
from subprocess import check_call, check_output, CalledProcessError
import os
import sys
from typing import List, Tuple

from github import Github
from github.Repository import Repository
from github.PullRequest import PullRequest
from github.PaginatedList import PaginatedList
from github.Commit import Commit
from github.Milestone import Milestone
import docopt

check_pr_not_merged = 'pr-not-merged'
check_tracker = 'tracker'

def _check(condition, name, description):
    if condition:
        if name in disabled_checks:
            print(f'ignoring check failed: {description} [--ignore-{name}]')
        else:
            print(f'check failed: {description} [--ignore-{name}]')
            sys.exit(3)


def get_branch_name(pr_ids: List[str]) ->  str:
    return 'octopus-backport-' + '-'.join(pr_ids)


def backport_commits(branch_name: str, commits: List[Commit]):

    print(f"git cherry-pick --abort ; git reset --hard HEAD && git checkout octopus && git branch -D {branch_name}")
    
    current_branch = check_output('git symbolic-ref --short HEAD', shell=True).decode().strip()
    assert current_branch == 'octopus', current_branch

    commits_str = ' '.join(c.sha for c in commits)

    check_call("git pull upstream octopus", shell=True)
    check_call(f"git checkout -b {branch_name}", shell=True)
    check_call(f"git cherry-pick -x {commits_str}", shell=True)
    push_backport_branch(branch_name)

def push_backport_branch(branch_name):
    check_call(f"git push --set-upstream origin {branch_name}", shell=True)


def get_pr(ceph, pr_id: str) -> PullRequest:
    pr: PullRequest = ceph.get_pull(int(pr_id))
    _check(not pr.merged,
           check_pr_not_merged,
           f'PR not merged: {pr.html_url}')
    _check('https://tracker.ceph.com/issues/' in pr.body,
           check_tracker,
           f'looks like pr contains a link to the tracker {pr.html_url}')
    return pr

def get_prs(ceph: Repository, pr_ids: List[str]) -> Tuple[List[PullRequest], List[str]]:
    prs = [get_pr(ceph, pr_id) for pr_id in pr_ids]
    prs = sorted(prs, key=lambda pr: pr.merged_at)
    return prs, [str(pr.number) for pr in prs]


def get_pr_commits(pr: PullRequest) -> PaginatedList:
    cs = pr.get_commits()
    for c in cs:
        _check('https://tracker.ceph.com/issues/' in c.commit.message,
               check_tracker,
               f'looks like {c} of {pr} contains a link to the tracker')

    c_ids = list(c.sha for c in cs)
    print(c_ids)
    return cs


def commit_in_current_branch(commit: Commit):
    try:
        in_branches = [
            b.decode().replace('*', '').strip() for b in
            check_output(f'git branch --contains {commit.sha}', shell=True).splitlines()
        ]
    except CalledProcessError:
        print('maybe helps: $ git checkout master && git pull upstream master && git checkout -')
        raise
    print(f'already in branches {in_branches}')
    if 'octopus' in in_branches:
        print(f'{commit} already in branch `octopus`')
        sys.exit(3)

    msg = commit.commit.message
    orig_title = msg.split('\n')[0]
    title=orig_title
    for c in '[]*?':
      title = title.split(c)[0]
    print(f"title='{title}' original title='{orig_title}'")
  
    out = check_output(["git", "log", "--no-merges", "--grep", title, "--oneline"]).strip()
    if out:
        print(f"Commit likely already in current branch:\n  {out.decode()}")
        sys.exit(3)


def create_backport_pull_request(ceph: Repository,
                                 milestone: Milestone,
                                 pr_ids: List[str],
                                 prs: List[PullRequest],
                                 title):
    numberstr = ', '.join(f'#{pr_id}' for pr_id in pr_ids)
    body = f"Backport of {numberstr}"
    assert 'octopus' not in title
    backport_pr: PullRequest = ceph.create_pull(
        title='octopus: ' + title,
        body=body,
        base='octopus',
        head=f'sebastian-philipp:{get_branch_name(pr_ids)}',
    )
    labels = set(sum([get_pr_labels(pr) for pr in prs], []))
    backport_pr.set_labels(*list(labels))
    backport_pr.as_issue().edit(milestone=milestone,)
    print(f'Backport PR creted: {backport_pr.html_url}')


def get_pr_labels(pr: PullRequest):
    labels = [l.name for l in pr.labels]
    return [l for l in 'cephadm orchestrator mgr documentation'.split() if l in labels]


def backport(g: Github, pr_ids: List[str], title:str):
    ceph: Repository = g.get_repo('ceph/ceph')
    octopus_milestone: Milestone = ceph.get_milestone(13)

    prs, pr_ids = get_prs(ceph, pr_ids)

    commits: List[Commit] = []
    for pr in prs:
        this_commits = get_pr_commits(pr)
        for c in this_commits:
            commits.append(c)

    for c in commits:
        commit_in_current_branch(c)

    branch_name = get_branch_name(pr_ids)

    backport_commits(branch_name, commits)
    create_backport_pull_request(ceph,
                                 octopus_milestone,
                                 pr_ids,
                                 prs,
                                 title)

def search_prs(g: Github):
    q = {
        'repo': 'ceph/ceph',
        'label':'cephadm',
        'is':'merged',
        'base':'master',
    }
    #print([pr.number for pr in prs])

def main_create_backport_pr(push: bool,
                            pr_ids: List[str],
                            title: str):
    ceph: Repository = g.get_repo('ceph/ceph')
    octopus_milestone: Milestone = ceph.get_milestone(13)

    prs, pr_ids = get_prs(ceph, pr_ids)

    if push:
        push_backport_branch(get_branch_name(pr_ids))
    create_backport_pull_request(ceph,
                                 octopus_milestone,
                                 pr_ids,
                                 prs,
                                 title)


if __name__ == '__main__':
    args = docopt.docopt(__doc__)

    disabled_checks = set()
    if args['--ignore-pr-not-merged']:
        disabled_checks.add(check_pr_not_merged)
    if args['--ignore-tracker']:
        disabled_checks.add(check_tracker)

    with open(f"{os.environ['HOME']}/.github_token") as f:
        token = f.read().strip()

    g = Github(token)
    if args['search']:
        search_prs(g)

    if args['backport']:
        backport(g,
                 pr_ids=args['<pr_id>'],
                 title=args['<backport-title>'])

    if args['create-backport-pr']:
        main_create_backport_pr(args['--push'],
                                args['<pr_id>'],
                                args['<backport-title>']
                                )

