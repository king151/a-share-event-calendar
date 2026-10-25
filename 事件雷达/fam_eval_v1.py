# -*- coding: utf-8 -*-
"""候选规则族评估器：对拟加进 HIGH_CERT_FAMILIES 的规则族，量化
  mid 覆盖（转化潜力）/ judged 误杀（必须 0）/ dropped 命中（无害，已在分数里）/ pending 误杀
并给样例，供眼检取舍。用法：python fam_eval.py <radar.db>"""
import sys, re, io, json, sqlite3, collections

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
DB = sys.argv[1]
conn = sqlite3.connect(DB)
load = lambda sql: [r[0] for r in conn.execute(sql)]
mids   = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='filtered' AND fclass='mid'")
judged = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='judged'")
dropped= load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='dropped'")
pend   = load("SELECT title||' '||IFNULL(content,'') FROM telegrams WHERE status='pending'")

FAMS = [
    ("统计同比披露",   r"同比增长|同比下降|同比增长?|同比下降?|同比减少|同比增长?|比上年同期|同比增长\d|同比增(?:速|幅)|同比增长|同比下降"),
    ("数据引用转述",   r"数据显示|据报道|据称|报道称|消息(称|面)|援引"),
    ("当地时间通稿",   r"当地时间\d+日"),
    ("海外利率汇率",   r"基准利率|国债收益率|风险溢价|\d个基点|美元指数|美联储|日本央行|欧洲央行|英格兰银行|外汇存款|汇率中间价"),
    ("国际时政外交通稿", r"当地时间|总统|外长|外交部|克宫|白宫|佩斯科夫|泽连斯基|胡塞|哈马斯|以军|以色列国防军|北约|联合国安理会|停火|会晤|访问|峰会|大使"),
    ("军事冲突行动",   r"导弹|袭击|轰炸|空袭|军事行动|武装|加沙|停战|静默状态|摧毁|击落"),
    ("灾害事故",      r"地震|余震|台风|洪水|暴雨|山火|震级|遇难|伤亡"),
    ("大宗商品行情",   r"原油期货|期货主力合约|COMEX|LME|现货黄金|现货白银|金价|钨价|碳酸锂指数|大宗商品"),
    ("贸易救济反倾销", r"反倾销(终裁|初裁|调查)|反补贴(调查)?|贸易救济|保障措施调查|作出反倾销"),
    ("主权信用评级",   r"惠誉|穆迪|标普全球?评级|评级展望|确认.{0,10}评级|主权评级"),
    ("概率与统计调查", r"的概率为|概率为\d|调查显示|调查结果|民调|抽样调查|普查数据"),
    ("公司例行商务合作", r"签署.{0,8}(备忘录|合作意向)?|谅解备忘录|达成合作"),
    ("基础设施运输通报", r"通行量|发运量|吞吐量|发送旅客|开行|\d+(万吨|万标箱|万吨级)"),
    ("消费者物价气象", r"CPI|PPI|物价|气候|气温|寒潮|降温|降雨"),
    ("题词致辞贺信",   r"致辞|贺信|题词|致电慰问|唁电"),
]

rows = []
for name, p in FAMS:
    rx = re.compile(p)
    mh = sum(1 for t in mids if rx.search(t))
    jh = [t for t in judged if rx.search(t)]
    dh = sum(1 for t in dropped if rx.search(t))
    ph = [t for t in pend if rx.search(t)]
    samp_mid = " ┃ ".join((next(t for t in mids if rx.search(t)) or "")[:44] for _ in [0])
    rows.append((name, mh, len(jh), dh, len(ph), jh[:2], samp_mid))
    print(f"{name:　<8} mid命中={mh:>6}  judged误杀={len(jh)}  pending误杀={len(ph)}  drop命中={dh}")
    print(f"    样例: {samp_mid}")
    for t in jh[:2]:
        print(f"    ⚠ 误杀真候选: {t[:60]}")
print(f"\nmids 总量 = {len(mids)}；judged={len(judged)} dropped={len(dropped)} pending={len(pend)}")
