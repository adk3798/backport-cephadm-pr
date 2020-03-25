from subprocess import check_call, check_output, CalledProcessError
import os
import sys

from github import Github, Repository, PullRequest, PaginatedList, GitCommit, Commit, Milestone


def backport_commits(branch_name, commits):

    print(f"git cherry-pick --abort ; git reset --hard HEAD && git checkout octopus && git branch -D octopus-{branch_name}")
    
    current_branch = check_output('git symbolic-ref --short HEAD', shell=True).decode().strip()
    assert current_branch == 'octopus', current_branch

    check_call("git pull upstream octopus", shell=True)
    check_call(f"git checkout -b octopus-{branch_name}", shell=True)
    check_call(f"git cherry-pick -x {commits}", shell=True)
    check_call(f"git push --set-upstream origin octopus-{branch_name}", shell=True)


def get_pr(pr_id) -> PullRequest:
    pr: PullRequest = ceph.get_pull(int(pr_id))
    if not pr.merged:
        print('PR not merged: {pr.html_url}')
        sys.exit(3)
    if 'https://tracker.ceph.com/issues/' in pr.body:
        print(f'looks like pr contains a link to the tracker {pr.html_url}')
        sys.exit(3)
    return pr


def get_pr_commits(pr: PullRequest) -> PaginatedList:
    cs = pr.get_commits()
    for c in cs:
        if 'https://tracker.ceph.com/issues/' in c.commit.message:
            print(f'looks like {c} of {pr} contains a link to the tracker')
            sys.exit(3)

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


def create_backport_pull_request(ceph: Repository, milestone: Milestone, pr: PullRequest):
    pr_id = pr.as_issue().number
    quote = '\n'.join('> ' + l for l in pr.body.splitlines())
    body = f"""
Backport of #{pr_id}

Original description:
{quote}
"""
    title = f'octopus: {pr.title}'

    print(title)
    backport_pr: PullRequest = ceph.create_pull(
        title=title,
        body=body,
        base='octopus',
        head=f'sebastian-philipp:octopus-backport-{pr_id}',
    )
    backport_pr.set_labels('cephadm')
    backport_pr.as_issue().edit(milestone=milestone,)
    print(f'Backport PR creted: {backport_pr.html_url}')


if __name__ == '__main__':
    pr_id = sys.argv[1]

    with open(f"{os.environ['HOME']}/.github_token") as f:
        token = f.read().strip()

    g = Github(token)
    ceph: Repository = g.get_repo('ceph/ceph')
    octopus_milestone: Milestone = ceph.get_milestone(13)

    pr = get_pr(pr_id)

    commits = get_pr_commits(pr)
    
    for c in commits:
        commit_in_current_branch(c)
    backport_commits(f'backport-{pr_id}', ' '.join(c.sha for c in commits))
    create_backport_pull_request(ceph, octopus_milestone, pr)

    

