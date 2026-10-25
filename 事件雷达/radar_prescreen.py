# -*- coding: utf-8 -*-
"""
radar_prescreen.py — 大资金流动事件预筛（事件雷达 v3 · 方向调整 2026-10-24 v2.1）

用户决定：去掉个股事件，保留大型公司上市发掘，目标转向大的政策/行业信息，
即"金额大且可估算的事件"。

预筛只做两件事（最终裁决由 LLM 判读层负责）：
  1) 硬信号挑重点：金额>=100亿的资金事件 / 大型上市IPO / 政策法规 / 行业资金动向
  2) 噪音排除：业绩披露、行情流水、会见外交、宏观统计、存量数据披露等普通流

输出：
  prescreen_batch.jsonl    —— 候选批次（供 annotate.py 判读；含 amount/signals 参考字段）
  investigate_queue.jsonl  —— 待调查清单：硬候选但金额缺失/文字无法锁定
                                （金额是"代码找不到、需智能调查"的核心缺口，由 AI 层调查补全）
                                （带 priority 排序：全局词/强金额信号 → 优先调查）

用法：
  python radar_prescreen.py                  # 全量待判，输出全部硬候选 + 待调查清单
  python radar_prescreen.py --date 2026-08-19
  python radar_prescreen.py --tier T_IPO
  python radar_prescreen.py --limit 200 --out 自定义.jsonl
  python radar_prescreen.py --apply-filter      # 把预筛结论落库：噪音(P4/DIGEST)标 filtered 跳过，硬候选保留待判
  python radar_prescreen.py --apply-filter --unfilter   # 反向：把 filtered 恢复为 pending
"""
import sys
import re
import json
import sqlite3
import datetime as dt
from pathlib import Path
from collections import Counter

HERE = Path(__file__).resolve().parent
DB = HERE / "radar.db"

# ---------------- 金额提取 ----------------
NUM = r"(\d{1,3}(?:[，,]\d{3})*|\d+(?:\.\d+)?)"
AMT_RE = re.compile(
    rf"({NUM})\s*(万亿|亿|千万|万|千)(?:\s*(美元|港元|港币|欧元|日元|英镑|卢布))?")
TEXT_AMT = [  # (关键词, 折亿元)
    ("数万亿", 50000.0), ("数千亿", 5000.0), ("数百亿", 500.0), ("数十亿", 50.0),
    ("万亿", 10000.0), ("千亿", 1000.0), ("百亿", 100.0), ("十亿", 10.0),
]
CUR_MULT = {"美元": 7.2, "港元": 0.92, "港币": 0.92, "欧元": 7.8,
            "日元": 0.05, "英镑": 9.2, "卢布": 0.08}
# 数量/计数上下文：数字+单位若紧邻这些词，通常指"数量/消耗量/规模"，不是资金金额
QTY_CTX = re.compile(
    r"消耗量|使用量|用量|数量|总量|产量|产能|吞吐量|运量|发送量|发送次数|下载量|访问量"
    r"|交易量|成交量|持仓量|发行量|人次|次数|万家|万户|万台|万辆|万件|万张"
    r"|万盒|万瓶|万吨|万公里|万人|千瓦时|亿千瓦时"
    r"|市场规模|渗透率|渗透|装机量|出货量|出货|销量|销售额|零售额"
    r"|市场份额|市占率|需求|消费量|采购量")


# 资金动作语境：纯量词(万亿/千亿/百亿/数十亿)只有紧邻明确的资金动作才计为资金金额，
# 避免"市场规模数万亿""装机量将达数万亿""OLED市场预计XX亿"被误标为大额资金。
FUND_ACT_RE = re.compile(
    r"投资|募资|融资|募集|收购|并购|重组|回购|要约|增资|定增|增发|发债|发行|债券|转债"
    r"|贷款|授信|借款|合同|中标|订单|签约|罚没|处罚|罚款|没收|补贴|减免|退税|减税|支出"
    r"|预算|下达|发放|拨付|投入|出资|认购|申购|交易|成交|成交额|流入|流出|置换|配股|分拆|私有化|借壳|担保|质押"
    r"|规模达|将达|高达|预计投资|拟投资|计划投资|拟募资|募资")


def _qty_ctx(text, start, end, span=18):
    """金额匹配位置前后是否落入数量/计数语境（非资金）。"""
    lo = max(0, start - span)
    hi = min(len(text), end + span)
    return bool(QTY_CTX.search(text[lo:hi]))


