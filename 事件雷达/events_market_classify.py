# -*- coding: utf-8 -*-
"""
events_market_classify.py — 全部事件打三类市场视野标：A股 / 港股 / 外资（默认A股）
用途：页面默认只看 A股；港股/外资为次要视野。规则自动分类 + OVERRIDES 人工修正，幂等可重跑。
再做：UPDATE events SET market=?；打印全量分配清单（UTF-8 文件供人工复核）。
"""
import io
import re
import sys
import json
import sqlite3
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DB = HERE / "radar.db"

# 外资主体关键词（海外公司/机构/政府/财团）——注意裸词误伤：现代(?!化)、高通(?!量)、大众仅指车企形式
FOREIGN_RE = re.compile(
    r"微软|Meta|英伟达|谷歌|苹果|亚马逊|OpenAI|Anthropic|特斯拉|马斯克|SpaceX|xAI|甲骨文|软银|"
    r"三星|SK海力士|SK海力|台积电|联发科|高通(?!量)|AMD|英特尔|博通|美光|索尼|丰田|本田|日产|"
    r"辉瑞|礼来|诺和诺德|强生|默沙东|阿斯利康|罗氏|诺华|"
    r"OpenRouter|CoreWeave|Palantir|Oracle|Cursor|HuggingFace|Hugging Face|派拉蒙|华纳兄弟|迪士尼|"
    r"Netflix|奈飞|Equinix|Vantage|Adnoc|阿达尼|GIC|淡马锡|黑石|KKR|贝恩|贝莱德|高盛|摩根|标普道琼斯|"
    r"PIF|主权基金|Stargate|星际之门|ElPaso|彭博|花旗|瑞银|巴克莱|德意志银行|三菱UFJ|三井|"
    r"美联储|美国财政部|美债|美股|纳斯达克|MSCI|富时罗素|阿斯麦|ASML|日月光|东京电子|应用材料|KLA|泛林|"
    r"现代(?!化)|LG|波音|空客|雷神|洛克希德|诺斯罗普|西门子|施耐德|博世|大众汽车|大众集团|沙特阿美|"
    r"壳牌|埃克森|雪佛龙|比特币|以太坊|稳定币|USDT|Coinbase|币安")
# 上市地点/市场层级（优先于主体关键词）：事件明确发生地说明归属
VENUE_HK_RE = re.compile(r"港股|港元|香港上市|H股|恒生|恒指|港交所|港债|在香港(发行|上市|发)|离岸人民币")
VENUE_FOREIGN_RE = re.compile(r"纳斯达克上市|纽交所|美股上市|美股IPO|NYSE|NASDAQ")
VENUE_A_RE = re.compile(r"科创板|创业板|北交所|沪市|深市|上交所|深交所|A股|定增")
# 港股上市公司（中国公司但上市地在港股）；未上市中资巨头不在此列——归A股视野
COMPANY_HK_RE = re.compile(r"京东|中国太平|比亚迪电子")

# 人工修正表：key片段（前缀匹配）→ market（规则自动结果与判定冲突时在此覆盖）
OVERRIDES_PREFIX = [
    ("Nebius-发行45亿美元可转债", "外资"),          # 海外 AI 算力租赁商可转债
    ("日本制铁-美钢蒙谷工厂", "外资"),              # 日本制铁投资美国钢铁
    ("京东-机器人战略", "港股"),                    # 京东集团(9618.HK)
    ("司法部-欧盟不当域外管辖认定", "港股"),        # 事件主体为京东
    ("司法部-欧盟外国补贴条例不当域外管辖公告", "港股"),
    ("国药现代-收购国瑞药业", "A股"),               # 纯A股并购（外部药企词误触）
    ("广汽集团-收购一汽丰田", "A股"),               # A股公司收购合资股权（"丰田"误触）
]

