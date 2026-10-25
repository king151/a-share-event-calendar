# -*- coding: utf-8 -*-
"""
AI 精选节点：读取雷达原始情报 latest_scan.json → AI 判读规则输出 AI精选.js
流程（即"AI处理层"的三步走，也可由 opencode 会话人工智能判读生成）：
  第一层 发现（信息源头=财联社）：电报快讯+历史存档，先记重要性（不死抠金额）
  第二层 金额锁定（外部信息源）：候选缺金额时，向外部一手源做定向查询锁定量级
         —— 公司类电报 → 巨潮/东财当日公告；政策类电报 → 官网原文条款
         锁定结果按天缓存（ai_lock.json），同一天不重复外查，对源站友好
  第三层 AI评估兜底：外部源也锁不出的，AI给估算+口径+置信度（∈低中高三档）
         → 有了量级，级别自然可分（★ <10亿 / ★★ 10~100亿 / ★★★ 100~1000亿 / ★★★★ >1000亿）
输出：AI精选.js （注：当前大事件日历页未引用此文件；网页数据以 radar_server 的 data.json
      为权威，本文件供 AI 精选通道复用。历史电报候选由 _cls_candidates() 规则推导生成，
      不写死任何日期/金额/来源。）
"""
import hashlib
import json
import re
import sqlite3
import datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
HREQILI = 200.0   # AI金额线：200亿（公司候选·解禁只留重量级；调低即放宽）

# ---- 第二层·金额锁定（外部源） ----
LOCK_FILE = HERE / "ai_lock.json"
MONEY_WORDS = ["重组", "要约", "募资", "定增", "发行", "回购", "分红", "解禁",
               "中标", "合同", "增资", "股权激励", "预披", "业绩预告", "处罚"]

# —— 用户口径：ST/*ST 股票不关心 —— 与雷达引擎同口径（见 radar_server.ST_RE）
ST_RE = re.compile(r"(^|[^A-Za-z0-9])[*＊]?ST(?=[\u4e00-\u9fa5A-Za-z0-9])", re.I)

def _load_lock():
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_lock(d):
    try:
        LOCK_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass

def _lock_amounts(cands):
    """外部信息源锁定金额：对"待评估"的电报候选做一次定向外查（同日缓存，不重复抓）"""
    lock = _load_lock()
    today = dt.date.today().isoformat()
    notes_df = None
    fetched = {"n": 0}
    for c in cands:
        if "（待AI评估）" not in (c.get("amount") or ""):
            continue
        rec = lock.get(c["id"])
        if rec and rec.get("date") == today:      # 当天已锁过 → 直接复用，不外查
            c["amount"] = rec.get("amount")
            c["notes"] = rec.get("notes") or c["notes"]
            continue
        m = re.match(r"【电报】([^：:，,。]{2,12})[:：]", c.get("name") or "")
        name = m.group(1) if m else None
        note = "🤖 外部金额锁定：该电报无可提取公司主体（保持AI待评估）。"
        if name:
            try:
                if notes_df is None:               # 整个处理批次只取一次全市场公告
                    import akshare as ak
                    notes_df = ak.stock_notice_report(symbol="全部", date=today.replace("-", ""))
                    fetched["n"] += 1
                sub = notes_df[notes_df["公告标题"].astype(str).str.contains(name, na=False)]
                if len(sub) == 0:
                    note = f"🤖 外部金额锁定：{name}今日无公告涉金额线索（电报属日常经营类）。"
                else:
                    hits = sub[sub["公告标题"].astype(str).str.contains("|".join(MONEY_WORDS), na=False)]
                    if len(hits):
                        t0 = str(hits.iloc[0]["公告标题"])
                        c["amount"] = "外部锁定线索：" + t0[:40] + "（金额待核）"
                        c["sources"] = c.get("sources", []) + [{
                            "name": "外部锁定·当日公告", "org": "巨潮资讯（东财镜像）",
                            "url": str(hits.iloc[0]["网址"]), "role": "ext"}]
                        note = f"🤖 外部金额锁定：{name} 命中资金类公告「{t0[:30]}」→ 金额升_full待人工核。"
                    else:
                        note = f"🤖 外部金额锁定：{name} 有公告但均非资金类（{len(sub)}条），保持待评估。"
            except Exception as e:
                note = f"🤖 外部金额锁定失败（{e!r}），保持待评估。"
        c["notes"] = note + "\n" + c["notes"]
        lock[c["id"]] = {"date": today, "amount": c.get("amount"), "notes": c["notes"]}
    _save_lock(lock)
    if fetched["n"]:
        print(f"[金额锁定] 外查全市场公告 {fetched['n']} 次（按天缓存，重复运行不重复抓）")

