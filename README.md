# simple-backport-pr

this is a simple script

## usage

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
 cat ~/.simple-backport-pr-pacific.cache.json | jq '.prs[].number' | xargs  python ~/src/simple-backport-ceph/simple-backport-pr.py crunch --ignore-tracker
```