# ---------- 分类测试套件（典型电报/事件文本 → 期望视野），main() 先跑全套 ----------
TEST_CASES = [
    # A股：宏观政策/本土主体/未上市中资
    ("工信部等七部门印发《新型电池产业发展十五五规划》", "A股"),
    ("DeepSeek完成约1000亿元融资（投前估值约5000亿元）", "A股"),   # 未上市中资巨头 → A股视野
    ("字节跳动将获得约296亿美元银团贷款", "A股"),
    ("宁德时代拟以200亿至400亿元回购股份并注销（A股史上最大规模回购）", "A股"),
    ("财政部拟发行2026年中央金融机构注资特别国债（一期）规模1500亿元", "A股"),
    ("华虹宏力旗下无锡三期增资约41.6亿美元 大基金三期旗下基金等入股", "A股"),
    ("天合光能拟定增募资不超180亿元投建光伏项目", "A股"),
    ("国常会核准辽宁庄河、浙江金七门等四个华龙一号核电机组", "A股"),
    ("上期所郑商所广期所扩大QFI可交易品种 新增23个期货期权合约", "A股"),
    ("四川某地建设现代化产业体系推进农业现代化", "A股"),           # "现代化"不受"现代"误伤
    ("上汽通用五菱发布新车 凌余汽车时代", "A股"),
    ("中澳央行续签本币互换协议 规模扩至2200亿元", "A股"),
    # 港股：上市地点/港元/港股公司
    ("阿里巴巴拟配售800亿港元新股 全部投入AI建设", "港股"),
    ("传音控股港股上市获中国证监会备案", "港股"),
    ("SHEIN（希音）港股IPO筹资约17亿美元", "港股"),
    ("央行在香港发行2026年第七期中央银行票据", "港股"),
    ("财政部2026年将在香港发行155亿元人民币国债", "港股"),
    ("汇丰香港：港元稳定币按计划2026年下半年推出", "港股"),
    ("京东发布机器人战略 截至2028年投入百亿资源", "港股"),
    ("中国太平获财政部注资70亿元", "港股"),
    # 外资：海外主体/海外市场
    ("Meta未来支出承诺总额达8250亿美元 涉AI算力数据中心", "外资"),
    ("英伟达据悉即将达成以近140亿美元收购Hugging Face", "外资"),
    ("微软预计2027财年资本支出将达1750亿美元", "外资"),
    ("美国财政部扩大长期国债回购规模 9月9日生效", "外资"),
    ("某公司在纳斯达克挂牌上市 募资2.4亿美元", "外资"),
    ("甲骨文与美国五角大楼签10年期最高70亿美元软件订阅合同", "外资"),
    ("Anthropic与Rum Group达成137亿美元AI算力协议", "外资"),
    ("日本制铁拟30亿美元升级美国钢铁蒙谷工厂", "外资", "日本制铁-美钢蒙谷工厂30亿美元投资-20261022"),
    ("SK海力士拟回购40万亿韩元并注销", "外资"),
    # 修正表生效用例（第三元 = key 前缀，测试 OVERRIDES 命中）
    ("广汽集团拟收购一汽丰田50%股权", "A股", "广汽集团-收购一汽丰田50%-20260929"),
    ("国药现代4.06亿元收购国瑞药业51%股权", "A股", "国药现代-收购国瑞药业51%-20260819"),
]

def market_of_text(text):
    """三层优先级：港股地点/主体 → 外资地点 → A股地点 → 外资主体 → 默认A股。
    未上市中资巨头（DeepSeek/字节等）不命中任何层时自然落入 A股 默认视野。"""
    if VENUE_HK_RE.search(text) or COMPANY_HK_RE.search(text):
        return "港股"
    if VENUE_FOREIGN_RE.search(text) and not VENUE_A_RE.search(text):
        return "外资"
    if VENUE_A_RE.search(text):
        return "A股"
    if FOREIGN_RE.search(text):
        return "外资"
    return "A股"

def market_of(key, name, tags, sector):
    text = f"{key} {name} {' '.join(tags)} {sector or ''}"
    for pfx, m in OVERRIDES_PREFIX:
        if key.startswith(pfx):
            return m
    return market_of_text(text)

def run_tests():
    fails = []
    for i, case in enumerate(TEST_CASES, 1):
        text, expect = case[0], case[1]
        key = case[2] if len(case) > 2 else text
        got = market_of(key, text, [], "")
        if got != expect:
            fails.append((i, text, expect, got))
    if fails:
        print("测试失败：")
        for i, t, e, g in fails:
            print(f"  ✗ [{i}] {t[:48]}  期望{e} 得{g}")
    else:
        print(f"分类测试套件：{len(TEST_CASES)}/{len(TEST_CASES)} 全部通过")
    return not fails

def main():
    if not run_tests():
        sys.exit(1)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
    if "market" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN market TEXT DEFAULT 'A股'")
        conn.commit()
        print("[schema] events.market 已添加")
    rows = conn.execute("SELECT event_id, key, name, tags, sector, market FROM events").fetchall()
    n_new = 0
    lines = {"A股": [], "港股": [], "外资": []}
    for r in rows:
        tags = json.loads(r["tags"] or "[]")
        m = market_of(r["key"], r["name"] or "", tags, r["sector"])
        lines[m].append(r)
        if m != (r["market"] or "A股"):
            n_new += 1
        conn.execute("UPDATE events SET market=? WHERE event_id=?", (m, r["event_id"]))
    conn.commit()
    conn.close()
    out = []
    for grp in ["A股", "港股", "外资"]:
        out.append(f"== {grp}（{len(lines[grp])}）==")
        for r in lines[grp]:
            out.append(f"  [{r['market'] or '-' }→] {r['key'][:60]}  |  {r['name']}")
    Path(HERE / "market_assignments.txt").write_text("\n".join(out), encoding="utf-8")
    print(f"分配完成：A股={len(lines['A股'])} 港股={len(lines['港股'])} 外资={len(lines['外资'])} 变更={n_new}")
    print("清单 → 事件雷达/market_assignments.txt（眼检+改 OVERRIDES 重跑）")

if __name__ == "__main__":
    main()
