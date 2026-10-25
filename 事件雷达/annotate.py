# -*- coding: utf-8 -*-
"""
LLM 语义判读助手（事件雷达 v3 · 判读层）

用法：
  python annotate.py dump  --date 2026-08-19 [--limit N] [--out 路径.jsonl]
       导出某天待判电报，每行 {"id","date","title","content"}，供 LLM 逐条语义判读
  python annotate.py apply <verdicts.json>
       应用判读结果：电报置 judged/dropped；事件按 key 去重 upsert 进 events 表
  python annotate.py stats
       判读进度（总数/待判/已判/丢弃/事件数/标签分布）

verdict 单条结构：
  {"id": 电报id, "is_event": true|false,
   "event": {
      "key": "同一事件的稳定去重键，如 宇树科技-科创板上市-20260819",
      "name": "事件名",
      "category": "政策法规 | 行业事件 | 公司大事件 | 监管处罚",
      "tags": ["IPO上市", ...],
      "importance": "高|中|低",
      "level": 1|2|3|4,           # 1★<10亿 2★10~100亿 3★100~1000亿 4★>1000亿/全局级
      "scope": "watch|archive",   # watch=正式日历（默认）；archive=存档（个股小事件）
      "signal_date": "YYYY-MM-DD",  # 发生日（首次公开日）
      "land_date": "YYYY-MM-DD",    # 落地日（上市/施行/执行日），未知可留空
      "status": "待落地|已落地|关注中",
      "amount_yi": 4449.0,          # 涉及金额（亿元），无可不填
      "amount": "总市值4449亿元",
      "sector": "机器人/宇树科技",
      "sources": [{"name":"财联社电报","org":"财联社","url":"https://www.cls.cn/detail/xxx","role":"info"}],
      "timeline": [{"date":"2026-08-17","label":"【发生】官宣将于8月19日上市"}],
      "watch":    [{"date":"2026-08-19","label":"【落地】正式登陆科创板"}],
      "notes": "一句话说明为什么重要"
   } | null}

判读口径（2026-10-24 用户决定，必须遵守）：
  ✅ 建事件（watch）：
     · 政策法规：财税/货币/监管/行业新政，涉及大额资金流动或全局影响面（金额可估算或影响全市场）
     · 行业事件：行业层面资金动向（产业基金、行业整合、产能扩张、价格战、补贴、关税等，金额≥10亿或影响面大）
     · 大型公司上市：IPO/借壳/分拆上市，募资或市值≥50亿（或处于关键阶段：辅导验收/过会/注册/备案/申购）
     · 百亿级公司资金事件：并购/重组/再融资/大额合同/回购等，金额≥100亿
     · 大额机构监管处罚：罚没≥10亿（个人小额处罚不建）
  ❌ 不建事件（个股层面，除非金额≥100亿）：
     个股收购重组、停牌复牌、增减持、立案调查、小额处罚（千万级）、退市、个股合同中标、
     个股增资设厂、解禁、业绩披露、行情流水、宏观统计披露、会见外交
  金额：优先电报原文直接披露；原文无直接金额时，必须调查补全（查官网原文/公告/权威新闻），
       仍无法锁定则 amount_yi 留空并 notes 注明"金额待调查"，不得凭空编造。
  落地日：必须以官方信息为准（上市日/施行日/执行日），不得用发生日+天数推算冒充。

标签体系（判读时统一使用）：
  资金动向类：IPO上市 / 新股申购 / 募资定增 / 大额回购 / 大额合同 / 大额投资
              / 并购重组 / 跨境资金 / 股权变更
  政策行业类：政策法规 / 行业事件 / 产业基金 / 关税制裁 / 补贴退坡
  风险事件类：监管处罚 / 破产重整 / 立案调查
  其他：      宏观数据(丢弃) / 业绩披露(丢弃) / 行情流水(丢弃) / 个股流水(不建)

去重保障：
  · telegrams.hash 唯一 → 同一条电报永不重复入库
  · telegrams.status 非 pending 不重复判读 → 处理进度可见、可续跑
  · events.key 唯一 → 同一事件多条电报合并为一条，不重复建事件
"""
import sys
import json
import re
import hashlib
import sqlite3
import datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "radar.db"
JUDGE_DIR = HERE / "judge_queue"
JUDGE_DIR.mkdir(exist_ok=True)


