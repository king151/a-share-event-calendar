# -*- coding: utf-8 -*-
"""
财联社电报历史回补（2026-07-24 → 现在）
· 慢速：每页间隔 CLS_SLEEP 秒，对源站友好
· 断点续上：游标逐页写入 cls_state.json，中断后重跑自动从断点继续，不重复抓
· 流式存档：每页追加 cls_backfill.jsonl（不重写大文件）
· 并行测试：一旦遇到"离岸信托"电报，立即就地跑一遍"雷达捕获→AI评估"全流程并落盘
"""
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from akshare.request import make_request_with_retry_json
from akshare.utils.cons import headers

HERE = Path(__file__).resolve().parent
URL = "https://www.cls.cn/v1/roll/get_roll_list"
RN = 50                 # 实测接口每页上限50
CLS_SLEEP = 2.0         # 页间隔（你说要慢一点）
TARGET = time.mktime((2026, 7, 24, 0, 0, 0, 0, 0, -1))   # 回补起点：7-24 00点

STATE = HERE / "cls_state.json"
DATA = HERE / "cls_backfill.jsonl"

CLS_KEYWORDS = [
    "财政部", "税务总局", "证监会", "国务院", "央行", "人民银行", "发改委",
    "印花税", "降准", "降息", "LPR", "MLF", "存款准备金", "利率", "外汇",
    "关税", "反垄断", "重大资产重组", "要约收购", "退市", "破产重整", "立案",
    "行政处罚", "IPO", "发行上市", "注册", "解禁", "减持", "回购", "信托",
]


def call(last_time):
    params = {"app": "CailianpressWeb", "category": "", "last_time": int(last_time),
              "os": "web", "refresh_type": "1", "rn": str(RN), "sv": "8.4.6"}
    params["sign"] = hashlib.md5(hashlib.sha1(urlencode(params).encode()).hexdigest().encode()).hexdigest()
    return make_request_with_retry_json(URL, params=params, max_retries=5, headers=headers)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"cursor": int(time.time()), "pages": 0, "items": 0, "trust_tested": False,
            "trust_found": None, "hit_days": {}}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")





def main():
    st = load_state()
    print(f"回补启动：游标 {time.strftime('%Y-%m-%d %H:%M', time.localtime(st['cursor']))} → 目标 2026-07-24 00:00"
          f"（已抓 {st['pages']} 页/{st['items']} 条）", flush=True)
    fails = 0
    while st["cursor"] > TARGET + 60:
        try:
            data = call(st["cursor"])
        except Exception as e:
            fails += 1
            print(f"[net] 第{fails}次失败 {e!r}（断点已存，稍后续）", flush=True)
            if fails >= 5:
                print("网络连续失败，退出保存断点。重跑本脚本即续抓。", flush=True)
                return
            time.sleep(10)
            continue
        fails = 0
        roll = (data.get("data") or {}).get("roll_data") or []
        if not roll:
            print("返回空页（可能已达最旧），提前收工。", flush=True)
            break
        with open(DATA, "a", encoding="utf-8") as f:
            for it in roll:
                key = hashlib.md5(f"{it.get('ctime')}|{it.get('title')}|{it.get('content')[:50] if isinstance(it.get('content'),str) else ''}".encode()).hexdigest()[:10]
                f.write(json.dumps({"k": key, "t": it.get("ctime"), "lv": str(it.get("level")),
                                    "ti": it.get("title"), "c": it.get("content"),
                                    "id": it.get("id")}, ensure_ascii=False) + "\n")
        st["items"] += len(roll)
        st["pages"] += 1
        oldest = min(int(r["ctime"]) for r in roll)
        st["cursor"] = min(st["cursor"], oldest)
        d = time.strftime("%Y-%m-%d", time.localtime(oldest))
        st["hit_days"][d] = st["hit_days"].get(d, 0) + len(roll)
     
        save_state(st)
        remain = (st["cursor"] - TARGET) / 86400.0
        if st["pages"] % 25 == 0:
            print(f"[{st['pages']:4d}页/{st['items']:6d}条] 游标 {time.strftime('%Y-%m-%d %H:%M', time.localtime(st['cursor']))}"
                  f"（剩余约 {remain:.0f} 天）", flush=True)
        time.sleep(CLS_SLEEP)
    save_state(st)
    print(f"回补完成：共 {st['pages']} 页 / {st['items']} 条，游标停在 {time.strftime('%Y-%m-%d %H:%M', time.localtime(st['cursor']))}", flush=True)


if __name__ == "__main__":
    main()