def extract_amount(text):
    """返回 (amount_yi, raw片段)。取全文最大金额口径，折算成亿元。
    排除"数量/消耗量/人次"等非资金语境，避免把词元量、规模等当金额。"""
    best = 0.0
    raw = ""
    for m in AMT_RE.finditer(text):
        if _qty_ctx(text, m.start(), m.end()):
            continue
        try:
            num = float(m.group(1).replace(",", "").replace("，", ""))
            unit = m.group(3)
            cur = m.group(4) or ""
            mult = {"万亿": 1e4, "亿": 1.0, "千万": 1e-1, "万": 1e-4, "千": 1e-5}[unit]
            v = num * mult * CUR_MULT.get(cur, 1.0)
            if v > best:
                best = v
                raw = m.group(0)
        except Exception:
            continue
    rest = text
    for kw, v in TEXT_AMT:
        if kw in rest:
            pos = rest.find(kw)
            # 纯量词仅在资金动作语境附近才计为资金金额
            if (not _qty_ctx(text, pos, pos + len(kw))
                    and FUND_ACT_RE.search(text[max(0, pos - 20):pos + len(kw) + 20])):
                if v > best:
                    best, raw = v, kw
            rest = rest.replace(kw, "")
    return best, raw


# ---------------- 硬信号（目标方向） ----------------
STRONG_IPO_RE = re.compile(
    r"IPO|递表|招股书|招股|过会|注册生效|新股申购|发行股份|辅导验收|A股上市|H股上市|港股上市|美股IPO")
IPO_NEG_RE = re.compile(
    r"终止上市|退市|终止发行|撤回|暂缓|中止|终止分拆|终止IPO|上市失败|取消上市"
    r"|上市(?:险企|公司|企业|券商|银行|板块|的概念|仅|以来|第|后|前夕|在即|满|不到)")

# 政策法规：机构 + 政策动作（AND 组合）
POLICY_ORG = (
    r"国务院|国常会|中央政治局|两办|中共中央|财政部|央行|人民银行|证监会|发改委|国家税务总局|税务总局"
    r"|医保局|住建部|金融监管总局|银保监会|商务部|工信部|科技部|农业农村部|能源局|外汇局|交易所"
    r"|中证协|基金业协会|国资委|审计署|民政部|人社部|生态环境部|交通运输部|文化和旅游部|市场监管总局"
    r"|最高人民法院|最高人民检察院")
POLICY_ACT = (
    r"发布|印发|出台|修改|修订|明确|规范|实施|施行|试点|征求意见|批复|批准|设立|延续|扩大|调整"
    r"|放宽|收紧|取消|暂停|恢复|启动|推动|支持|部署|安排|宣布|决定|同意|许可|放开|完善|优化|延期")
STRONG_POLICY_RE = re.compile(
    rf"(?:{POLICY_ORG}).{{0,24}}(?:{POLICY_ACT})|"
    rf"(?:{POLICY_ACT}).{{0,24}}(?:{POLICY_ORG})")
# 政策全局级词（金额缺时用于判断是否值得调查）
POLICY_GLOBAL = re.compile(r"全国|全市场|全体|存量|万亿级|千亿级|百亿级|全行业|居民|专项债|特别国债|央票|互换|减税|降费|退税")

# 行业资金动向
STRONG_INDUSTRY_RE = re.compile(
    r"(?:行业|产业|全国|全球|欧盟|美国|日本|韩国).{0,20}(?:基金|投资|规划|产能|价格|补贴|关税|禁令|制裁|标准|政策|采购|招标|并购|重组|整合)"
    r"|(?:设立|成立|发起).{0,20}(?:产业基金|行业基金|母基金|引导基金|联盟|平台).{0,20}(?:亿|百亿|万亿|亿级)"
    r"|产能.{0,10}(?:过剩|去化|扩张|新建)|价格战|涨价潮|扩产潮|补贴退坡|以旧换新|汽车下乡|家电下乡"
    r"|(?:市场|交易中心|交易所).{0,14}(?:正式运行|转入正式运行|开市|启动交易)")

# 资金信号（T_MEGA 判定：金额>=100亿 且 命中其一）
MONEY_SIGNALS = [
    "回购", "收购", "并购", "重组", "要约", "增资", "募资", "定增", "增发", "发行", "发债",
    "债券", "转债", "投资", "注资", "授信", "贷款", "合同", "中标", "订单", "签约", "罚没",
    "处罚", "罚款", "没收", "分红", "派现", "减持", "增持", "信托", "互换", "基金", "补贴",
    "竞买", "出让", "转让", "置换", "配股", "分拆", "私有化", "借壳", "担保", "质押",
]
STRONG_MONEY = ["回购", "收购", "并购", "重组", "要约", "增资", "募资", "定增", "发行", "发债",
                "投资", "中标", "订单", "罚没", "没收", "信托", "互换", "分拆", "私有化", "借壳"]

