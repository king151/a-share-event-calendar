# -*- coding: utf-8 -*-
"""P1-1 落地日补全 第1批：将查证的官方落地日/关注日/时间线写入 events 表"""
import sys, io, json, sqlite3, datetime as dt
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
conn = sqlite3.connect(r"F:\随心\事件雷达\radar.db")
conn.row_factory = sqlite3.Row
now = dt.datetime.now().isoformat(timespec="seconds")

def j(v, d=None):
    try:
        return json.loads(v) if v else (d if d is not None else [])
    except Exception:
        return d if d is not None else []

def s(x):
    return json.dumps(x, ensure_ascii=False)

def upd(eid, land=None, status=None, amount=None, amount_yi=None, add_tl=None, add_watch=None, add_src=None, notes_extra=None):
    r = conn.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
    assert r, f"missing {eid}"
    tl = j(r["timeline"]);  w = j(r["watch"]);  src = j(r["sources"]);  notes = r["notes"] or ""
    for item in (add_tl or []):
        if item not in tl: tl.append(item)
    tl = sorted(tl, key=lambda x: x["date"])
    for item in (add_watch or []):
        if item not in w: w.append(item)
    for item in (add_src or []):
        if not any(x.get("url") == item.get("url") for x in src): src.append(item)
    if notes_extra and notes_extra not in notes:
        notes = "; ".join([x for x in [notes, notes_extra] if x])
    conn.execute("""UPDATE events SET land_date=?, status=?, amount=?, amount_yi=?, timeline=?, watch=?, sources=?, notes=?, updated_at=? WHERE event_id=?""",
        (land if land is not None else r["land_date"], status or r["status"], amount or r["amount"],
         amount_yi if amount_yi is not None else r["amount_yi"], s(tl), s(w), s(src), notes, now, eid))
    print(f"OK {eid}: land={land}  status={status or r['status']}")

# 1. 私募母基金规定征求意见 → 观察日 2026-11-23（证监会公告）
rows = conn.execute("SELECT event_id FROM events WHERE name LIKE '%私募投资母基金管理规定%'").fetchall()
for r in rows:
    upd(r["event_id"], add_watch=[{"date": "2026-11-23", "label": "意见反馈截止（证监会公告：公众反馈截至11月23日）"}],
        add_src=[{"name": "证监会《私募投资母基金管理规定（征求意见稿）》征求意见通知", "org": "中国证监会", "url": "https://www.csrc.gov.cn/csrc/c101981/c7666360/content.shtml", "role": "ext"}],
        notes_extra="官方查证(2026-10-25)：意见反馈截止2026年11月23日")

