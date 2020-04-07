"""
Simple workflow for backporting Ceph PRs

Usage:
  simple-backport-pr.py search
  simple-backport-pr.py crunch options <pr_id>...
  simple-backport-pr.py backport options <pr_id>...
  simple-backport-pr.py create-backport-pr [--no-push] options <backport-title> <pr_id>...
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
commit_not_merged = 'commit-not-merged'

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
    backported: bool

    @classmethod
    def from_gh(cls, gh: Commit) -> "CachedCommit":
        tmp = cls(gh.sha, gh.commit.message, False)

        r = cls(gh.sha, gh.commit.message, tmp._in_current_branch())
        r.save()
        return r

    @classmethod
    def from_cache(cls, sha) -> "CachedCommit":
        d = gh_cache.commits[sha].copy()
        if 'backported' not in d or not d['backported']:
            d['backported'] = False
            tmp = cls(**d)
            d['backported'] = tmp._in_current_branch()
        return cls(**d)

    def save(self):
        gh_cache.commits[self.sha] = self._asdict().copy()
        gh_cache.save()

    def _in_current_branch(self):
        try:
            in_branches = [
                b.decode().replace('*', '').strip() for b in
                check_output(f'git branch --contains {self.sha}', shell=True).splitlines()
            ]
        except CalledProcessError:
            print(
                'maybe helps: $ git checkout master && git pull upstream master && git checkout -')
            raise
        #print(f'already in branches {in_branches}')
        if 'octopus' in in_branches:
            return True

        msg = self.message
        orig_title = msg.split('\n')[0]
        title = orig_title
        for c in '[]*?':
            title = title.split(c)[0]

        out = check_output(["git", "log", "--no-merges", "--grep", self.sha, "--oneline"]).strip()
        if out:
            if not _check_silent:
                print(f"Commit likely already in current branch:\n  {out.decode()}")
            return True

        out = check_output(["git", "log", "--no-merges", "--grep", title, "--oneline"]).strip()
        if out:
            if not _check_silent:
                print(f"Commit likely already in current branch:\n  {out.decode()}")
            return True
        return False

    def validate(self):
        _check('https://tracker.ceph.com/issues/' in self.message,
               check_tracker,
               f'looks like {self} contains a link to the tracker')


class CachedPr(NamedTuple):
    number: int
    commits: int
    title: str
    body: str
    merged: bool
    merged_at: datetime
    html_url: str

    @classmethod
    def from_gh_pr(cls, gh: PullRequest) -> "CachedPr":
        r = cls(gh.number,
                gh.commits,
                gh.title,
                gh.body,
                gh.merged,
                gh.merged_at,
                gh.html_url)
        gh_cache.pull_instances[r.number] = gh
        r.save()
        return r


    @classmethod
    def from_cache(cls, number: int) -> "CachedPr":
        d = gh_cache.prs[str(number)].copy()
        d['merged_at'] = parser.isoparse(d['merged_at'])
        if 'html_url' not in d:
            d['html_url'] = ''
        return cls(**d)

    @classmethod
    def from_any(cls, number: int):
        try:
            return cls.from_cache(number)
        except KeyError:
            return cls.from_gh_pr(ceph_repo().get_pull(number))

    def save(self):
        d = self._asdict().copy()
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

        for c in ret:
            c.validate()

        return ret

    def backported(self):
        return all(c.backported for c in self.get_commits())


    @property
    def github(self) -> PullRequest:
        if self.number in gh_cache.pull_instances:
            return gh_cache.pull_instances[self.number]
        gh_cache.pull_instances[self.number] = ceph_repo().get_pull(self.number)
        return gh_cache.pull_instances[self.number]

    def validate(self):
        _check(not self.merged,
               check_pr_not_merged,
               f'PR not merged: {self.html_url}')
        if self.body:
            _check('https://tracker.ceph.com/issues/' in self.body,
                   check_tracker,
                   f'looks like pr contains a link to the tracker {self.html_url}')

    def get_labels(self):
        labels = [l.name for l in self.github.labels]
        return [l for l in 'cephadm orchestrator mgr documentation'.split() if l in labels]

_check_silent = False
def _check(condition, name, description):
    if condition:
        if name in disabled_checks:
            print(f'ignoring check failed: {description} [--ignore-{name}]')
        else:
            if not _check_silent:
                print(f'check failed: {description} [--ignore-{name}]')
            sys.exit(3)


def get_branch_name(prs: List[CachedPr]) ->  str:
    return 'octopus-backport-' + '-'.join([str(pr.number) for pr in prs])


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


def get_prs(pr_ids: List[str]) -> List[CachedPr]:
    prs = [CachedPr.from_any(int(pr_id)) for pr_id in pr_ids]
    for pr in prs:
        pr.validate()
    prs = sorted(prs, key=lambda pr: pr.merged_at)
    return prs


def create_backport_pull_request(milestone: Milestone,
                                 prs: List[CachedPr],
                                 title):
    numberstr = ', '.join(f'#{pr.number}' for pr in prs)
    body = f"Backport of {numberstr}"
    assert 'octopus' not in title
    backport_pr: PullRequest = ceph_repo().create_pull(
        title='octopus: ' + title,
        body=body,
        base='octopus',
        head=f'sebastian-philipp:{get_branch_name(prs)}',
    )
    labels = set(sum([pr.get_labels() for pr in prs], []))
    backport_pr.set_labels(*list(labels))
    backport_pr.as_issue().edit(milestone=milestone,)
    print(f'Backport PR creted: {backport_pr.html_url}')


def backport(pr_ids: List[str]):

    prs = get_prs(pr_ids)

    commits: List[CachedCommit] = []
    for pr in prs:
        this_commits = pr.get_commits()
        for c in this_commits:
            commits.append(c)

    for c in commits:
        _check(c.backported, commit_not_merged, "Commit already in current branch")

    commit_shas = [c.sha for c in commits]

    branch_name = get_branch_name(prs)

    backport_commits(branch_name, commit_shas)

    print('Maybe you now want to run')
    print(f'  {sys.executable} {sys.argv[0]} create-backport-pr <backport-title> {" ".join(pr_ids)}')

def search_prs(g: Github):
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
    for issue in issues[0:60]:
        prs.append(CachedPr.from_any(int(issue.number)))
    print(f'requests remaining: {g._Github__requester.rate_limiting[0]}')




def main_create_backport_pr(push: bool,
                            pr_ids: List[str],
                            title: str):

    prs = get_prs(pr_ids)

    if push:
        push_backport_branch(get_branch_name(prs))
    octopus_milestone: Milestone = ceph_repo().get_milestone(13)
    create_backport_pull_request(octopus_milestone,
                                 prs,
                                 title)

def crunch(pr_ids):
    global _check_silent
    _check_silent = True
    prs = get_prs(pr_ids)
    max_n = max(len(str(pr.number)) for pr in prs)
    max_t = max(len(str(pr.title)) for pr in prs)
    max_at = max(len(str(pr.merged_at.isoformat())) for pr in prs)
    f = '{n:<' + str(max_n) + '}  {t:<' + str(max_t) + '} {b:<10} {at:<' + str(max_at) + '}'
    print(f.format(n='NUM', t='TITLE', b='BACKPORTED', at='MERGED AT'))
    for pr in prs:
        print(f.format(n=pr.number, t=pr.title, b=str(pr.backported()), at=pr.merged_at.isoformat()))


def ceph_repo() -> Repository:
    global _ceph_repo
    if _ceph_repo is None:
        _ceph_repo = g.get_repo('ceph/ceph')
    return _ceph_repo


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
    #g = Github(token + 'xxx')
    #g = Github()

    _ceph_repo = None
    if args['search']:
        search_prs(g)

    if args['backport']:
        backport(pr_ids=args['<pr_id>'])

    if args['create-backport-pr']:
        main_create_backport_pr(not args['--no-push'],
                                args['<pr_id>'],
                                args['<backport-title>']
                                )
    if args['crunch']:
        crunch(args['<pr_id>'])