# ---------------- 噪音排除（无硬信号时才生效） ----------------
NOISE_RE = re.compile(
    r"净利润|归母净利|净利(?!润率)|营业收入|营收|同比增长|环比增长|同比上升|环比上升"
    r"|前三季度|第三季度|第二季度|第一季度|半年度|年度报告|业绩预告|业绩快报|一季报|中报|年报"
    r"|成交额|净流入|净流出|涨幅|跌幅|涨超|跌超|上涨|下跌|收涨|收跌|报收|指数|收盘|开盘"
    r"|涨停|跌停|连板|龙虎榜|主力资金|ETF|市值(?:单日|超|达|报|居)|刷新历史新高|历史新高"
    r"|GDP|生产总值|增加值|CPI|PPI|PMI|社融|M2"
    r"|会见|会晤|致电|祝贺|出访|访问|应约|通话|出席.{0,20}(论坛|会议|峰会)"
    r"|投资者关系|调研记录|股东大会决议|董事会决议|监事会|换届|聘任|解聘|回购注销|限售股上市流通"
    r"|涨幅榜|跌幅榜|收盘价|盘中|午后|早盘|尾盘|涨停板|跌停板")

# 存量统计披露：机构定期披露余额/存量/规模数据（央行/统计局/商务部等）→ 普通流
STATS_DISCLOSURE_RE = re.compile(
    r"(?:二季度末|三季度末|一季度末|四季度末|上半年|前三季度|前8个月|1—\d+月|1-\d+月|截至.{0,8}末)"
    r".{0,12}(?:余额|存量|规模|获贷率|收入|占比|比重|同比|增长|下降|超|达)"
    r"|(?:央行|人民银行|统计局|商务部|税务总局|财政部|发改委|外汇局|中科院|能源局).{0,12}(?:公布|披露|发布|显示|数据显示|称).{0,20}(?:余额|存量|规模|获贷率|收入|占比|同比|增长|下降|超|达)")

# 新闻聚合/栏目类
DIGEST_RE = re.compile(
    r"新闻精选|要闻一览|隔夜全球要闻|新闻联播|大事提醒|综述|早间新闻|晚间新闻|午间新闻"
    r"|早间精选|晚间精选|午间精选|早间要闻|晚间要闻|周末要闻|下周|风口研报|今日关注|盘面解析|午评|收评")
# 人事任免：属普通流，非政策/资金事件（覆盖 STRONG_POLICY 的"任命/任免/当选"动作命中）
APPOINT_RE = re.compile(
    r"任免|任命|人事任免|任职|就任|接任|当选|出任|兼任|履新|就职|辞职|卸任|免去|辞去|不再担任|另有任用"
    r"|任[^，。；]{0,10}(?:书记|副书记|省长|副省长|部长|副部长|主任|副主任|厅长|局长|市长|主席|秘书长|董事|董事长|总经理|委员)")


# ---------------- 预筛落库：噪音标记 filtered（跳过不判读），硬候选保留 pending ----------------
FILTER_OUT_TIERS = {"P4", "DIGEST"}        # 关键词预筛直接排除：无需LLM判读
KEEP_TIERS = {"T_IPO", "T_MEGA", "T_POLICY", "T_INDUSTRY"}  # 硬候选：保留待判读