def dbconn():
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ev_id(key):
    return "E" + hashlib.md5(key.encode("utf-8")).hexdigest()[:12].upper()


def _norm_name(s):
    """事件名归一化：去掉【电报】前缀、括号内日期、标点、空白"""
    s = s or ""
    s = s.replace("【电报】", "").replace("[电报]", "").replace("电报", "")
    s = re.sub(r"[（(]\d{4}-\d{2}-\d{2}[)）]", "", s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。、；：！？“”‘’（）()\[\]【】《》…—\-]", "", s)
    return s.rstrip(".")


def _lcs_len(a, b):
    """最长公共子串长度（行内存版）"""
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0
    dp = [0] * (lb + 1)
    best = 0
    for i in range(la):
        prev = 0
        for j in range(lb):
            tmp = dp[j + 1]
            if a[i] == b[j]:
                dp[j + 1] = prev + 1
                best = max(best, dp[j + 1])
            else:
                dp[j + 1] = 0
            prev = tmp
    return best


def find_similar_event(conn, ev):
    """同一事件防重复：key 未命中时，按 同signal_date+归一化名称 找已有事件并入。

    规则（宁缺毋滥，防误并不同主体）：
      ① 归一化名称相等 → 合并
      ② 互相包含（较短者≥6字）→ 合并（标题精简/扩展关系）
      ③ 前2字相同 且 最长公共子串 ≥ max(12, 0.6×较短长度) → 合并
    不满足则返回 None（新建事件）。
    """
    sig = ev.get("signal_date")
    if not sig:
        return None
    rows = conn.execute("SELECT * FROM events WHERE signal_date=?", (sig,)).fetchall()
    if not rows:
        return None
    n = _norm_name(ev.get("name"))
    if not n:
        return None
    for r in rows:
        rn = _norm_name(r["name"])
        if not rn:
            continue
        if n == rn:
            return r
        short, long_ = (n, rn) if len(n) <= len(rn) else (rn, n)
        if len(short) >= 6 and short in long_:
            return r
        if n[:2] == rn[:2] and len(n) >= 6 and len(rn) >= 6:
            L = _lcs_len(n, rn)
            if L >= max(12, int(0.6 * min(len(n), len(rn)))):
                return r
    return None


def _j(v, dft=None):
    try:
        return json.loads(v) if v else (dft or [])
    except Exception:
        return dft or []


# ---------------- dump ----------------
def cmd_dump(args):
    date = args.get("date") or ""
    limit = int(args.get("limit") or 0)
    out = args.get("out")
    if not out:
        out = str(JUDGE_DIR / f"batch_{date}.jsonl") if date else str(JUDGE_DIR / "batch_all.jsonl")
    conn = dbconn()
    sql = "SELECT id,date,title,content FROM telegrams WHERE status='pending'"
    q = []
    if date:
        sql += " AND date=?"
        q.append(date)
    sql += " ORDER BY id"
    rows = conn.execute(sql, q).fetchall()
    conn.close()
    if limit > 0:
        rows = rows[:limit]
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            content = (r["content"] or "")[:300] + ("…[截断]" if len(r["content"] or "") > 300 else "")
            f.write(json.dumps({"id": r["id"], "date": r["date"], "title": r["title"],
                                "content": content}, ensure_ascii=False) + "\n")
    print(f"已导出 {len(rows)} 条待判电报 → {out}")
    print(f"判读后请用: python annotate.py apply {JUDGE_DIR / ('verdicts_' + date + '.json') if date else 'verdicts.json'}")