# ---- 第二步·AI评估表：按标题片段匹配。金额均为AI估算，必须给口径，置信度注明 ----
# （估算错了不要紧，字段里写明"AI估算"会引导人工复核；转正时用户可改）
POLICY_ASSESS = [
    {"match": "境内债券市场企业所得税", "amount": "受惠债券存量约万亿级（AI估算口径：境外机构持有境内债券约3万亿元，央行托管数据量级）；免税本身影响的是其利息税负", "level": 3, "conf": "中",
     "extra": "AI评估：境外投资者利息免税延续→不构成新增现金流，但影响外资持债意愿。量级若要精确请查 centr*托管月报。"},
    {"match": "国债和地方政府债券增值税", "amount": "受惠标的仅境外发行的国债/地方债利息，规模远小于在岸债（AI估算<百亿级）；置信度低", "level": 2, "conf": "低",
     "extra": "AI评估：小众渠道的税收延续，重要度低。"},

    {"match": "限售股个人所得税", "amount": "未披露；影响面=全体限售股股东（全市场，AI估税基千亿级、年新增预扣百亿级）", "level": 3, "conf": "低",
     "extra": "AI评估：征管规则变化（预扣口径），直接金额难量化，重要在执行面。"},
    {"match": "外籍个人股息红利", "amount": "影响面=外籍个人股东（群体小），AI估百亿以内", "level": 2, "conf": "低",
     "extra": "AI评估：涉及群体窄，重要度中等。"},
]


def _assess(c):
    """第一步没金额的，第二步AI评估：匹配评估表 → 填amount/level/notes"""
    for a in POLICY_ASSESS:
        if a["match"] in (c.get("name") or ""):
            c["amount"] = a["amount"]
            c["level"] = a["level"]
            c["notes"] = f"🤖 AI评估（置信度{a['conf']}）：{a['amount']}。\n{a['extra']}\n{c['notes']}"
            return True
    # 电报类快讯也无金额 → 归为"待人工评估"，不硬编
    if "（待AI评估）" in (c.get("amount") or ""):
        c["notes"] = "🤖 AI评估：电报无金额披露，已列入待人工评估清单。\n" + c["notes"]
        return True
    return False


def _load_scan():
    for p in (HERE / "latest_scan.json", HERE.parent / "latest_scan.json"):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")), p
    # 新架构下 radar_server 不再生成 latest_scan.json；退化为读 data.json（网页唯一权威源）
    for dp in (HERE / "data.json", HERE.parent / "data.json"):
        if dp.exists():
            try:
                dj = json.loads(dp.read_text(encoding="utf-8"))
            except Exception:
                continue
            cands = (dj.get("events") or []) + (dj.get("cands") or [])
            return {"time": dj.get("updated", ""), "candidates": cands}, dp
    raise SystemExit("未找到 latest_scan.json 或 data.json——先启动雷达服务并点一次📡扫描")

data, _src = _load_scan()
cands = data["candidates"]

# ---- 财联社历史电报 → 离岸信托候选（规则推导；不写死日期/金额/来源） ----
DB = HERE / "radar.db"