# ---------------- 高确定性噪音规则族（fclass='hi'，计入"消化率"的排泄通道） ----------------
# 来源：2026-10-25 陈旧误滤 751 条人工逐日裁决（720 条丢弃全是这些族群），抽检误滤率≈1%。
# 纪律：宁窄勿宽——只有"直击 Nan 族=当天 LLM 反复丢弃的唯一形态"才可信；族群只在 P4/DIGEST
# 粗滤时打 hi 标，硬候选（政策/行业/IPO/大额）永远不受它影响。
HIGH_CERT_FAMILIES = [
    ("例行资金操作", r"逆回购(操作|协议)|（RRP）使用规模|公开市场(今日)?净(投放|回笼)|MLF操作|买断式逆回购|国库现金管理|固定利率逆回购|再投资购买|准备金管理"),
    ("国债招标发行", r"记账式(贴现|附息)|凭证式国债|招标面值总额"),
    ("中标利率行情", r"中标(收益率|利率)|发行利率"),
    ("市场资金流向", r"南向资金|北向资金|净流入超?|净买入超?|资金借道|两融余额|主力资金"),
    ("产品规模统计", r"ETF.{0,8}(吸金|净流入|新发|天量)|新成立基金|新发基金|新发规模|公募.{0,4}(分红|规模)|FOF.{0,6}(首募|新发|规模)|分红(突破|超|逾|贡献)|年内.{0,10}(新发|募集|新受理|分红)|再融资.{0,4}(改革|成效|变奏)"),
    ("个股行情波动", r"盘前|盘后|盘中|低开|高开|拉升|涨停|连板|异动|走强|震荡|涨超|跌超|跌幅|收跌|收涨|涨幅收窄|涨幅扩大|涨势|跌势|股价|板块"),
    ("研报评级观点", r"目标价|维持.{0,8}评级|予.{0,10}评级|买入.{0,4}评级|跑赢大市|看多|看空"),
    ("业绩披露", r"第?一?二?三?四季度(营收|净利|业绩)|净利(润)?\d?.{0,12}(同比|环比)|营收\d?.{0,10}(同比|环比)|每股(收益|派现)|归属于?股东净利润|半年报业绩|财报(速览|显示)"),
    ("回购进度公告", r"累计回购|已耗资|回购进度|回购计划规模|回购股份(并|用于)|扩大.{0,4}回购"),
    ("加密持仓动态", r"(增持|买入|清仓|减持)(了?)?(\d|约|近).{0,8}(枚|万枚|美元|亿元).{0,4}(比特币|以太坊|BTC|ETH|黄金)|持仓.{0,8}(突破|升至|降至|缩水)|合约爆仓|储备(减少|增)"),
    ("存量数据披露", r"(余额|存量|覆盖率|参保人数|总资产|存款|受托).{0,8}(为?|达到?|超|突破)"),
    ("要闻综述聚合", r"要闻一览|盘前要闻|投资舆情热点|热点一览|情报平台显示|雾里看花"),
]

_FAM_RE = None
def noise_family(text):
    """高确定性噪音直击检测：返回 (族名, 优先级最高者) 或 None。"""
    global _FAM_RE
    if _FAM_RE is None:
        _FAM_RE = [(n, re.compile(p)) for n, p in HIGH_CERT_FAMILIES]
    for name, rx in _FAM_RE:
        if rx.search(text):
            return name
    return None


def apply_db_filter(conn, rows, unfilter=False):
    """把关键词预筛结论落到数据库：
    · 噪音(P4/DIGEST，如个股行情/业绩/会见外交/新闻聚合) → status='filtered'，跳过不判读
      同时打 fclass 标：hi=高确定性噪音规则直击（日常反复验证的可信排泄） / mid=规则未击中的粗滤
    · 硬候选(T_IPO/T_MEGA/T_POLICY/T_INDUSTRY) → 保持 pending，留给 LLM 判读
    unfilter=True 反向恢复：filtered → pending（可整体重来）。返回受影响条数。"""
    if unfilter:
        cur = conn.execute("UPDATE telegrams SET status='pending', fclass='' WHERE status='filtered'")
        return cur.rowcount
    n = 0
    for tel_id, _date, title, content in rows:
        text = f"{title or ''} {content or ''}"
        amount_yi, _raw = extract_amount(text)
        tier = classify(text, amount_yi)
        if tier in FILTER_OUT_TIERS:
            fam = noise_family(text)
            fcls = "hi" if fam else "mid"
            cur = conn.execute(
                "UPDATE telegrams SET status='filtered', fclass=? WHERE id=? AND status='pending'",
                (fcls, tel_id))
            n += cur.rowcount
    return n


def classify(text, amount_yi):
    """返回 tier（供 LLM 参考，非最终裁决）。
    NOISE 在 T_MEGA 之前拦截个股行情/业绩公告噪音——但 ≥1000 亿元级的全球大额资金
    （如微软 1750 亿美元 capex）强制放行进 T_MEGA，交给 LLM 层裁决（宁可误进判读，不许漏大额）。"""
    if DIGEST_RE.search(text):
        return "DIGEST"
    if APPOINT_RE.search(text):
        return "P4"   # 人事任免：非资金/政策事件，直接排除
    if STRONG_IPO_RE.search(text) and not IPO_NEG_RE.search(text):
        return "T_IPO"
    if STATS_DISCLOSURE_RE.search(text):
        return "P4"   # 存量统计披露（余额/存量数据），优先于政策/行业判定
    if STRONG_POLICY_RE.search(text):
        return "T_POLICY"
    if STRONG_INDUSTRY_RE.search(text):
        return "T_INDUSTRY"
    if amount_yi and amount_yi >= 100 and any(s in text for s in MONEY_SIGNALS):
        if amount_yi >= 1000 or not NOISE_RE.search(text):
            return "T_MEGA"
    return "P4"


