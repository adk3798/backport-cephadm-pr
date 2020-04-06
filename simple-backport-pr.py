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
import json
from datetime import datetime
from subprocess import check_call, check_output, CalledProcessError
import os
import sys
from typing import List, Tuple, NamedTuple, Dict, Optional

from dateutil import parser
from github import Github
from github.Repository import Repository
from github.PullRequest import PullRequest
from github.PaginatedList import PaginatedList
from github.Commit import Commit
from github.Milestone import Milestone
import docopt

check_pr_not_merged = 'pr-not-merged'
check_tracker = 'tracker'

class GHCache:
    FNAME = os.path.expanduser('~/.simple-backport-pr.cache.json')

    def __init__(self):
        try:
            with open(self.FNAME) as f:
                self._content: dict = json.load(f)
        except FileNotFoundError:
            self._content = {}

        self.pull_instances: Dict[int, PullRequest] = {}


    @property
    def prs(self) -> Dict[str, dict]:
        if 'prs' not in self._content:
            self._content['prs'] = {}

        return self._content['prs']

    @property
    def commits(self) -> Dict[str, dict]:
        if 'commits' not in self._content:
            self._content['commits'] = {}

        return self._content['commits']

    @property
    def pr_commits(self) -> Dict[str, List[str]]:
        if 'pr_commits' not in self._content:
            self._content['pr_commits'] = {}

        return self._content['pr_commits']


    def save(self):
        try:
            c = json.dumps(self._content)
        except:
            print(self._content)
            raise
        with open(self.FNAME, 'w') as f:
            f.write(c)



gh_cache = GHCache()

class CachedCommit(NamedTuple):
    sha: str
    message: str

    @classmethod
    def from_gh(cls, gh: Commit) -> "CachedCommit":
        r = cls(gh.sha, gh.commit.message)
        r.save()
        return r

    @classmethod
    def from_cache(cls, sha) -> "CachedCommit":
        d = gh_cache.commits[sha].copy()
        return cls(**d)

    def save(self):
        gh_cache.commits[self.sha] = self._asdict().copy()
        gh_cache.save()


class CachedPr(NamedTuple):
    number: int
    commits: int
    title: str
    body: str
    merged: bool
    merged_at: datetime
    html_url: str
    ceph: Repository

    @classmethod
    def from_gh_pr(cls, gh: PullRequest, ceph: Repository) -> "CachedPr":
        r = cls(gh.number,
                gh.commits,
                gh.title,
                gh.body,
                gh.merged,
                gh.merged_at,
                gh.html_url,
                ceph)
        gh_cache.pull_instances[r.number] = gh
        r.save()
        return r


    @classmethod
    def from_cache(cls, number: int, ceph: Repository) -> "CachedPr":
        d = gh_cache.prs[str(number)].copy()
        d['merged_at'] = parser.isoparse(d['merged_at'])
        d['ceph'] = ceph
        if 'html_url' not in d:
            d['html_url'] = ''
        return cls(**d)

    def save(self):
        d = self._asdict().copy()
        del d['ceph']
        d['merged_at'] = self.merged_at.isoformat()
        gh_cache.prs[str(self.number)] = d
        gh_cache.save()

    def get_commits(self) -> List[CachedCommit]:
        if str(self.number) in gh_cache.pr_commits:
            return [
                CachedCommit.from_cache(sha)
                for sha in gh_cache.pr_commits[str(self.number)]
            ]
        ret = [
                CachedCommit.from_gh(c) for c in self.github.get_commits()
            ]
        gh_cache.pr_commits[str(self.number)] = [cc.sha for cc in ret]
        gh_cache.save()
        return ret


    @property
    def github(self) -> PullRequest:
        if self.number in gh_cache.pull_instances:
            return gh_cache.pull_instances[self.number]
        gh_cache.pull_instances[self.number] = self.ceph.get_pull(self.number)
        return gh_cache.pull_instances[self.number]



