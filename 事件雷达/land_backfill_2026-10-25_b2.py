# -*- coding: utf-8 -*-
"""P1-1 落地日补全 第2批 + Phase A 结果记录：官方日期查证落库"""
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

def upd(eid, land="__keep__", status=None, amount="__keep__", amount_yi="__keep__", add_tl=None, add_watch=None, add_src=None, notes_extra=None):
    r = conn.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
    assert r, f"missing {eid}"
    tl = j(r["timeline"]); w = j(r["watch"]); src = j(r["sources"]); notes = r["notes"] or ""
    for item in (add_tl or []):
        if item not in tl: tl.append(item)
    tl = sorted(tl, key=lambda x: x["date"])
    for item in (add_watch or []):
        if item not in w: w.append(item)
    for item in (add_src or []):
        if not any(x.get("url") == item.get("url") for x in src): src.append(item)
    if notes_extra and notes_extra not in notes:
        notes = "; ".join([x for x in [notes, notes_extra] if x])
    land_v = r["land_date"] if land == "__keep__" else land
    amt_v = r["amount"] if amount == "__keep__" else amount
    yi_v = r["amount_yi"] if amount_yi == "__keep__" else amount_yi
    conn.execute("UPDATE events SET land_date=?, status=?, amount=?, amount_yi=?, timeline=?, watch=?, sources=?, notes=?, updated_at=? WHERE event_id=?",
        (land_v, status or r["status"], amt_v, yi_v, s(tl), s(w), s(src), notes, now, eid))
    print(f"OK {eid}: {r['name'][:26]} land={land_v}")

def upd_many(kw, **kw2):
    rows = conn.execute("SELECT event_id FROM events WHERE name LIKE ?", (f"%{kw}%",)).fetchall()
    for r in rows:
        upd(r["event_id"], **kw2)

# 1. 数字人民币扩容至30家 → 官方公告日8-17生效
upd_many("数字人民币", land="2026-08-17", status="已落地",
    add_tl=[
        {"date": "2026-04-02", "label": "第一次扩容：新增12家，运营机构增至22家"},
        {"date": "2026-08-17", "label": "【落地】央行公告新增8家（平安/恒丰/渤海/上海/杭州/徽商/长沙/广西北部湾），运营机构增至30家"},
    ],
    add_src=[{"name": "数字人民币业务运营机构扩容至30家", "org": "新华社/上海证券报", "url": "https://www.news.cn/20260818/d9654b805b5e456eaae30df0523707f0/c.html", "role": "ext"}],
    notes_extra="官方查证(2026-10-25)：央行8月17日公告，新增机构完成业务技术准备后开展业务")

# 2. 城更贷款办法 → 自印发之日起施行（8-28）
upd_many("城市更新项目贷款管理办法", land="2026-08-28", status="已落地",
    add_src=[{"name": "金融监管总局 住房城乡建设部关于印发《城市更新项目贷款管理办法（试行）》的通知（金规〔2026〕7号）", "org": "国家金融监督管理总局", "url": "https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId=1270033&itemId=928", "role": "ext"}],
    notes_extra="官方查证(2026-10-25)：第二十四条自印发之日起施行，新签借款合同项目依本办法执行")

# 3. DRG基层病组 → 官方时间表 2026-12-31切换准备 / 2027-03-31落地应用
upd_many("DRG", 
    add_watch=[
        {"date": "2026-12-31", "label": "3.0版分组切换准备截止（医保局：按病种付费地区）"},
        {"date": "2027-03-31", "label": "3.0版分组落地应用（医保局官方时间）"},
    ],
    add_tl=[{"date": "2026-09-02", "label": "【发生】DRG/DIP付费3.0版分组方案发布（DRG 492核心病组/825细分组，DIP 5125病种；首批DRG基层病组31个）"}],
    add_src=[{"name": "国家医保局：推进DRG/DIP3.0版分组落地实施", "org": "央广网", "url": "https://health.cnr.cn/yg/20260902/t20260902_527802351.shtml", "role": "ext"}],
    notes_extra="官方查证(2026-10-25)：3.0版9月2日发布，切换2026年底前、落地2027年3月底前")

# 4. 长江存储已问询 → 募资330亿官方确认
rows = conn.execute("SELECT event_id FROM events WHERE event_id='E9D3FBE1468A0'").fetchall()
for r in rows:
    upd(r["event_id"],
        amount="拟募资330亿元（208亿量产线技术升级+122亿研发，科创板拟募资纪录）", amount_yi=330.0,
        add_tl=[{"date": "2026-08-21", "label": "IPO申请获上交所受理（招股书披露，受理到问询仅9个工作日）"}],
        add_src=[
            {"name": "长江存储控股股份有限公司 科创板IPO 审核信息（上交所）", "org": "上海证券交易所", "url": "https://www.sse.com.cn/listing/renewal/ipo/index_listing_detail.shtml?auditId=2251", "role": "ext"},
            {"name": "长江存储科创板IPO审核状态变更为已问询", "org": "IT之家", "url": "https://www.ithome.com/0/998/284.htm", "role": "ext"},
        ],
        notes_extra="官方查证(2026-10-25)：拟公开发行19.8亿至24.3亿股，募资330亿元；2026Q1归母净利333.79亿元")
