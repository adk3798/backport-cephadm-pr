# simple-backport-pr

Simple workflow for backporting Ceph PRs

## Installation

```
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## usage

```
Usage:
  simple-backport-pr.py search
  simple-backport-pr.py crunch options [<pr_id>...]
  simple-backport-pr.py backport options <pr_id>...
  simple-backport-pr.py create-backport-pr [--no-push] options <backport-title> <pr_id>...
  simple-backport-pr.py -h | --help

Options:
  --ignore-pr-not-merged
  --ignore-tracker
  -h --help                Show this screen.
```


### search for issues:

```
cd src/ceph ;
git checkout master ; git pull upstream master 
git checkout pacific ; git pull upstream pacific
python ~/src/simple-backport-ceph/simple-backport-pr.py search
```

### print PR List

```
cd src/ceph ;
git checkout master ; git pull upstream master 
git checkout pacific ; git pull upstream pacific
python ~/src/simple-backport-ceph/simple-backport-pr.py crunch --ignore-tracker
```