def _check(condition, name, description):
    if condition:
        if name in disabled_checks:
            print(f'ignoring check failed: {description} [--ignore-{name}]')
        else:
            print(f'check failed: {description} [--ignore-{name}]')
            sys.exit(3)


def get_branch_name(pr_ids: List[str]) ->  str:
    return 'octopus-backport-' + '-'.join(pr_ids)


def backport_commits(branch_name: str, commits: List[str]):

    print(f"git cherry-pick --abort ; git reset --hard HEAD && git checkout octopus && git branch -D {branch_name}")
    
    current_branch = check_output('git symbolic-ref --short HEAD', shell=True).decode().strip()
    assert current_branch == 'octopus', current_branch

    commits_str = ' '.join(c for c in commits)

    check_call("git pull upstream octopus", shell=True)
    check_call(f"git checkout -b {branch_name}", shell=True)
    check_call(f"git cherry-pick -x {commits_str}", shell=True)
    push_backport_branch(branch_name)

def push_backport_branch(branch_name):
    check_call(f"git push --set-upstream origin {branch_name}", shell=True)


def get_pr(ceph: Repository, pr_id: str) -> CachedPr:
    pr = CachedPr.from_cache(int(pr_id), ceph)
    _check(not pr.merged,
           check_pr_not_merged,
           f'PR not merged: {pr.html_url}')
    _check('https://tracker.ceph.com/issues/' in pr.body,
           check_tracker,
           f'looks like pr contains a link to the tracker {pr.html_url}')
    return pr

def get_prs(ceph: Repository, pr_ids: List[str]) -> Tuple[List[CachedPr], List[str]]:
    prs = [get_pr(ceph, pr_id) for pr_id in pr_ids]
    prs = sorted(prs, key=lambda pr: pr.merged_at)
    return prs, [str(pr.number) for pr in prs]


def get_pr_commits(pr: CachedPr) -> List[CachedCommit]:
    cs = pr.get_commits()
    for c in cs:
        _check('https://tracker.ceph.com/issues/' in c.message,
               check_tracker,
               f'looks like {c} of {pr} contains a link to the tracker')

    c_ids = list(c.sha for c in cs)
    print(c_ids)
    return cs


def commit_in_current_branch(commit: CachedCommit):
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

    msg = commit.message
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
                                 prs: List[CachedPr],
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


def get_pr_labels(pr: CachedPr):
    labels = [l.name for l in pr.github.labels]
    return [l for l in 'cephadm orchestrator mgr documentation'.split() if l in labels]


def backport(g: Github, pr_ids: List[str], title:str):
    ceph: Repository = g.get_repo('ceph/ceph')
    octopus_milestone: Milestone = ceph.get_milestone(13)

    prs, pr_ids = get_prs(ceph, pr_ids)

    commits: List[CachedCommit] = []
    for pr in prs:
        this_commits = get_pr_commits(pr)
        for c in this_commits:
            commits.append(c)

    for c in commits:
        commit_in_current_branch(c)

    commit_shas = [c.sha for c in commits]

    branch_name = get_branch_name(pr_ids)

    backport_commits(branch_name, commit_shas)
    sys.exit()
    create_backport_pull_request(ceph,
                                 octopus_milestone,
                                 pr_ids,
                                 prs,
                                 title)

def search_prs(g: Github):
    ceph: Repository = g.get_repo('ceph/ceph')
    q = {
        'repo': 'ceph/ceph',
        'label':'cephadm',
        'is':'merged',
        'base':'master',
    }
    print(f'requests remaining: {g._Github__requester.rate_limiting[0]}')
    issues = g.search_issues('', **q)
    print([issue.number for issue in issues])
    prs = []
    for issue in issues[0:20]:
        try:
            prs.append(CachedPr.from_cache(int(issue.number), ceph))
        except:
            prs.append(CachedPr.from_gh_pr(issue.as_pull_request(), ceph))
    print(f'requests remaining: {g._Github__requester.rate_limiting[0]}')




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
    #g = Github()
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

