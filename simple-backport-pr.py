"""
Simple workflow for backporting Ceph PRs

Usage:
  simple-backport-pr.py [--label=label] search
  simple-backport-pr.py [--label=label] crunch options [<pr_id>...]
  simple-backport-pr.py [--label=label] backport options <pr_id>...
  simple-backport-pr.py [--label=label] create-backport-pr [--no-push] options <backport-title> <base-branch-name> <pr_id>...
  simple-backport-pr.py -h | --help

Options:
  --ignore-pr-not-merged
  --ignore-commit-not-merged
  --ignore-order-commit-shas-non-equal
  --ignore-tracker
  --label=label              GH labels
  -h --help                  Show this screen.
"""
import json
from datetime import datetime
from subprocess import check_call, check_output, CalledProcessError
import os
import sys
from typing import List, Tuple, NamedTuple, Dict, Optional, OrderedDict

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
order_commit_shas_non_equal = 'order-commit-shas-non-equal'

def get_current_branch_name() -> str:
    current_branch = check_output('git symbolic-ref --short HEAD', shell=True).decode().strip()
    return current_branch

base_branch_name = get_current_branch_name()

default_labels = 'cephadm orchestrator'.split()
labels: List[str] = []


class GHCache:
    @staticmethod
    def _fname():
        suffixes = ''
        if base_branch_name != "octopus":
            suffixes += "-" + base_branch_name
        if labels != default_labels:
            suffixes += '-' + ','.join(sorted(labels))
        ret = os.path.expanduser(f'~/.simple-backport-pr{suffixes}.cache.json')
        return ret

    def __init__(self):
        try:
            with open(self._fname()) as f:
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

        # takes ages to fill the cache. make sure it's not getting corrupted
        with open(self._fname() + '.tmp', 'w') as f:
            f.write(c)
            f.flush()
            os.fsync(f.fileno())

        os.rename(self._fname() + '.tmp', self._fname())


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
        if base_branch_name in in_branches:
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
    backported: bool

    @classmethod
    def from_gh_pr(cls, gh: PullRequest) -> "CachedPr":
        r = cls(gh.number,
                gh.commits,
                gh.title,
                gh.body,
                gh.merged,
                gh.merged_at,
                gh.html_url,
                False)
        gh_cache.pull_instances[r.number] = gh
        r.save()
        return r


    @classmethod
    def from_cache(cls, number: int) -> "CachedPr":
        d = gh_cache.prs[str(number)].copy()
        d['merged_at'] = parser.isoparse(d['merged_at'])
        if 'html_url' not in d:
            d['html_url'] = ''
        if 'backported' not in d:
            d['backported'] = False
        return cls(**d)

    @classmethod
    def from_any(cls, number: int):
        try:
            return cls.from_cache(number)
        except KeyError:
            return cls.from_gh_pr(ceph_repo().get_pull(number))

    def save(self):
        d = self._asdict().copy()

        if str(self.number) in gh_cache.prs:
            d['backported'] = d['backported'] or self.from_cache(self.number).backported

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

    def get_backported(self) -> bool:
        if self.backported:
            return True
        b = all(c.backported for c in self.get_commits())
        if b:
            copy = self._replace(backported=True)
            copy.save()
        return b


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
        return [l for l in 'cephadm orchestrator rook mgr documentation'.split() if l in labels]

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
    return f'{base_branch_name}-backport-' + ('-'.join([str(pr.number) for pr in prs]))[:60]


def backport_commits(branch_name: str, commits: List[str]):

    print(f"git cherry-pick --abort ; git reset --hard HEAD && git checkout {base_branch_name} && git branch -D {branch_name}")

    commits_str = ' '.join(c for c in commits)

    check_call(f"git pull upstream {base_branch_name}", shell=True)
    check_call(f"git checkout -b {branch_name}", shell=True)
    check_call(f"git cherry-pick -x {commits_str}", shell=True)

def order_commit_shas(commit_shas: List[str]):
    out = check_output(f"git rev-list --topo-order {' '.join(commit_shas)}", shell=True)
    commit_shas_set = set(commit_shas)
    lines = out.decode().splitlines()
    ret = [
        l.strip()
        for l in lines
        if l.strip() in commit_shas_set
    ]
    _check(len(ret) != len(commit_shas_set),
           order_commit_shas_non_equal,
           f'order_commit_shas: dropping commits: {set(commit_shas) - set(ret)}, adding commits: {set(ret) - commit_shas_set}')

    ret.reverse()
    return ret