# 2. 财政部提前下达2027年地方债限额 → 官方指向2027年1月启动发行
rows = conn.execute("SELECT event_id FROM events WHERE name LIKE '%财政部：抓紧做好提前下达2027%' OR name LIKE '%提前下达2027年部分新增地方政府债务限额%'").fetchall()
for r in rows:
    upd(r["event_id"], add_watch=[{"date": "2027-01-01", "label": "2027年1月尽早启动债券发行（财政部发布会口径，具体日以正式下达文件为准）"}],
        add_tl=[{"date": "2026-10-23", "label": "【发生】财政部新闻发布会：四季度内在当年限额60%以内提前下达，上限3.12万亿元"}],
        add_src=[{"name": "2026年前三季度财政收支情况新闻发布会文字实录", "org": "财政部", "url": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202610/t20261023_4003429.htm", "role": "ext"}],
        notes_extra="官方查证(2026-10-25)：确保2027年1月尽早启动债券发行")

# 3&4. 中金吸并东兴/信达（两条同一合并事件） → 落地日 2026-10-19 终止上市
csrc_tl = [
    {"date": "2026-09-07", "label": "【落地】证监会出具批复：同意注册并核准吸收合并"},
    {"date": "2026-10-16", "label": "换股实施股权登记日（A股）"},
    {"date": "2026-10-19", "label": "【落地】东兴证券、信达证券终止上市，不进入退市整理期，原股东换股为中金股份"},
]
for kw in ["中金公司换股吸收合并", "中金公司吸收合并东兴证券、信达证券"]:
    rows = conn.execute("SELECT event_id FROM events WHERE name LIKE ?", (f"%{kw}%",)).fetchall()
    for r in rows:
        upd(r["event_id"], land="2026-10-19", status="已落地", add_tl=csrc_tl,
            add_src=[{"name": "中金公司公告：东兴证券、信达证券10月19日终止上市", "org": "新华财经/中国金融信息网", "url": "https://www.cnfin.com/kx/detail/20261012/4479747_1.html", "role": "ext"}],
            notes_extra="官方查证(2026-10-25)：换股登记日10月16日，东兴/信达10月19日终止上市")

# 5. 宇树 询价/申购 两条 → 落地日=上市日2026-08-19
for kw in ["宇树科技科创板IPO询价", "宇树科技科创板IPO网上申购"]:
    rows = conn.execute("SELECT event_id FROM events WHERE name LIKE ?", (f"%{kw}%",)).fetchall()
    for r in rows:
        upd(r["event_id"], land="2026-08-19", status="已落地",
            add_tl=[{"date": "2026-08-19", "label": "【落地】正式登陆科创板（开盘涨超500%，市值超3500亿元）"}],
            notes_extra="官方查证(2026-10-25)：上市日2026-08-19")

# 6-8. 粤芯三条 → 落地日=2026-10-13 上市
yue_tl = [
    {"date": "2026-09-24", "label": "网上/网下申购（发行价12.01元/股，代码301660）"},
    {"date": "2026-10-13", "label": "【落地】创业板上市（开盘45元，市值超1200亿元）"},
]
for kw in ["粤芯半导体取得创业板IPO注册批文", "粤芯股份创业板IPO注册生效并申购", "粤芯半导体IPO发行价格确定为12.01元"]:
    rows = conn.execute("SELECT event_id FROM events WHERE name LIKE ?", (f"%{kw}%",)).fetchall()
    for r in rows:
        amt = "募资最高70.8亿元（发行5.13亿股×12.01元，超额配售全额行使）；此前拟募资75亿" if "批文" in kw or "发行价格" in kw else None
        yi = 70.8 if amt else None
        upd(r["event_id"], land="2026-10-13", status="已落地", amount=amt, amount_yi=yi,
            add_tl=yue_tl,
            add_src=[{"name": "粤芯半导体：将于10月13日在创业板上市", "org": "财联社", "url": "https://www.cls.cn/detail/2501586", "role": "ext"}],
            notes_extra="官方查证(2026-10-25)：2026-10-13创业板上市，市值超1200亿")

# 9. SHEIN 通过港交所聆讯 → 落地日=2026-09-01 上市
rows = conn.execute("SELECT event_id FROM events WHERE name LIKE '%希音国际(SHEIN)通过港交所上市聆讯%'").fetchall()
for r in rows:
    upd(r["event_id"], land="2026-09-01", status="已落地",
        add_tl=[{"date": "2026-09-01", "label": "【落地】港股IPO完成（筹资约17亿美元）"}],
        notes_extra="官方查证(2026-10-25)：上市日2026-09-01（库内已上市事件佐证）")

# 10. 传音控股港股备案 → 落地日=2026-10-15
rows = conn.execute("SELECT event_id FROM events WHERE name LIKE '%传音控股港股上市获中国证监会备案%'").fetchall()
for r in rows:
    upd(r["event_id"], land="2026-10-15", status="已落地",
        amount="募资30.59亿港元（发行8664.83万股×35.30港元下限定价），净筹29.68亿港元", amount_yi=26.2,
        add_tl=[
            {"date": "2026-10-07", "label": "启动招股（区间35.30~38.80港元，基石GIC/易方达/比亚迪认购12.45亿港元）"},
            {"date": "2026-10-13", "label": "定价35.30港元（区间下限）"},
            {"date": "2026-10-15", "label": "【落地】港交所主板挂牌（02636.HK，实现A+H；首日破发跌约5.7%）"},
        ],
        add_src=[{"name": "传音控股港股上市首日破发 最多跌逾4%", "org": "观点网", "url": "https://www.guandian.cn/m/show/613532", "role": "ext"}],
        notes_extra="官方查证(2026-10-25)：2026-10-15港交所挂牌")

# 11. 第十二批药品集采 → 中选结果公布节点（官方：年内实施，执行日以各地通知为准）
rows = conn.execute("SELECT event_id FROM events WHERE name LIKE '%第十二批国家组织药品集采%' OR name LIKE '%药品集采开标%'").fetchall()
for r in rows:
    if r["event_id"] and "集采" in r["event_id"] or rows:
        pass
    upd(r["event_id"],
        add_tl=[{"date": "2026-08-06", "label": "【落地】中选结果公布（医保局：年内实施，具体执行日期以各地通知为准）"}],
        add_src=[{"name": "关于公布第12批国家组织药品集中带量采购中选结果的通知", "org": "国家医保局", "url": "https://www.nhsa.gov.cn/art/2026/8/6/art_187_21682.html", "role": "ext"}],
        notes_extra="官方查证(2026-10-25)：无统一执行日，官方口径为年内实施")

# 12. 债市互联互通 → 官方口径注明（无确切日）
rows = conn.execute("SELECT event_id FROM events WHERE name LIKE '%银行间与交易所债券市场互联互通%'").fetchall()
for r in rows:
    upd(r["event_id"],
        use_notes := None,
        add_src=[{"name": "2026金融街论坛：潘功胜宣布推动债市互联互通落地", "org": "新华财经", "url": "https://www.cnfin.com/gs-lb/detail/20261019/4483307_1.html", "role": "ext"}],
        notes_extra="官方查证(2026-10-25)：口径为'已具备落地条件，测试后即可开通'，无确切落地日")

conn.commit()
conn.close()
print("第1批落地日补全完成")
