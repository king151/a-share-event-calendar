# -*- coding: utf-8 -*-
"""候选规则族评估器 v2（收窄后）：算族级命中 + 并集覆盖。用法：python fam_eval2.py <radar.db>"""
import sys, re, io, sqlite3, collections

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
DB = sys.argv[1]
conn = sqlite3.connect(DB)
load = lambda sql: [r[0] for r in conn.execute(sql)]
mids   = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='filtered' AND fclass='mid'")
judged = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='judged'")
dropped= load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='dropped'")

FAMS = [
    ("海外利率汇率",   r"基准利率|国债收益率|风险溢价|\d个基点|美元指数|美联储|日本央行|欧洲央行|英格兰银行|汇率中间价"),
    ("国际时政外交通稿", r"当地时间|总统|外长|外交部|克宫|白宫|佩斯科夫|泽连斯基|胡塞|哈马斯|以军|以色列国防军|北约|联合国安理会|停火|会晤|访问|峰会|大使"),
    ("军事冲突行动",   r"导弹|袭击|轰炸|空袭|军事行动|武装|加沙|停战|静默状态|摧毁|击落"),
    ("灾害事故",      r"地震|余震|台风|洪水|暴雨|山火|震级|遇难|伤亡"),
    ("大宗商品行情",   r"原油期货|期货主力合约|COMEX|LME|现货黄金|现货白银|金价|大宗商品|克价"),
    ("概率调查统计",   r"的概率为|概率为\d|调查显示|民调|普查数据|抽样调查"),
    ("主权信用评级",   r"惠誉|穆迪|标普全球?评级|评级展望|确认.{0,10}评级|主权评级"),
    ("物价与气象",     r"CPI|PPI|寒潮|降温|降雨|汛期|高温预警"),
    ("致辞贺信慰问",   r"致辞|贺信|唁电"),
    ("统计数据披露",   r"同比增长|同比下降|同比增长?|同比减少|比上年同期|同比增(?:速|幅)"),
    ("数据引用(窄)",   r"数据显示"),
    ("贸易救济(窄)",   r"反倾销(终裁|初裁)|日落复审"),
    ("运输量披露(窄)", r"通行量|吞吐量|发送旅客|发运量"),
]
rows = []
for name, p in FAMS:
    rx = re.compile(p)
    mh = sum(1 for t in mids if rx.search(t))
    jh = [t for t in judged if rx.search(t)]
    dh = sum(1 for t in dropped if rx.search(t))
    print(f"{name:　<10} mid={mh:>6}  judged误杀={len(jh)}  drop={dh}")
    for t in jh[:2]:
        print(f"    ⚠ {t[:60]}")
    rows.append(rx)
# 并集覆盖（互不重复的 mid 文本数）
union = [t for t in mids if any(rx.search(t) for rx in rows)]
dhunion = sum(1 for t in dropped if any(rx.search(t) for rx in rows))
junion = [t for t in judged if any(rx.search(t) for rx in rows)]
print(f"\n并集覆盖 mid = {len(union)} / {len(mids)}　（dropped={dhunion}, judged误杀={len(junion)}）")
for t in junion[:3]:
    print(f"    ⚠ 并集误杀: {t[:60]}")
# 未覆盖 mid 的形态抽样（看还剩什么）
rest = [t for t in mids if not any(rx.search(t) for rx in rows)]
print("\n== 未覆盖 mid 随机样例 12 条 ==")
import random
random.seed(7)
for t in random.sample(rest, 12):
    print("  ▸", (t or "").strip()[:58])
