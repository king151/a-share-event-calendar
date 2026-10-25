# -*- coding: utf-8 -*-
"""
radar_scope.py — 事件范围治理（事件雷达 v3 · 方向调整 2026-10-24）

用户决定：去掉个股事件，保留大型公司上市发掘，目标转向大的政策/行业信息，
即"金额大且可估算的事件"。

本脚本给 events 表加 scope 列（不删除任何数据），按目标规则把事件分为：
  watch   —— 留在正式日历（政策法规 / 行业事件 / 大型IPO上市 / 百亿级并购 / 大额机构处罚）
  archive —— 存档（个股层面收购重组、增减持、停牌复牌、小额处罚、立案、退市等）

用法：
  python radar_scope.py --preview    # 只打印分类统计与明细，不写库
  python radar_scope.py --apply      # 正式标记 scope 并落库
  python radar_scope.py --apply --force  # 重新全量计算（覆盖已有 scope）
"""
import sys
import sqlite3
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "radar.db"

IPO_MIN_YI = 50.0      # "大型上市"金额门槛（募资/市值 ≥50亿；无金额的关键阶段词候选保留待调查）
MEGA_YI = 100.0        # 非政策/行业/IPO 的公司资金事件门槛（≥100亿）
PENALTY_YI = 10.0      # 监管处罚保留门槛（机构大额罚没 ≥10亿）

# ---- 归档例外补丁（按名称关键词，命中即 archive）----
EXCEPT_ARCHIVE_KW = [
    "征收税收收入", "税收收入同比增长",   # 纯宏观统计、无事件性质
    "终止分拆",                          # 分拆上市失败 = 个股层面
]

# ---- 保留例外补丁（按 event_id，无金额但确属目标方向的重大事件） ----
EXCEPT_KEEP_EVENTS = set()

# ---- 保留例外补丁（按名称关键词，命中即 watch） ----
EXCEPT_KEEP_KW = [
    "离岸信托",      # 高净值资金全球配置重大政策（4★，用户已确认）
]

def dbconn():
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_scope_col(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(events)")]
    if "scope" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN scope TEXT DEFAULT 'watch'")


def scope_of(r):
    """按新目标规则返回 watch / archive"""
    if r["event_id"] in EXCEPT_KEEP_EVENTS:
        return "watch"
    name = r["name"] or ""
    if any(kw in name for kw in EXCEPT_ARCHIVE_KW):
        return "archive"
    if any(kw in name for kw in EXCEPT_KEEP_KW):
        return "watch"
    category = r["category"] or ""
    tags = json.loads(r["tags"] or "[]")
    yi = r["amount_yi"] or 0
    # 政策法规 / 行业事件 —— 目标方向，全保留
    if category in ("政策法规", "行业事件"):
        return "watch"
    # 大型公司上市/IPO —— 保留发掘（金额≥门槛，或处于关键阶段待调查）
    is_ipo_tag = category == "公司大事件" and any(t in ("IPO上市", "新股申购") for t in tags)
    is_ipo_name = ("IPO" in name) or (
        "上市" in name and "终止上市" not in name and "退市" not in name)
    if is_ipo_tag or is_ipo_name:
        if yi and yi < IPO_MIN_YI:
            return "archive"                       # 小型IPO（募资<50亿）不算大型
        return "watch"                             # 大型或关键阶段（金额待调查）
    # 百亿级资金事件（非政策/行业/IPO）：大型并购、大额再融资、大额订单、大额回购等
    if category == "公司大事件" and yi >= MEGA_YI:
        return "watch"
    # 大额机构监管处罚（>=10亿）
    if category == "监管处罚" and yi >= PENALTY_YI:
        return "watch"
    # 其余：个股层面事件 -> 存档
    return "archive"


def main():
    args = sys.argv[1:]
    preview = "--preview" in args
    apply = "--apply" in args
    force = "--force" in args
    conn = dbconn()
    ensure_scope_col(conn)
    rows = conn.execute("SELECT * FROM events").fetchall()
    n_watch = n_archive = 0
    watch_rows, archive_rows = [], []
    for r in rows:
        old = r["scope"] or ""
        new = scope_of(r)
        if old and old != new and not force and not preview:
            print(f"  跳过(已有scope={old}，用--force重算): {r['name'][:40]}")
            continue
        if new == "watch":
            n_watch += 1
            watch_rows.append(r)
        else:
            n_archive += 1
            archive_rows.append(r)
        if apply and (force or not old or old != new):
            conn.execute("UPDATE events SET scope=? WHERE event_id=?", (new, r["event_id"]))
    if apply:
        conn.commit()
    conn.close()

    print(f"事件总数 {len(rows)}：watch(正式日历)={n_watch} | archive(存档)={n_archive}")
    if preview or apply:
        print("\n—— 保留（watch）明细 ——")
        for r in sorted(watch_rows, key=lambda x: (x["signal_date"] or ""), reverse=True):
            print(f"  {r['signal_date']} | {r['level']}★ | {r['category']} | {(r['name'] or '')[:44]} | yi={r['amount_yi']}")
        print(f"\n—— 归档（archive）共 {n_archive} 条，前 25 条 ——")
        for r in sorted(archive_rows, key=lambda x: (x["signal_date"] or ""), reverse=True)[:25]:
            print(f"  {r['signal_date']} | {r['level']}★ | {r['category']} | {(r['name'] or '')[:44]}")
    if apply:
        print("\n已写库。" if not preview else "")


if __name__ == "__main__":
    main()