# -*- coding: utf-8 -*-
import json

BASE = 'F:/\u968f\u5fc3'
EV = '\u4e8b\u4ef6\u96f7\u8fbe'
out = BASE + '/' + EV + '/judge_queue/verdicts_need_judge_20261024.json'
src_path = BASE + '/' + EV + '/need_judge.jsonl'

data = json.load(open(out, encoding='utf-8'))
src = {}
for line in open(src_path, encoding='utf-8'):
    line = line.strip()
    if line:
        o = json.loads(line)
        src[o['id']] = o

# 1) normalize level to N-star format
changed = 0
for v in data:
    if v['is_event']:
        e = v['event']
        lv = e.get('level')
        if isinstance(lv, int) or (isinstance(lv, str) and lv.isdigit()):
            e['level'] = str(int(lv)) + '\u2605'
            changed += 1
print('level normalized:', changed)

# 2) unify dedup keys: strip trailing letter suffix so same-day same-event telegrams share one key
key_stripped = 0
for v in data:
    if v['is_event']:
        k = v['event'].get('key', '')
        if k and k[-1:].isalpha():
            v['event']['key'] = k[:-1]
            key_stripped += 1
print('keys stripped:', key_stripped)

# 3) schema recheck
req = ['key', 'name', 'category', 'tags', 'importance', 'level', 'scope', 'signal_date', 'status', 'amount_yi', 'amount', 'sector', 'notes']
opt = {'land_date', 'amount_yi'}
cats = {'\u653f\u7b56\u6cd5\u89c4', '\u884c\u4e1a\u4e8b\u4ef6', '\u516c\u53f8\u5927\u4e8b\u4ef6', '\u76d1\u7ba1\u5904\u7f5a'}
bad = 0
for v in data:
    if not v['is_event']:
        continue
    e = v['event']
    for k in req:
        if k in opt:
            continue
        if k not in e or e[k] is None:
            bad += 1
            print('missing', k, v['id'])
            break
    if not isinstance(e.get('tags'), list):
        bad += 1
        print('tags', v['id'])
    if e.get('scope') != 'watch':
        bad += 1
    if e.get('category') not in cats:
        bad += 1
        print('cat', v['id'], e.get('category'))
    if e.get('importance') not in {'\u9ad8', '\u4e2d', '\u4f4e'}:
        bad += 1
        print('imp', v['id'], e.get('importance'))
    lv = e.get('level', '')
    if not (isinstance(lv, str) and len(lv) == 2 and lv[1] == '\u2605'):
        bad += 1
        print('levelfmt', v['id'], repr(lv))
print('schema problems:', bad)

# duplicate keys after unification
keys = [v['event']['key'] for v in data if v['is_event']]
dup_keys = sorted([k for k in set(keys) if keys.count(k) > 1])
print('duplicate keys (same event multi-telegram):', dup_keys)

# level distribution
lvl = {}
for v in data:
    if v['is_event']:
        lvl[v['event'].get('level')] = lvl.get(v['event'].get('level'), 0) + 1
print('level dist:', lvl)

# amount-null events with notes
print('--- amount_yi null events ---')
for v in data:
    if v['is_event'] and v['event'].get('amount_yi') is None:
        print('%s | %s | notes=%s' % (v['id'], v['event'].get('name', ''), v['event'].get('notes', '')))

# samples: source line vs verdict
sample = [3847, 6848, 2249, 24177, 36550, 32783, 23098, 20253, 12104, 14686, 15226, 17830, 21109, 23094, 24552, 39703, 28860]
print('--- samples (source vs verdict) ---')
for sid in sample:
    if sid in src:
        o = src[sid]
        print('SRC %s: title=%s | amount_yi=%s | amount_raw=%s' % (sid, o.get('title', '')[:60], o.get('amount_yi'), str(o.get('amount_raw', ''))[:60]))
    for v in data:
        if v['id'] == sid and v['is_event']:
            e = v['event']
            print('  VERDICT: name=%s | amount=%s | amount_yi=%s | level=%s | cat=%s | land=%s | status=%s | key=%s' % (e.get('name'), e.get('amount'), e.get('amount_yi'), e.get('level'), e.get('category'), e.get('land_date'), e.get('status'), e.get('key')))

with open(out, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
check = json.load(open(out, encoding='utf-8'))
print('rewritten entries:', len(check))
print('final true count:', sum(1 for v in check if v['is_event']))