# 辅导验收事件也补金额口径
upd_many("长江存储控股IPO辅导状态", notes_extra="官方查证(2026-10-25)：招股书拟募资330亿元（8-21受理）")

# 5. 分众收购新潮 → 反垄断附条件批准 10-10
upd_many("新潮传媒",
    add_tl=[
        {"date": "2026-09-18", "label": "向市场监管总局提交附加限制性条件承诺方案（9-19生效，每年报告履行情况）"},
        {"date": "2026-10-10", "label": "【落地】市场监管总局附加限制性条件批准（经营者集中反垄断审查通过）"},
    ],
    add_src=[{"name": "市场监管总局关于附加限制性条件批准分众传媒收购新潮传媒股权案公告", "org": "市场监管总局", "url": "https://www.samr.gov.cn/jzxts/tzgg/ftjpz/art/2026/art_dffe5a3714b342d5a003d782f8ba91a4.html", "role": "ext"}],
    notes_extra="官方查证(2026-10-25)：反垄断附条件批准10-10公告，尚需交易所审核+证监会注册后交割")

# 6. SK海力士回购 → 10-21完成
upd_many("SK海力士",
    land="2026-10-21", status="已落地",
    add_tl=[
        {"date": "2026-08-20", "label": "启动40.0434万亿韩元回购（计划期8-20至11-19，回购2407万股约3.3%）"},
        {"date": "2026-10-21", "label": "【落地】回购提前约一个月完成（韩国上市公司史上最大规模股票注销，注销安排10-28业绩公告时）"},
    ],
    add_src=[{"name": "SK海力士完成了40万亿韩元股票回购", "org": "财联社", "url": "https://www.cls.cn/detail/2511015", "role": "ext"}],
    notes_extra="官方查证(2026-10-25)：10月21日完成回购，此前公告期限8-20至11-19")

# 7. 软银零售债券 → 9-4定价落地
upd_many("软银",
    land="2026-09-04", status="已落地",
    add_tl=[{"date": "2026-09-04", "label": "【落地】债券定价发行（日本史上最大零售债，9-16付息/2033-09-16到期）"}],
    notes_extra="官方查证(2026-10-25)：1万亿日元7年期零售债券，票息指引4.3%~4.9%，定价2026-09-04")

# 8. 华峰化学收购 → 股东会/注册流程中，补官方时间线
upd_many("华峰化学",
    add_tl=[
        {"date": "2026-09-02", "label": "【发生】披露重组草案：发行股份及支付现金购买华峰合成树脂100%（41.5亿）+华峰热塑100%（27亿），合计68.5亿"},
        {"date": "2026-09-23", "label": "股东会审议通过重组相关议案"},
    ],
    add_src=[
        {"name": "华峰化学 第九届董事会第二十四次会议决议公告", "org": "巨潮资讯", "url": "http://static.cninfo.com.cn/finalpage/2026-09-03/1225545042.PDF", "role": "ext"},
    ],
    notes_extra="官方查证(2026-10-25)：交易作价41.5亿+27亿=68.5亿，业绩承诺3年净利合计不低于18亿；尚需交易所审核+证监会注册")

# 9. 广汽收购一汽丰田 → 预案披露，作价未定
upd_many("一汽丰田",
    add_tl=[{"date": "2026-09-28", "label": "【发生】披露重大资产重组预案：拟发行股份购买一汽股份持有一汽丰田50%股权，股票复牌"}],
    add_src=[{"name": "广汽集团：拟通过发行股份购买一汽丰田50%股权，股票明日开市起复牌", "org": "每日经济新闻", "url": "https://www.nbd.com.cn/articles/2026-09-28/4593832.html", "role": "ext"}],
    notes_extra="官方查证(2026-10-25)：交易完成后广汽50%/丰田体系50%；一汽仍通过销售公司FTMS持股38%；作价未定")

# 10. 华龙一号核电 → 核准已发生，开工日无官方时间
upd_many("华龙一号",
    add_tl=[{"date": "2026-08-01", "label": "【发生】中国核电公告：国常会核准庄河1/2号+金七门3/4号机组；同批合计8台机组（含太平岭三期/莱阳一期），总投资超1700亿元，其中6台采用华龙一号"}],
    add_src=[
        {"name": "中国核电关于辽宁庄河核电、浙江金七门核电项目核准的公告", "org": "上海证券交易所", "url": "https://static.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-08-01/601985_20260801_LY6U.pdf", "role": "ext"},
        {"name": "华龙一号2.0版示范项目获国务院常务会议核准", "org": "新华网", "url": "https://www.news.cn/20260731/45b3e587d06c4ff69a351a59983fb14d/c.html", "role": "ext"},
    ],
    notes_extra="官方查证(2026-10-25)：核准批复即落地节点已发生；正式开工日以业主公告为准（现场推进开工前准备）")

conn.commit(); conn.close()
print("第2批落地日补全完成")