def prescreen(rows):
    out, invest = [], []
    noise = 0
    for r in rows:
        tel_id, date, title, content = r
        text = f"{title or ''} {content or ''}"
        amount_yi, raw = extract_amount(text)
        tier = classify(text, amount_yi)
        if tier in ("P4", "DIGEST"):      # 噪音（个股流水/业绩/行情/会见外交/新闻聚合）→ 已过滤跳过，不判读
            noise += 1
            continue
        item = {
            "id": tel_id, "date": date, "title": title or "",
            "content": (content or "")[:300] + ("…[截断]" if len(content or "") > 300 else ""),
            "tier": tier, "amount_yi": round(amount_yi, 2) if amount_yi else None,
            "amount_raw": raw, "signals": [],
        }
        out.append(item)
        if tier in ("T_IPO", "T_POLICY", "T_INDUSTRY") and not amount_yi:
            # 待调查优先级：全局级词/强金额信号 → 优先
            pri = 1 if (POLICY_GLOBAL.search(text) or any(s in text for s in STRONG_MONEY)) else 2
            item["investigate_hint"] = {
                "T_IPO": "查：该主体IPO所处阶段、预计募资/估值量级（招股书/交易所官网/证监会注册信息）",
                "T_POLICY": "查：政策原文条款（机构官网/政府网站），确认涉及资金规模、生效/施行日、覆盖范围",
                "T_INDUSTRY": "查：行业层面资金动向规模（行业报告/官方发布会/权威媒体），确认金额量级与时间节点",
            }[tier]
            item["pri"] = pri
            invest.append(item)
    return out, invest, noise


def main():
    args = sys.argv[1:]
    d = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--"):
            k = args[i][2:]
            v = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else "1"
            d[k] = v
            i += 1 if v == "1" else 2
        else:
            d.setdefault("path", args[i])
            i += 1
    date = d.get("date") or ""
    tier_f = d.get("tier") or ""
    limit = int(d.get("limit") or 0)
    out = d.get("out")
    apply_filter = "--apply-filter" in args
    unfilter = "--unfilter" in args

    conn = sqlite3.connect(str(DB), timeout=30)
    sql = "SELECT id,date,title,content FROM telegrams WHERE status='pending'"
    q = []
    if date:
        sql += " AND date=?"
        q.append(date)
    rows = conn.execute(sql, q).fetchall()
    if apply_filter:
        n = apply_db_filter(conn, rows, unfilter=unfilter)
        if n:
            conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            conn.execute(
                "INSERT INTO meta(k,v) VALUES('last_filter_at',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (dt.datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
        cur = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='pending'").fetchone()[0]
        print(f"预筛落库{'（恢复模式）' if unfilter else ''}：{'恢复为待判' if unfilter else '已过滤跳过'} {n} 条，当前待判(硬候选) {cur} 条")
    conn.close()

    items, invest, noise = prescreen(rows)
    if tier_f:
        items = [x for x in items if x["tier"] == tier_f]
    order = {"T_IPO": 0, "T_MEGA": 1, "T_POLICY": 2, "T_INDUSTRY": 3, "DIGEST": 9}
    items.sort(key=lambda x: (order.get(x["tier"], 9), - (x["amount_yi"] or 0), x["date"]))
    if limit > 0:
        items = items[:limit]

    tiers = Counter(x["tier"] for x in items)
    print(f"预筛完成：待判 {len(rows)} 条，其中噪音(已过滤跳过) {noise} 条，硬候选 {len(items)} 条")
    print(f"分档：T_IPO(大型上市)={tiers.get('T_IPO', 0)} | T_MEGA(>=100亿)={tiers.get('T_MEGA', 0)} "
          f"| T_POLICY(政策)={tiers.get('T_POLICY', 0)} | T_INDUSTRY(行业)={tiers.get('T_INDUSTRY', 0)}")
    if not out:
        out = str(HERE / "prescreen_batch.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for x in items:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(f"已写出 {len(items)} 条候选 → {out}")

    inv_out = d.get("invest") or str(HERE / "investigate_queue.jsonl")
    if not tier_f:
        invest.sort(key=lambda x: (x.get("pri", 2), x["date"]))
        with open(inv_out, "w", encoding="utf-8") as f:
            for x in invest:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        pri1 = sum(1 for x in invest if x.get("pri") == 1)
        print(f"待调查清单 {len(invest)} 条（金额缺失，需AI调查锁定；优先级1={pri1} 条先查）→ {inv_out}")
    if tier_f:
        print(f"（--tier {tier_f} 已过滤；若需全量请不带 --tier）")


if __name__ == "__main__":
    main()