# -*- coding: utf-8 -*-
import json, random

BASE = 'F:/\u968f\u5fc3'
EV = '\u4e8b\u4ef6\u96f7\u8fbe'
out = BASE + '/' + EV + '/judge_queue/verdicts_need_judge_20261024.json'
src_path = BASE + '/' + EV + '/need_judge.jsonl'

data = json.load(open(out, encoding='utf-8'))
src = []
for line in open(src_path, encoding='utf-8'):
    line = line.strip()
    if line:
        src.append(json.loads(line))

assert len(data) == len(src) == 1083
assert [x['id'] for x in data] == [x['id'] for x in src], 'id order mismatch'
print('JSON valid; entries=%d; id order matches source: True' % len(data))
true_ids = [v['id'] for v in data if v['is_event']]
false_ids = [v['id'] for v in data if not v['is_event']]
print('is_event=true=%d  false=%d' % (len(true_ids), len(false_ids)))
assert len(set(true_ids)) + len(set(false_ids)) == 1083

cat = {}; imp = {}; lvl = {}; st = {}
for v in data:
    if v['is_event']:
        e = v['event']
        cat[e['category']] = cat.get(e['category'], 0) + 1
        imp[e['importance']] = imp.get(e['importance'], 0) + 1
        lvl[e['level']] = lvl.get(e['level'], 0) + 1
        st[e['status']] = st.get(e['status'], 0) + 1
print('category:', cat)
print('importance:', imp)
print('level:', lvl)
print('status:', st)

pending = sum(1 for v in data if v['is_event'] and ('\u5f85\u8c03\u67e5' in v['event'].get('notes','') or '\u5f85\u6838\u5b9e' in v['event'].get('notes','')))
noland = sum(1 for v in data if v['is_event'] and not v['event'].get('land_date'))
nullamt = sum(1 for v in data if v['is_event'] and v['event'].get('amount_yi') is None)
print('events noting amount pending: %d; without land_date: %d; amount_yi null: %d' % (pending, noland, nullamt))

random.seed(42)
sample_false = random.sample(false_ids, 15)
id2 = {o['id']: o for o in src}
print('--- random 15 is_event=false items ---')
for i in sample_false:
    print('%s | %s' % (i, id2[i].get('title', '')[:70]))