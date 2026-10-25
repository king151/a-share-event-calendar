# -*- coding: utf-8 -*-
"""
事件雷达：把财联社电报发布时刻(到分钟)精确写回 timeline 的【发生】节点。
依据：data.json 每个事件 evidence 存的是本地 telegrams.id，可经 telegrams.ts 取到发布秒级时间戳。
规则：
  - 每个事件取 evidence 对应电报里 ts 最早的一条 = 财联社首次发布时刻 → 作为该事件【发生】时刻。
  - 已有【发生】节点：把最早那条节点日期改写为 "YYYY-MM-DD HH:MM"（真实发布时刻）。
  - 无【发生】节点：在 timeline 头部插入 "【发生】事件名"（带发布时刻）。
  - 不动【落地】/关注 节点（落地日是"日"，非发布时刻）。
输出：先 dry（--dry 只看统计），正式运行备份 data.json.bak_minute 后写回。
"""
import json, sqlite3, datetime, re, shutil, os, sys

DIR = r"F:\随心\事件雷达"
DATA = os.path.join(DIR, "data.json")
DB = os.path.join(DIR, "radar.db")


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    d = json.load(open(DATA, encoding="utf-8"))
    evs = d["events"]

    enriched_events = 0
    enriched_nodes = 0
    inserted_nodes = 0
    no_evidence = []

    for e in evs:
        evid = e.get("evidence") or []
        if not evid:
            no_evidence.append(e.get("name") or "")
            continue
        # evidence -> ts，取最早一条 = 首次发布时刻
        earliest = None
        for eid in evid:
            try:
                r = cur.execute("SELECT ts FROM telegrams WHERE id=?", (int(eid),)).fetchone()
            except Exception:
                r = None
            if r and r[0]:
                if earliest is None or r[0] < earliest:
                    earliest = r[0]
        if earliest is None:
            no_evidence.append(e.get("name") or "")
            continue
        dmin = datetime.datetime.fromtimestamp(earliest)
        dtstr = dmin.strftime("%Y-%m-%d %H:%M")

        tl = e.get("timeline") or []
        # 找到第一个【发生】节点
        occ_idx = None
        for i, n in enumerate(tl):
            if (n.get("label") or "").startswith("【发生】"):
                occ_idx = i
                break
        if occ_idx is not None:
            n = tl[occ_idx]
            if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", n.get("date") or ""):
                n["date"] = dtstr
                enriched_nodes += 1
                enriched_events += 1
        else:
            name = e.get("name") or ""
            tl.insert(0, {"date": dtstr, "label": f"【发生】{name[:40]}"})
            e["timeline"] = tl
            inserted_nodes += 1
            enriched_events += 1

    print(f"事件总数: {len(evs)}")
    print(f"精确回补成功: {enriched_events}/{len(evs)}")
    print(f"  - 改写已有【发生】节点时间: {enriched_nodes} 个")
    print(f"  - 补插带发布时刻的【发生】节点: {inserted_nodes} 个")
    print(f"无evidence、无法回补: {len(no_evidence)} 个")
    for m in no_evidence[:10]:
        print("   ", m)

    DRY = "--dry" in sys.argv
    if DRY:
        print("[DRY-RUN] 未写盘。去掉 --dry 后正式写回。")
        return

    bak = DATA + ".bak_minute"
    shutil.copy2(DATA, bak)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print("已备份 ->", os.path.basename(bak))
    print("已写回 data.json")


if __name__ == "__main__":
    main()