# 事件类型规则：正文特征词 → 类型/分类/语义key（配置表，不是事件记录本身）
TRUST_TYPE_RULES = [
    {"kw": ["提示提醒", "申报提示"], "type": "申报提示", "category": "监管处罚", "level": 2,
     "key": "离岸信托申报", "amount": "（未披露，外部锁定待补）", "sector": "跨市场·高净值税务"},
    {"kw": ["发布公告", "印发", "明确", "公告", "规定"], "type": "政策发布", "category": "政策法规", "level": None,
     "key": "离岸信托个税", "amount": "（待AI评估）", "sector": "跨市场·高净值税务"},
]


def _trust_deadline(texts, anchor):
    """由同主题电报正文推导落地/大限日期（不写死日期）：
    ① 正文显式「过渡期内（X月X日前）」→ 该日期；
    ② 正文含「90日过渡期」→ 锚点日（政策发布日）+90天。"""
    for t in texts:
        m = re.search(r"过渡期内[（(]?(\d{1,2})月(\d{1,2})日", t)
        if m:
            try:
                return dt.date(anchor.year, int(m.group(1)), int(m.group(2))).isoformat()
            except ValueError:
                pass
    for t in texts:
        if "90日" in t:
            return (anchor + dt.timedelta(days=90)).isoformat()
    return None


def _cls_candidates():
    """从雷达库历史电报（radar.db telegrams，source='cls_hist'）挑离岸信托相关电报
    → 生成候选事件。事件内容全部由正文规则推导，不写死任何日期/金额/来源：
    · 类型/分类/星级：正文特征词（发布公告/明确→政策发布；提示提醒→申报提示）
    · 金额：政策发布未披露 → 走 POLICY_ASSESS 评估表；申报提示 → 标外部待锁定
    · 落地日/大限：「过渡期内（X月X日前）」或「90日过渡期」从正文解析，非常量
    · 来源：复用 data.json 既有同事件已验证链接（电报详情页/官网原文），无则用通用页
    当天多条取最早一条（与旧逻辑一致）。"""
    if not DB.exists():
        return []
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT date,title,content FROM telegrams WHERE source='cls_hist'"
        " AND (title LIKE '%离岸信托%' OR content LIKE '%离岸信托%') ORDER BY date").fetchall()
    conn.close()
    raw = {}
    for date, title, content in rows:
        text = f"{title or ''}{content or ''}"
        if "早间新闻精选" in text:
            continue
        if date not in raw:                 # 每天最早一条
            raw[date] = (date, title or "", text)
    if not raw:
        return []
    # 复用 data.json 既有同事件已验证来源（按 发生日+主题词 匹配，避免在代码里写死URL）
    reuse = {}
    dj = None
    for dp in (HERE / "data.json", HERE.parent / "data.json"):
        if dp.exists():
            try:
                dj = json.loads(dp.read_text(encoding="utf-8"))
                break
            except Exception:
                pass
    if dj:
        for e in dj.get("events", []):
            if "离岸信托" in (e.get("name") or ""):
                known = {s["url"] for s in reuse.get(e.get("signalDate"), [])}
                for s in (e.get("sources") or []):
                    if s.get("url") and s["url"] not in known:
                        reuse.setdefault(e.get("signalDate"), []).append(s)
    out = []
    for day, title, text in sorted(raw.values()):
        rule = next((r for r in TRUST_TYPE_RULES if any(k in text for k in r["kw"])), None)
        if not rule:
            continue
        name = (title or text)[:40]
        srcs = [{"name": "财联社电报", "org": "财联社（历史存档）",
                 "url": "https://www.cls.cn/telegraph", "role": "info"}]
        srcs += [dict(s) for s in reuse.get(day, [])]
        c = {
            "id": "R" + hashlib.md5(f"cls|{day}|{name[:60]}".encode()).hexdigest()[:8],
            "name": f"【电报】{name}" + ("…" if len(name) >= 40 else ""),
            "key": rule["key"],
            "category": rule["category"],
            "signalDate": day, "landDate": None,
            "status": "关注中",
            "level": rule["level"],
            "ai": True,
            "amount": rule["amount"],
            "sector": rule["sector"],
            "sources": srcs,
            "timeline": [{"date": day, "label": f"【发生】{name[:40]}"}],
            "watch": [],
            "notes": f"🤖 规则推导：{rule['type']}（命中关键词：{'、'.join(k for k in rule['kw'] if k in text)}）",
            "cand": True,
        }
        out.append(c)
    # 跨电报推导落地日/大限：同主题正文里的「过渡期内（X月X日前）」或「90日过渡期」
    anchor = dt.date.fromisoformat(sorted(raw)[0])
    dl = _trust_deadline([t for _, _, t in raw.values()], anchor)
    if dl:
        today = dt.date.today().isoformat()
        for c in out:
            if c["key"] == "离岸信托个税":
                c["landDate"] = dl
                c["status"] = "已落地" if dl <= today else "关注中"
                c["timeline"].append({"date": dl, "label": "【落地】公告实施之日起90日过渡期满（解析自电报正文）"})
            else:
                c["watch"] = [{"date": dl, "label": "⚠️ 90天存量申报大限"}]
    return out