# ---------------- apply ----------------
def _merge_ev(existing, ev, tel_id, now):
    """同一 key 事件合并：保留更完整信息，不重复建事件"""
    tags = sorted(set(_j(existing["tags"]) + ev.get("tags", [])))
    sources = {s["url"]: s for s in _j(existing["sources"]) + ev.get("sources", [])}
    tl = {(t["date"], t["label"]): t for t in _j(existing["timeline"]) + ev.get("timeline", [])}
    w = {(t["date"], t["label"]): t for t in _j(existing["watch"]) + ev.get("watch", [])}
    evd = sorted(set(_j(existing["evidence"]) + [tel_id]))
    # 金额/星级：取更完整的（有金额优先，level 取高）
    old_yi, new_yi = existing["amount_yi"], ev.get("amount_yi")
    amount = existing["amount"] if old_yi and (new_yi is None or old_yi >= new_yi) else ev.get("amount")
    amount_yi = old_yi if old_yi and (new_yi is None or old_yi >= new_yi) else new_yi
    def _lvl(x):
        try:
            return int(str(x).rstrip("\u2605")) if x else 0
        except Exception:
            return 0
    level = max(_lvl(existing["level"]), _lvl(ev.get("level")))
    # 日期：signal 取更早，land 取非空
    signal = min([x for x in [existing["signal_date"], ev.get("signal_date")] if x] or [""])
    land = ev.get("land_date") or existing["land_date"] or ""
    status = "已落地" if land else (existing["status"] or "待落地")
    if existing["land_date"] and not land:
        land = existing["land_date"]
        status = "已落地"
    merged_notes = ""
    for n in [existing["notes"], ev.get("notes")]:
        if n and n not in merged_notes:
            merged_notes = "；".join([x for x in [merged_notes, n] if x])
    notes = merged_notes
    scope = ev.get("scope") or existing["scope"] or "watch"
    return {
        "name": ev.get("name") or existing["name"],
        "scope": scope,
        "category": ev.get("category") or existing["category"],
        "tags": json.dumps(tags, ensure_ascii=False),
        "importance": (ev.get("importance") or existing["importance"] or ""),
        "level": level,
        "signal_date": signal,
        "land_date": land,
        "status": status,
        "amount_yi": amount_yi,
        "amount": amount,
        "sector": ev.get("sector") or existing["sector"],
        "sources": json.dumps(list(sources.values()), ensure_ascii=False),
        "timeline": json.dumps(list(tl.values()), ensure_ascii=False),
        "watch": json.dumps(list(w.values()), ensure_ascii=False),
        "notes": notes,
        "evidence": json.dumps(evd, ensure_ascii=False),
        "updated_at": now,
    }


