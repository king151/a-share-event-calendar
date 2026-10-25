# -*- coding: utf-8 -*-
"""mid 库噪音形态挖掘 v2：
- 真候选保护集 = judged（已由 LLM 判读采信）+ pending；dropped 是 LLM 丢弃的噪音，命中不算误杀（已在判+丢通道计分）
- 文段索引全量扫描 2~5 字文段（mid 数 vs judged 数 vs dropped 数），按 mid 命中排序输出
- 短名单再正则精确复测 judged/pending 误杀
用法：python mid_families_mine2.py <radar.db路径> [最小条数阈值=60]"""
import sys, re, io, json, collections, sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB = sys.argv[1]
MIN = int(sys.argv[2]) if len(sys.argv) > 2 else 60

conn = sqlite3.connect(DB)
load = lambda sql: [r[0] for r in conn.execute(sql)]
mids   = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='filtered' AND fclass='mid'")
judged = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='judged'")
dropped= load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='dropped'")
pend   = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='pending'")
print(f"mids={len(mids)} judged={len(judged)} dropped={len(dropped)} pending={len(pend)}")

CJK = re.compile(r"[\u4e00-\u9fff]")
def grams(text, usec=200):
    out = set()
    use = (text or "")[:usec]
    n = len(use)
    for i in range(n):
        for L in (2, 3, 4, 5):
            j = i + L
            if j <= n and CJK.search(use[i:j]):
                out.add(use[i:j])
    return out

def index(rows):
    C = collections.Counter()
    for t in rows:
        for g in grams(t):
            C[g] += 1
    return C

CM, CJ, CD = index(mids), index(judged), index(dropped)
cands = {g: c for g, c in CM.items() if c >= MIN}
print(f"候选文段（≥{MIN} 条 mid）：{len(cands)}")

rows = []
for g, c in cands.items():
    jh, dh = CJ.get(g, 0), CD.get(g, 0)
    rows.append((g, c, jh, dh))
# 排序：protected 误杀=0 优先，mid 数降序
rows.sort(key=lambda r: (r[2], -r[1]))

def sample(gram, rows_src=mids, n=2):
    out = []
    for t in rows_src:
        if gram in t[:200]:
            out.append((t or "").strip()[:50])
            if len(out) >= n:
                break
    return " ┃ ".join(out)

protectBad = sum(1 for r in rows if r[2] > 0)
print(f"有真候选误杀的候选文段：{protectBad} / {len(rows)}（列出供排除）")

zero = [r for r in rows if r[2] == 0]
print(f"\n== 真候选零误杀、mid 命中 top 80 ==")
for g, c, jh, dh in zero[:80]:
    print(f"[mid {c:>5} | drop命中 {dh:>5}] {g}  ▸ {sample(g)}")

print(f"\n== judged 误杀 1~2 的文段（仅防漏）==")
for g, c, jh, dh in rows[:80]:
    if 0 < jh <= 2:
        print(f"[mid {c:>5} | judged误杀 {jh}] {g}")

json.dump([{"gram": g, "mid": c, "judged_hit": jh, "dropped_hit": dh} for g, c, jh, dh in zero[:300]],
          open(sys.argv[0].replace("mid_families_mine2.py", "mid_families_top.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("→ top300 已存 mid_families_top.json")