def amt(c):
    m = re.search(r"([\d.]+)\s*亿", (c.get("amount") or ""))
    return float(m.group(1)) if m else None

keep, drop = [], []
for c in cands:
    # —— 当前模式：只保留财联社来源（其余源候选直接淘汰，后续需要再放开） ——
    srcs = " ".join(f"{s.get('org', '')}{s.get('name', '')}" for s in c.get("sources", []))
    if not ("财联社" in srcs or "电报" in (c.get("name") or "")):
        drop.append(c["id"])
        continue
    c["ai"] = True
    _assess(c)                # 电报无金额 → AI评估重要性（历史存档的两条带完整评估）
    if "离岸信托" in (c.get("name") or ""):
        c["key"] = "离岸信托个税"
    # —— 用户口径：ST/*ST 股票不关心 —— 与雷达引擎同口径，全流程一致 ——
    if c.get("category") != "政策法规" and (
            ST_RE.search(f"{c.get('name') or ''} {c.get('sector') or ''}") or
            re.search(r"实施其他风险警示|实施风险警示|被实施风险警示|退市风险警示", c.get("name") or "")):
        drop.append(c["id"])
        continue
    keep.append(c)

# 财联社历史电报里的离岸信托事件（规则推导；来源链接复用 data.json 已验证记录）
cls_extra = _cls_candidates()
for c in cls_extra:
    _assess(c)                 # 政策发布 → 评估表定金额/星级；申报提示无评估项，保持原样
keep += cls_extra

# 金额锁定层：外部信息源（巨潮/东财公告等）负责钉金额，同日缓存不重复外查
_lock_amounts(keep)

js = "/* AI精选.js —— 由AI处理层生成，勿手改；页面自动加载，🤖按钮合并 */\n"
js += f"window.AI_SCAN_TIME = {json.dumps(data.get('time',''), ensure_ascii=False)};\n"
js += f"window.AI_RULE = {json.dumps('当前模式：仅财联社数据源（电报快讯+历史存档），AI评估重要性→金额→落日历', ensure_ascii=False)};\n"
js += "window.AI_EVENTS = " + json.dumps(keep, ensure_ascii=False, indent=1) + ";\n"
js += "window.AI_DROP = " + json.dumps(drop, ensure_ascii=False) + ";\n"
out = HERE / "AI精选.js"
out.write_text(js, encoding="utf-8")
pol = sum(1 for c in keep if c["category"] == "政策法规")
print(f"AI精选: {len(keep)}（财联社电报流{len(keep)-len(cls_extra)} + 财联社史档{len(cls_extra)}），淘汰 {len(drop)}")
print("生成:", out)
