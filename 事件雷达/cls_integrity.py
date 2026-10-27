# -*- coding: utf-8 -*-
"""财联社电报库完整性体检（只读，不改数据）：
1) 逐日量统计（近 14 天）——找"空心日"
2) 连续性缺口：相邻电报时间差 > 高频阈值 即报（07-24 点 > 60 分钟；00-07 点 > 4 小时）
3) 头部新鲜度：最新电报距现在多久

用法：python cls_integrity.py
服务端另有自动版：radar_server.py 的 gapfill 机制（ensure_fetch 后 6 小时一轮，
对检出的缺口从终点向起点定向翻页补采，source 落 cls_gap 便于审计）。
本脚本用于人工目检/演示日常水位。"""
import io, sys, datetime as dt, sqlite3
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
DB = Path(__file__).resolve().parent / "radar.db"

conn = sqlite3.connect(str(DB), timeout=30)
conn.row_factory = sqlite3.Row
now = dt.datetime.now()
rows = conn.execute("SELECT ts, date FROM telegrams ORDER BY ts").fetchall()
conn.close()
if not rows:
    print("库为空：先跑一次服务（启动雷达.bat）或 refer README 冷启动。")
    sys.exit(0)
print(f"DB telegrams: {len(rows)} 条 | {dt.datetime.fromtimestamp(rows[0][0]):%Y-%m-%d} ~ {dt.datetime.fromtimestamp(rows[-1][0]):%Y-%m-%d}")

# --- 头部新鲜度 ---
head = dt.datetime.fromtimestamp(rows[-1][0])
lag = now - head
print(f"\n/*1 头部新鲜度*/ 最新电报 {head:%Y-%m-%d %H:%M}，距今 {lag.total_seconds()/60:.0f} 分钟 → {'OK' if lag.total_seconds() < 3600*30 else '落后过多'}")

# --- 空心日 ---
from collections import Counter
perday = Counter(r["date"] for r in rows)
d0 = dt.date.fromisoformat(rows[0][1]); d1 = dt.date.fromisoformat(rows[-1][1])
missing, cur = [], d0
while cur <= d1:
    if cur.isoformat() not in perday:
        missing.append(cur.isoformat())
    cur += dt.timedelta(days=1)
print(f"\n/*2 空心日*/ 全程 {d0}~{d1}，完全没数据的天： {missing if missing else '无 ✓'}")

# --- 缺口（近 45 天） ---
cut = (now - dt.timedelta(days=45)).strftime("%Y-%m-%d")
recent = [r for r in rows if r["date"] >= cut]
gaps = []
for a, b in zip(recent[:-1], recent[1:]):
    ta = dt.datetime.fromtimestamp(a[0]); tb = dt.datetime.fromtimestamp(b[0])
    diff = (tb - ta).total_seconds() / 60
    big = diff > 60 if 7 <= tb.hour < 24 else diff > 240
    if big:
        gaps.append((ta, tb, diff))
print(f"\n/*3 高频时段缺口*/ 近45天 {len(recent)} 条，可疑缺口 {len(gaps)} 个（白天>60min / 夜间>4h；周末清晨微量空窗属自然低频）：")
for ta, tb, diff in gaps[:40]:
    print(f"  {ta:%Y-%m-%d %H:%M} → {tb:%H:%M}  空 {diff:.0f} 分钟（{tb:%Y-%m-%d} {'白天' if 7 <= tb.hour < 24 else '夜间'}）")

# --- 近14天逐日量 ---
print("\n/*4 近14天逐日条数*/")
for i in range(13, -1, -1):
    d = (now.date() - dt.timedelta(days=i)).isoformat()
    c = perday.get(d, 0)
    print(f"  {d}  {'▇' * min(c // 5, 40)} {c}")