def push_backport_branch(branch_name):
    check_call(f"git push --set-upstream origin {branch_name}", shell=True)


def get_prs(pr_ids: List[str]) -> List[CachedPr]:
    earliest_pr = datetime(2020, 3, 15)

    prs = [CachedPr.from_any(int(pr_id)) for pr_id in pr_ids ]
    prs = [pr for pr in prs if pr.merged_at > earliest_pr]

    for pr in prs:
        pr.validate()
    prs = sorted(prs, key=lambda pr: pr.merged_at)

    return prs


def create_backport_pull_request(milestone: Milestone,
                                 prs: List[CachedPr],
                                 title):
    numberstr = ', '.join(f'#{pr.number}' for pr in prs)
    body = f"Backport of {numberstr}"
    assert base_branch_name not in title
    backport_pr: PullRequest = ceph_repo().create_pull(
        title=f'{base_branch_name}: ' + title,
        body=body,
        base=base_branch_name,
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
        _check(c.backported, commit_not_merged, f"Commit {c.sha} already in current branch")

    commit_shas = [c.sha for c in commits]

    commit_shas = order_commit_shas(commit_shas)

    branch_name = get_branch_name(prs)

    backport_commits(branch_name, commit_shas)

    print('Maybe you now want to run')
    print(f'  {sys.executable} {sys.argv[0]} create-backport-pr <backport-title> {" ".join(pr_ids)}')


def search_prs_label(g: Github, label: str) -> List[int]:
    q = {
        'repo': 'ceph/ceph',
        'label': label,
        'is': 'merged',
        'base': 'master',
        'created': '>2020-10-19'
    }
    issues = g.search_issues('', sort='updated', **q)
    ids = [issue.number for issue in issues[0:80]]
    print(f'found for label {label}: {ids}')
    return ids


def search_prs(g: Github):
    ids = set(sum([search_prs_label(g, l) for l in labels], []))

    prs = [CachedPr.from_any(id) for id in ids]

    print(f'found {len(prs)} issues')

    print(f'requests remaining: {g._Github__requester.rate_limiting[0]}')




def main_create_backport_pr(push: bool,
                            pr_ids: List[str],
                            title: str):

    prs = get_prs(pr_ids)

    if push:
        push_backport_branch(get_branch_name(prs))
    github_milestone: Milestone = ceph_repo().get_milestone(13)
    create_backport_pull_request(github_milestone,
                                 prs,
                                 title)

def crunch(pr_ids: List[str]):
    global _check_silent
    _check_silent = True

    if not pr_ids:
        pr_ids = list(gh_cache.prs.keys())

    prs = get_prs(pr_ids)
    max_n = max(len(str(pr.number)) for pr in prs)
    max_t = max(len(str(pr.title)) for pr in prs)
    max_at = max(len(str(pr.merged_at.isoformat())) for pr in prs)
    f = '{n:<' + str(max_n) + '}  {t:<' + str(max_t) + '} {b:<10} {at:<' + str(max_at) + '}'
    print(f.format(n='NUM', t='TITLE', b='BACKPORTED', at='MERGED AT'))
    for pr in prs:
        print(f.format(n=pr.number, t=pr.title, b=str(pr.get_backported()), at=pr.merged_at.isoformat()))


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
    if args['--ignore-commit-not-merged']:
        disabled_checks.add(commit_not_merged)
    if args['--ignore-tracker']:
        disabled_checks.add(check_tracker)
    if args['--ignore-order-commit-shas-non-equal']:
        disabled_checks.add(order_commit_shas_non_equal)
    if args['--label']:
        labels = args['--label'].split(',')
    else:
        labels = default_labels
    assert labels, 'labels cannot be empty'

    gh_cache = GHCache()

    with open(f"{os.environ['HOME']}/.github_token") as f:
        token = f.read().strip()

    g = Github(token)
    #g = Github(token + 'xxx')
    #g = Github()

    _ceph_repo = None
    if args['search']:
        assert base_branch_name in 'octopus pacific'.split()

        search_prs(g)

    if args['backport']:
        assert base_branch_name in 'octopus pacific'.split()

        backport(pr_ids=args['<pr_id>'])

    if args['create-backport-pr']:
        base_branch_name = args['<base-branch-name>']
        assert base_branch_name in 'octopus pacific'.split(), f'base-branch-name must be octopus or pacific'
        main_create_backport_pr(not args['--no-push'],
                                args['<pr_id>'],
                                args['<backport-title>']
                                )
    if args['crunch']:
        assert base_branch_name in 'octopus pacific'.split()

        crunch(args['<pr_id>'])

