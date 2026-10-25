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

HERE = Path(__file__).resolve().parent
DB = HERE / "radar.db"

# 外资主体关键词（海外公司/机构/政府/财团）
FOREIGN_RE = re.compile(
    r"微软|Meta|英伟达|谷歌|苹果|亚马逊|OpenAI|Anthropic|特斯拉|马斯克|SpaceX|xAI|甲骨文|软银|"
    r"三星|SK海力士|SK海力|台积电|联发科|高通|AMD|英特尔|博通|美光|索尼|丰田|本田|日产|大众|宝马|奔驰|"
    r"辉瑞|礼来|诺和诺德|强生|默沙东|阿斯利康|罗氏|诺华|"
    r"OpenRouter|CoreWeave|Palantir|Oracle|Cursor|HuggingFace|Hugging Face|派拉蒙|华纳兄弟|迪士尼|"
    r"Netflix|奈飞|Equinix|Vantage|Adnoc|阿达尼|GIC|淡马锡|黑石|KKR|贝恩|贝莱德|高盛|摩根|标普道琼斯|"
    r"PIF|主权基金|Stargate|星际之门|ElPaso|彭博|花旗|瑞银|巴克莱|德意志银行|三菱UFJ|三井|"
    r"美联储|美国财政部|美债|美股|纳斯达克|MSCI|富时罗素|阿斯麦|ASML|日月光|东京电子|应用材料|KLA|泛林|"
    r"现代|LG|波音|空客|雷神|洛克希德|诺斯罗普|西门子|施耐德|博世|沙特阿美|壳牌|埃克森|雪佛龙|"
    r"比特币|以太坊|稳定币|USDT|Coinbase|币安")
# 港股市场关键词（离岸人民币/港股通/港元融资/香港发行）
HK_RE = re.compile(r"港股|港元|香港上市|H股|恒生|恒指|港交所|港债|在香港(发行|上市|发)|离岸人民币")
# 京东/中国太平等纯港股上市公司
COMPANY_HK_RE = re.compile(r"京东|中国太平|比亚迪电子")

# 人工修正表：key片段（前缀匹配）→ market（规则自动结果与判定冲突时在此覆盖）
OVERRIDES_PREFIX = [
    ("Nebius-发行45亿美元可转债", "外资"),          # 海外 AI 算力租赁商可转债
    ("日本制铁-美钢蒙谷工厂", "外资"),              # 日本制铁投资美国钢铁
    ("美国财政部-扩大长债回购", "外资"),            # 美债操作
    ("字节跳动-银团贷款", "外资"),                  # 未上市中资巨头海外融资（非A股标的）
    ("DeepSeek-千亿级融资", "外资"),                # 未上市中资 AI（非A股标的）
    ("京东-机器人战略", "港股"),                    # 京东集团(9618.HK)
    ("司法部-欧盟不当域外管辖认定", "港股"),        # 事件主体为京东
    ("司法部-欧盟外国补贴条例不当域外管辖公告", "港股"),
    ("财政部-向中国太平注资", "港股"),              # 中国太平(0996.HK)
    ("国药现代-收购国瑞药业", "A股"),               # 纯A股并购（"辉瑞"等词误触）
    ("广汽集团-收购一汽丰田", "A股"),               # A股公司收购合资股权（"丰田"误触）
]

def market_of(key, name, tags, sector):
    text = f"{key} {name} {' '.join(tags)} {sector or ''}"
    for pfx, m in OVERRIDES_PREFIX:
        if key.startswith(pfx):
            return m
    if HK_RE.search(text) or COMPANY_HK_RE.search(text):
        return "港股"
    if FOREIGN_RE.search(text):
        return "外资"
    return "A股"

def main():
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