def cmd_apply(args):
    path = Path(args["path"])
    if not path.exists():
        print(f"verdicts 文件不存在: {path}")
        sys.exit(1)
    verdicts = json.loads(path.read_text(encoding="utf-8"))
    conn = dbconn()
    now = dt.datetime.now().isoformat(timespec="seconds")
    n_judged = n_dropped = n_events = n_merged = n_skipped = 0
    try:
        for v in verdicts:
            tel_id = int(v["id"])
            tel = conn.execute("SELECT id,status FROM telegrams WHERE id=?", (tel_id,)).fetchone()
            if tel is None or tel["status"] != "pending":
                n_skipped += 1
                continue
            if not v.get("is_event"):
                conn.execute("UPDATE telegrams SET status='dropped', judged_at=? WHERE id=?", (now, tel_id))
                n_dropped += 1
                continue
            ev = v["event"]
            if not ev or not ev.get("key"):
                print(f"警告: 电报 {tel_id} 标为事件但缺 key，跳过")
                n_skipped += 1
                continue
            key = ev["key"]
            existing = conn.execute("SELECT * FROM events WHERE key=?", (key,)).fetchone()
            if existing is None:
                # 防重复：LLM 给同一事件的 key 可能不稳定（带/不带"【电报】"、标题截断），
                # key 未命中时按 同发生日+名称相似 并入已有事件，避免同一事件拆成两条
                existing = find_similar_event(conn, ev)
                eid = _ev_id(key)
                conn.execute(
                    """INSERT INTO events(event_id,key,name,category,tags,importance,level,
                       signal_date,land_date,status,amount_yi,amount,sector,sources,timeline,watch,
                       notes,evidence,scope,updated_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, key, ev.get("name"), ev.get("category"),
                     json.dumps(ev.get("tags", []), ensure_ascii=False),
                     ev.get("importance"), ev.get("level"), ev.get("signal_date"), ev.get("land_date"),
                     ev.get("status") or ("已落地" if ev.get("land_date") else "待落地"),
                     ev.get("amount_yi"), ev.get("amount"), ev.get("sector"),
                     json.dumps(ev.get("sources", []), ensure_ascii=False),
                     json.dumps(ev.get("timeline", []), ensure_ascii=False),
                     json.dumps(ev.get("watch", []), ensure_ascii=False),
                     ev.get("notes"), json.dumps([tel_id]),
                     ev.get("scope") or "watch", now, now))
                n_events += 1
                eid_row = eid
            else:
                merged = _merge_ev(existing, ev, tel_id, now)
                conn.execute(
                    """UPDATE events SET name=?,category=?,tags=?,importance=?,level=?,signal_date=?,
                       land_date=?,status=?,amount_yi=?,amount=?,sector=?,sources=?,timeline=?,watch=?,
                       notes=?,evidence=?,scope=?,updated_at=? WHERE event_id=?""",
                    (merged["name"], merged["category"], merged["tags"], merged["importance"],
                     merged["level"], merged["signal_date"], merged["land_date"], merged["status"],
                     merged["amount_yi"], merged["amount"], merged["sector"], merged["sources"],
                     merged["timeline"], merged["watch"], merged["notes"], merged["evidence"],
                     merged["scope"], now, existing["event_id"]))
                n_merged += 1
                eid_row = existing["event_id"]
            conn.execute("UPDATE telegrams SET status='judged', event_id=?, judged_at=? WHERE id=?",
                         (eid_row, now, tel_id))
            n_judged += 1
        conn.execute("INSERT INTO meta(k,v) VALUES('last_judge_at',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (now,))
        conn.commit()
    finally:
        conn.close()
    print(f"应用完成: 判出事件 {n_judged} 条（新建 {n_events} / 合并 {n_merged}），丢弃 {n_dropped}，跳过 {n_skipped}")


# ---------------- stats ----------------
def cmd_stats(_args):
    conn = dbconn()
    total = conn.execute("SELECT COUNT(*) c FROM telegrams").fetchone()["c"]
    judged = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='judged'").fetchone()["c"]
    dropped = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='dropped'").fetchone()["c"]
    filtered = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='filtered'").fetchone()["c"]
    ev = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    by_date = conn.execute(
        "SELECT date, SUM(status='pending') p, SUM(status='judged') j FROM telegrams GROUP BY date ORDER BY date DESC LIMIT 12")
    tags = {}
    for r in conn.execute("SELECT tags FROM events"):
        for t in _j(r["tags"]):
            tags[t] = tags.get(t, 0) + 1
    by_date = [dict(r) for r in by_date]
    conn.close()
    print(f"电报总数 {total} | 待判(硬候选) {total - judged - dropped - filtered} | 已判 {judged} | 丢弃 {dropped} | 已过滤(跳过) {filtered} | 事件 {ev}")
    print("最近日期进度:")
    for r in by_date:
        print(f"  {r['date']}: 待判 {r['p'] or 0} / 已判 {r['j'] or 0}")
    print("事件标签分布:", ", ".join(f"{k}={v}" for k, v in sorted(tags.items(), key=lambda x: -x[1])))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    d = {}
    i = 0
    while i < len(rest):
        if rest[i].startswith("--"):
            k = rest[i][2:]
            v = rest[i + 1] if i + 1 < len(rest) and not rest[i + 1].startswith("--") else "1"
            d[k] = v
            i += 1 if v == "1" else 2
        else:
            d.setdefault("path", rest[i])
            i += 1
    if cmd == "dump":
        cmd_dump(d)
    elif cmd == "apply":
        cmd_apply(d)
    elif cmd == "stats":
        cmd_stats(d)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
