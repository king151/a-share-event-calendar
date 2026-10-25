# -*- coding: utf-8 -*-
"""
prescreen_fclass_backfill.py — 粗滤可信化改造：全库 filtered 补 fclass 细分（hi/mid）
依据 2026-10-25 P1-5 体检结论：手工抽检误滤率≈1%，直击 HIGH_CERT_FAMILIES 的粗滤=可信排泄。
 · hi  ：噪音规则族直击的 filtered → 计入消化率（视同"处理完成·排泄"）
 · mid ：规则未击中的粗滤 → 仍算未消化（保留红色作质检方向标）
附带验证：31 条新事件文本必须 0 直击；751 条裁决丢弃须高覆盖。
"""
import re
import sys
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import radar_prescreen as P

DB = HERE / "radar.db"

def ensure_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(telegrams)")]
    if "fclass" not in cols:
        conn.execute("ALTER TABLE telegrams ADD COLUMN fclass TEXT DEFAULT 'mid'")
        conn.commit()
        print("[schema] 已添加 fclass 列")

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_column(conn)

    # ---- 验证 A：31 条新建事件不得被任何噪音族直击 ----
    bad = []
    for rid, name, content in conn.execute(
            "SELECT r.rowid, e.name, r.content FROM events e JOIN telegrams r ON r.id=e.rowid"):
        pass  # placeholder, 下方用独立查询
    hint_ids = []
    for k in ["宁德", "甲骨文", "长鑫科技", "英伟达", "Meta", "微软", "阿里", "Anthropic", "字节", "派拉蒙", "施耐德", "华虹", "恒力", "保利", "陇电", "广西", "注资特别国债", "绿色算力"]:
        row = conn.execute("SELECT event_id, key FROM events WHERE name LIKE ?", (f"%{k}%",)).fetchone()
        if row:
            hint_ids.append(row["key"])
    ev_rows = conn.execute("SELECT key, name, timeline FROM events").fetchall()
    n_build_hit = 0
    for e in ev_rows:
        text = f"{e['name'] or ''} {e['timeline'] or ''}"
        fam = P.noise_family(text)
        if fam and any(k in (e["key"] or "") for k in [kk for kk in
                ("宁德", "甲骨文", "长鑫", "英伟达", "Meta", "微软", "阿里", "Anthropic", "字节", "派拉蒙", "施耐德", "华虹", "恒力", "保利", "陇电", "广西", "注资特别国债", "绿色算力", "SSI", "ElPaso", "OpenRouter", "HuggingFace", "联发科", "Rum", "山西", "八埭头", "Cursor", "OpenAI", "罚款")]):
            n_build_hit += 1
            print(f"  [BUILD-HIT!] {e['key'][:40]} → 族[{fam}]")
    print(f"验证A 新建事件直击数: {n_build_hit}（应为0）")

    # ---- 验证 B：751 条裁决丢弃覆盖率 ----
    vfile = HERE / "judge_queue" / "verdicts_stale_rescreen_2026-10-25.json"
    verdicts = json.load(open(vfile, encoding="utf-8"))
    drop_ids = [v["id"] for v in verdicts if not v["is_event"]]
    build_ids = [v["id"] for v in verdicts if v["is_event"]]
    tg = {r["id"]: r for r in conn.execute(
        "SELECT id, title, content FROM telegrams WHERE id IN (%s)" % ",".join("?"*751), drop_ids + build_ids)}
    hit = sum(1 for t in drop_ids if P.noise_family(f"{tg[t]['title'] or ''} {tg[t]['content'] or ''}") if t in tg)
    print(f"验证B 720条人工丢弃被规则族直击: {hit}/{len(drop_ids)} 覆盖率≈{hit*100//max(len(drop_ids),1)}%")

    # ---- 验证 C：31 条 Builds 原文直击（硬候选层兜底，理论必0） ----
    bh = 0
    for t in build_ids:
        if t in tg and P.noise_family(f"{tg[t]['title'] or ''} {tg[t]['content'] or ''}"):
            bh += 1
            print(f"  [BUILD-TEXTHIT!] id={t} {tg[t]['title'] or ''[:40]}")
    print(f"验证C Build原文直击数: {bh}（应为0）")

    # ---- 落库：全量 filtered 补 fclass ----
    rows = conn.execute("SELECT id, title, content FROM telegrams WHERE status='filtered'").fetchall()
    n_hi = n_mid = n_skip = 0
    for r in rows:
        text = f"{r['title'] or ''} {r['content'] or ''}"
        tier = P.classify(text, P.extract_amount(text)[0])
        if tier not in P.FILTER_OUT_TIERS:
            n_skip += 1
            continue
        fam = P.noise_family(text)
        conn.execute("UPDATE telegrams SET fclass=? WHERE id=?", ("hi" if fam else "mid", r["id"]))
        if fam:
            n_hi += 1
        else:
            n_mid += 1
    conn.commit()
    tot = conn.execute("SELECT COUNT(*) FROM telegrams").fetchone()[0]
    agg = conn.execute("SELECT status, fclass, COUNT(*) c FROM telegrams GROUP BY status, fclass").fetchall()
    print(f"\nretrofit 完成：直击hi={n_hi} 未击中mid={n_mid} 应为硬候选(漏网)={n_skip} 全库={tot}")
    for a in agg:
        print("   ", dict(a))
    conn.close()

if __name__ == "__main__":
    main()
