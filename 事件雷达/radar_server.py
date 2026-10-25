# -*- coding: utf-8 -*-
"""
A股大事件日历 · 事件雷达 v3（语义引擎）

架构变更（2026-10-24）：
  旧版用正则规则词典（CLS_EVENT_RULES / _cls_engine / CLS_SKIP_RE）做文字判读，
  存在：无新股上市规则漏掉宇树IPO、否定句误报、金额正则崩溃拖垮全批、静默失败等缺陷。
  现改为【抓取 ≠ 判读】：
    · 抓取层只负责拉电报 → 按 hash 去重写进 SQLite telegrams 表（不做任何文字规则判断）
    · 判读层由 LLM（annotate.py，逐批语义判读）完成：打标签/重要度/落地时间/金额
    · events 表按 key 去重：同一事件的多条电报合并成一条，不会重复处理
    · 进度表（telegrams.status + meta）让每条电报的处理状态可见

数据流：
  财联社电报(实时+历史存档) → telegrams(pending) → LLM判读 → verdicts → events → /api/scan → 网页

HTTP：
  GET /api/scan     网页主数据（兼容旧页面：candidates + progress）
  GET /api/progress 判读进度
  GET /api/pending  待判电报（可加 ?date=YYYY-MM-DD&limit=N）
  GET /api/events   事件表（?tag=IPO&status=待落地&scope=all 过滤；默认仅正式日历watch）
  GET /api/ping     存活检测
"""
import json
import hashlib
import sqlite3
import datetime as dt
import traceback
import threading
import logging
import logging.handlers
import shutil
import re
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============ 配置 ============
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 预筛规则引擎（入库瞬间分流：HIGH_CERT 噪音族直进 filtered，硬候选留 pending）
try:
    import radar_prescreen
except Exception:                                   # 规则模块缺位时降级为全量 pending，不影响可用性
    radar_prescreen = None

# ============ 日志：落盘轮转（pythonw 运行时 print 不可见，所有异常必须留痕） ============
LOG_FILE = HERE / "radar.log"
logger = logging.getLogger("radar")
logger.setLevel(logging.INFO)
_rot = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_rot.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_rot)
_LOG = logger.info
_ERR = logger.error

import akshare as ak

DB = HERE / "radar.db"
PORT = 7610
TTL_HOURS = {"cls_live": 1, "cls_hist": 24}   # 抓取节制：源站友好，与判读无关
HIST_FILE = HERE / "cls_backfill.jsonl"
LOCK = threading.Lock()


# ============ 数据库 ============
SCHEMA = """
CREATE TABLE IF NOT EXISTS telegrams(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  hash       TEXT UNIQUE NOT NULL,
  ts         INTEGER,
  date       TEXT,
  title      TEXT,
  content    TEXT,
  source     TEXT,                 -- cls_live / cls_hist
  status     TEXT DEFAULT 'pending',   -- pending 待LLM判读 / judged 已判出事件 / dropped 判定无事件 / filtered 预筛跳过
  fclass     TEXT DEFAULT 'mid',   -- filtered 细分：hi=高确定性噪音规则直击（可信排泄，计入消化率） / mid=规则未击中
  event_id   TEXT,
  judged_at  TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  event_id   TEXT PRIMARY KEY,
  key        TEXT UNIQUE NOT NULL,   -- 同一事件去重键（LLM给出）
  name       TEXT,
  category   TEXT,                   -- 政策法规 / 公司大事件 / 行业事件 / 监管处罚
  tags       TEXT DEFAULT '[]',      -- JSON数组：IPO/并购重组/再融资/减持增持/大额合同/资金监管…
  importance TEXT,                   -- 高 / 中 / 低
  level      INTEGER,                -- 1★<10亿 2★10~100亿 3★100~1000亿 4★>1000亿/全局
  signal_date TEXT,                  -- 发生日（首次公开）
  land_date  TEXT,                   -- 落地日（施行/执行）
  status     TEXT,                   -- 待落地 / 已落地 / 关注中
  scope      TEXT DEFAULT 'watch',   -- watch=正式日历 / archive=存档（不进日历）
  market     TEXT DEFAULT 'A股',     -- 市场视野：A股 / 港股 / 外资（默认A股）
  amount_yi  REAL,                   -- 金额（亿元，数值化，供水位图）
  amount     TEXT,                   -- 金额描述
  sector     TEXT,
  sources    TEXT DEFAULT '[]',      -- JSON: [{name,org,url,role}]
  timeline   TEXT DEFAULT '[]',      -- JSON: [{date,label}]
  watch      TEXT DEFAULT '[]',      -- JSON: [{date,label}]
  notes      TEXT,
  evidence   TEXT DEFAULT '[]',      -- 支撑该事件的电报id列表
  updated_at TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS meta(
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE INDEX IF NOT EXISTS idx_tel_status ON telegrams(status);
CREATE INDEX IF NOT EXISTS idx_tel_date ON telegrams(date);
CREATE INDEX IF NOT EXISTS idx_ev_key ON events(key);
CREATE INDEX IF NOT EXISTS idx_ev_date ON events(signal_date);
"""


def dbconn():
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = dbconn()
    conn.executescript(SCHEMA)
    # 旧库列补齐（schema 漂移防护：随版本演进给存量库 ALTER 补列）
    cols_t = {r[1] for r in conn.execute("PRAGMA table_info(telegrams)")}
    if "fclass" not in cols_t:
        conn.execute("ALTER TABLE telegrams ADD COLUMN fclass TEXT DEFAULT 'mid'")
    cols_e = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if "scope" not in cols_e:
        conn.execute("ALTER TABLE events ADD COLUMN scope TEXT DEFAULT 'watch'")
    if "market" not in cols_e:
        conn.execute("ALTER TABLE events ADD COLUMN market TEXT DEFAULT 'A股'")
    conn.commit()
    conn.close()


def meta_get(k, default=None):
    conn = dbconn()
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    conn.close()
    return r["v"] if r else default


def meta_set(k, v):
    conn = dbconn()
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    conn.commit()
    conn.close()


# ============ 抓取层（只拉取，不做文字规则判断） ============
def _tel_hash(title, content, ts):
    return hashlib.md5(f"{ts}|{title}|{(content or '')[:200]}".encode("utf-8")).hexdigest()


def ingest_telegrams(items, source):
    """items: [{t, ti, c, id}] → 按hash去重入库。返回新增条数。"""
    global _TEL_TS_CACHE
    conn = dbconn()
    n_new = 0
    try:
        rows = []
        for it in items:
            ts = int(it.get("t") or 0)
            if ts <= 0:
                continue
            title = (it.get("ti") or "").strip()
            content = (it.get("c") or "").strip()
            if not title and not content:
                continue
            d = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            # 入库即预筛：噪音（P4/DIGEST）→ filtered（hi=高确定性噪音族 / mid=规则未击中），
            # 硬候选保持 pending 留给 LLM 判读。与离线 prescreen/回补脚本同一套规则。
            status, fclass = "pending", "mid"
            if radar_prescreen is not None:
                try:
                    text = f"{title} {content}"
                    amount_yi, _raw = radar_prescreen.extract_amount(text)
                    tier = radar_prescreen.classify(text, amount_yi)
                    if tier in radar_prescreen.FILTER_OUT_TIERS:
                        fam = radar_prescreen.noise_family(text)
                        status, fclass = "filtered", ("hi" if fam else "mid")
                except Exception:
                    pass          # 预筛异常不阻断入库，该条按 pending 走 LLM 兜底
            rows.append((_tel_hash(title, content, ts), ts, d, title, content[:2000], source,
                         status, fclass, dt.datetime.now().isoformat(timespec="seconds")))
        conn.executemany(
            "INSERT OR IGNORE INTO telegrams(hash,ts,date,title,content,source,status,fclass,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            rows)
        conn.commit()
        n_new = conn.total_changes
    except Exception:
        _ERR("[ingest] %s 入库异常: %s", source, traceback.format_exc())
        raise
    finally:
        conn.close()
    if n_new:
        _TEL_TS_CACHE = None   # 新电报入库 → sigTime 缓存失效，事件"具体时间"即时可见
    return n_new


def scan_cls_live():
    """财联社实时电报（最新一屏）→ 入库"""
    try:
        df = ak.stock_info_global_cls()
        if df is None or df.empty:
            return 0
        items = []
        for _, r in df.iterrows():
            ti = str(r.get("标题") or "").strip()
            c = str(r.get("内容") or "").strip()
            try:
                t = dt.datetime.strptime(f"{r.get('发布日期')} {r.get('发布时间')}", "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                t = dt.datetime.now().timestamp()
            items.append({"t": t, "ti": ti, "c": c, "id": ""})
        return ingest_telegrams(items, "cls_live")
    except Exception:
        _ERR("[cls_live] 抓取失败: %s", traceback.format_exc())
        return 0


def scan_cls_hist():
    """历史存档（cls_backfill.jsonl）→ 入库。文件未变化则跳过，避免反复全量扫描。"""
    if not HIST_FILE.exists():
        return 0
    mtime = str(HIST_FILE.stat().st_mtime_ns)
    size = str(HIST_FILE.stat().st_size)
    if meta_get("hist_mtime") == mtime and meta_get("hist_size") == size:
        return 0
    items = []
    try:
        with HIST_FILE.open(encoding="utf-8") as f:
            for line in f:
                try:
                    it = json.loads(line)
                except Exception:
                    continue
                items.append({"t": it.get("t") or 0, "ti": it.get("ti") or "", "c": it.get("c") or "",
                              "id": it.get("id") or ""})
        n = ingest_telegrams(items, "cls_hist")
        meta_set("hist_mtime", mtime)
        meta_set("hist_size", size)
        return n
    except Exception:
        _ERR("[cls_hist] 入库失败: %s", traceback.format_exc())
        return 0


def _within_ttl(key):
    last = meta_get("last_fetch_" + key)
    if not last:
        return False
    try:
        return dt.datetime.now() - dt.datetime.fromisoformat(last) < dt.timedelta(hours=TTL_HOURS[key])
    except Exception:
        return False


def ensure_fetch():
    """TTL内不重复抓源站；过期才拉。返回各通道新增数。
    注意：cls_hist 吸入的是本地存档文件（cls_backfill.jsonl），mtime 未变即零开销，
    不受网络 TTL 限制——用户中途跑 backfill 后，下一次 scan 立刻可见。
    """
    added = {}
    with LOCK:
        if not _within_ttl("cls_live"):
            added["cls_live"] = scan_cls_live()
            meta_set("last_fetch_cls_live", dt.datetime.now().isoformat(timespec="seconds"))
        added["cls_hist"] = scan_cls_hist()
        if not _within_ttl("cls_hist"):
            meta_set("last_fetch_cls_hist", dt.datetime.now().isoformat(timespec="seconds"))
    for k, v in added.items():
        _LOG("[fetch] %s 新增 %d 条", k, v)
    return added


# ============ 事件表 → 页面结构 ============
def _j(v, dft=None):
    try:
        return json.loads(v) if v else (dft or [])
    except Exception:
        return dft or []


_TEL_TS_CACHE = None
def _tel_ts_map():
    """电报 id → 财联社发布时间戳 的映射（首次查询后缓存）。"""
    global _TEL_TS_CACHE
    if _TEL_TS_CACHE is None:
        conn = dbconn()
        _TEL_TS_CACHE = {r["id"]: r["ts"] for r in conn.execute(
            "SELECT id, ts FROM telegrams WHERE ts IS NOT NULL AND ts > 0")}
        conn.close()
    return _TEL_TS_CACHE


def _sig_time(ev_ids):
    """用 evidence(电报id) 合并出该事件的财联社具体时间（取最早一条的精确时刻）。"""
    m = _tel_ts_map()
    ts_list = [m[i] for i in (ev_ids or []) if i in m]
    if not ts_list:
        return ""
    return dt.datetime.fromtimestamp(min(ts_list)).strftime("%Y-%m-%d %H:%M")


def _event_row_to_api(r):
    return {
        "id": r["event_id"],
        "key": r["key"],
        "name": r["name"],
        "category": r["category"],
        "tags": _j(r["tags"]),
        "importance": r["importance"],
        "level": r["level"],
        "signalDate": r["signal_date"],
        "landDate": r["land_date"],
        "market": r["market"] or "A股",
        "status": r["status"],
        "amount": r["amount"],
        "amountYi": r["amount_yi"],
        "sector": r["sector"],
        "sources": _j(r["sources"]),
        "timeline": _j(r["timeline"]),
        "watch": _j(r["watch"]),
        "notes": r["notes"],
        "evidence": _j(r["evidence"]),
        "sigTime": _sig_time(_j(r["evidence"])),   # 财联社具体时间（evidence id 合并电报 ts）
        "auto": True,     # LLM判读结果直接进正式日历，免人工点击
        "ai": True,
        "cand": False,
    }


_PROG_CACHE = {"at": 0.0, "val": None}
def progress():
    """判读进度（3 秒内存缓存：一次 snapshot 渲染会连查多次）。"""
    now = dt.datetime.now().timestamp()
    if _PROG_CACHE["val"] is not None and now - _PROG_CACHE["at"] < 3:
        return _PROG_CACHE["val"]
    conn = dbconn()
    total = conn.execute("SELECT COUNT(*) c FROM telegrams").fetchone()["c"]
    judged = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='judged'").fetchone()["c"]
    dropped = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='dropped'").fetchone()["c"]
    filtered = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='filtered'").fetchone()["c"]
    ev = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    pending_by_date = [dict(r) for r in conn.execute(
        "SELECT date, COUNT(*) c FROM telegrams WHERE status='pending' GROUP BY date ORDER BY date DESC LIMIT 30")]
    # 按日消化统计：t=当日电报总数 j=语义判建事件 d=语义判丢弃 f=预筛粗滤 fh=高确定性噪音直击(可信排泄)
    # 消化率 = (j+d+fh)/t —— 手工抽检误滤率≈1%，直击规则族的可信过滤计入"处理完成"；
    # 其余 mid 粗滤仍算"只吃了没消化"，保留红色作质检方向标
    digest_by_date = [dict(r) for r in conn.execute(
        "SELECT date, COUNT(*) t,"
        " SUM(CASE WHEN status='judged' THEN 1 ELSE 0 END) j,"
        " SUM(CASE WHEN status='dropped' THEN 1 ELSE 0 END) d,"
        " SUM(CASE WHEN status='filtered' THEN 1 ELSE 0 END) f,"
        " SUM(CASE WHEN status='filtered' AND IFNULL(fclass,'mid')='hi' THEN 1 ELSE 0 END) fh,"
        " SUM(CASE WHEN status='pending' OR status IS NULL OR status='' THEN 1 ELSE 0 END) p"
        " FROM telegrams GROUP BY date ORDER BY date DESC")]
    filtered_hi = conn.execute(
        "SELECT COUNT(*) c FROM telegrams WHERE status='filtered' AND IFNULL(fclass,'mid')='hi'").fetchone()["c"]
    conn.close()
    out = {
        "total": total, "judged": judged, "dropped": dropped, "filtered": filtered,
        "pending": total - judged - dropped - filtered,
        "events": ev, "pendingByDate": pending_by_date,
        "digestByDate": digest_by_date, "filteredHi": filtered_hi,
        "lastJudgeAt": meta_get("last_judge_at", ""),
        "lastFilterAt": meta_get("last_filter_at", ""),
        "lastFetchAt": meta_get("last_fetch_cls_live", ""),
    }
    _PROG_CACHE["at"] = now
    _PROG_CACHE["val"] = out
    return out


def all_events(tag=None, status=None, scope="watch"):
    """scope: watch(默认，正式日历) / all(全部含存档) / archive(仅存档)"""
    conn = dbconn()
    sql = "SELECT * FROM events"
    where, args = [], []
    if tag:
        where.append("tags LIKE ?")
        args.append(f'%"{tag}"%')
    if status:
        where.append("status=?")
        args.append(status)
    if scope == "watch":
        where.append("(scope='watch' OR scope IS NULL)")
    elif scope == "archive":
        where.append("(scope='archive')")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY signal_date DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [_event_row_to_api(r) for r in rows]





# ============ 前端数据快照：data.json 是网页唯一数据权威 ============
# 数据流：db(扫描/判读) -> 后端写 data.json -> 网页 fetch /api/snapshot 渲染
# 后端负责更新文件；网页只读。localStorage 已降级为离线缓存，不再是权威源。
DATA_FILE = HERE / "data.json"
JUDGE_DIR = HERE / "judge_queue"


def pending_count():
    conn = dbconn()
    n = conn.execute("SELECT COUNT(*) c FROM telegrams WHERE status='pending'").fetchone()["c"]
    conn.close()
    return n


def dump_judge_batch():
    """把当前 pending 电报导出为判读批次文件（与 annotate.py dump 相同格式）。
    半自动闭环：断点更新发现新增待判时自动出包，AI 会话直接照包判读 → apply 落库。"""
    try:
        if not JUDGE_DIR.exists():
            JUDGE_DIR.mkdir()
        out = JUDGE_DIR / f"batch_auto_{dt.date.today().isoformat()}.jsonl"
        conn = dbconn()
        rows = conn.execute(
            "SELECT id,date,title,content FROM telegrams WHERE status='pending' ORDER BY id").fetchall()
        conn.close()
        if not rows:
            return None
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                content = (r["content"] or "")[:300] + ("…[截断]" if len(r["content"] or "") > 300 else "")
                f.write(json.dumps({"id": r["id"], "date": r["date"], "title": r["title"],
                                    "content": content}, ensure_ascii=False) + "\n")
        _LOG("[judge] 自动出判读包 %d 条 → %s", len(rows), out.name)
        return out
    except Exception:
        _ERR("[judge] 判读包导出失败: %s", traceback.format_exc())
        return None


def today_iso():
    return dt.date.today().isoformat()


def load_snapshot():
    """读 data.json；不存在或损坏则从 db 初始化并落盘。保证结构完整。
    正常读取不写盘（避免读一次写一次导致 data.json 反复闪动/updated 每次变化）；
    仅当文件缺失、损坏或结构不完整时才重建落盘。"""
    try:
        if DATA_FILE.exists():
            with DATA_FILE.open(encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("events"), list):
                if not isinstance(d.get("cands"), list):
                    d["cands"] = []
                d.setdefault("auto", True)
                d["progress"] = progress()
                d["updated"] = dt.datetime.now().isoformat(timespec="seconds")
                return d
    except Exception:
        _ERR("[snapshot] data.json 读取失败/损坏，将从 db 重建: %s", traceback.format_exc())
        traceback.print_exc()
    snap = {
        "events": all_events(scope="watch"),
        "cands": [],
        "auto": True,
        "progress": progress(),
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    save_snapshot(snap)
    return snap


def save_snapshot(snap):
    """加锁原子写 data.json（写临时文件再替换，避免半写）。
    落盘前按日留档：每天第一笔写盘前先把当前 data.json 备为 data.json.bak-YYYY-MM-DD，保留最近 5 代。"""
    tmp = DATA_FILE.with_suffix(".tmp")
    with LOCK:
        try:
            today = dt.date.today().isoformat()
            bak = DATA_FILE.with_name(f"data.json.bak-{today}")
            if DATA_FILE.exists() and not bak.exists():
                shutil.copy2(DATA_FILE, bak)
            dated = sorted(p for p in DATA_FILE.parent.glob("data.json.bak-*")
                           if re.fullmatch(r"data\.json\.bak-\d{4}-\d{2}-\d{2}", p.name))
            for old in dated[:-5]:
                old.unlink()
        except Exception:
            _ERR("[backup] data.json 备份轮转失败: %s", traceback.format_exc())
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        tmp.replace(DATA_FILE)


def mutate_state(payload):
    """前端所有写操作统一入口：以 payload 的 events/cands/auto 为最终状态落盘。
    action 仅 scan 需要后端额外合并 db 正式事件；其余 confirm/drop/add/import/reset
    都由前端先在内存完成结构变更，后端只负责持久化，避免重复处理。
    """
    snap = load_snapshot()
    if isinstance(payload.get("events"), list):
        snap["events"] = payload["events"]
    if isinstance(payload.get("cands"), list):
        snap["cands"] = payload["cands"]
    if isinstance(payload.get("auto"), bool):
        snap["auto"] = payload["auto"]

    action = payload.get("action")
    info = {"ok": True, "action": action}

    if action == "scan":
        today = today_iso()
        # 摘除本地全部 auto 事件 -> 整体镜像 db 正式事件（与旧前端 pullRadar 语义一致，避免陈旧副本）
        snap["events"] = [e for e in snap["events"] if not e.get("auto")]
        known_ids = {e["id"] for e in snap["events"]}
        known_keys = {e.get("key") for e in snap["events"] if e.get("key")}
        have_c = {c["id"] for c in snap["cands"]}
        added = ensure_fetch()
        fresh = all_events(scope="watch")
        new_ev = new_cand = 0
        for c in fresh:
            if c["id"] in known_ids or (c.get("key") and c["key"] in known_keys):
                continue
            known_ids.add(c["id"])
            if c.get("key"):
                known_keys.add(c["key"])
            if c.get("auto"):
                e = dict(c); e.pop("cand", None)
                if e.get("landDate") and e["landDate"] > today and e.get("status") not in ("已落地", "已失效/辟谣"):
                    e["status"] = "待落地"
                snap["events"].append(e); new_ev += 1
            elif c["id"] not in have_c:
                snap["cands"].append(c); have_c.add(c["id"]); new_cand += 1
        info["added"] = added
        info["fresh"] = len(fresh)
        info["newEvents"] = new_ev
        info["newCands"] = new_cand
        # 判读半自动闭环：有待判电报时自动出包，页面提示包名
        pend_now = pending_count()
        if pend_now > 0:
            jp = dump_judge_batch()
            if jp:
                info["judgeBatch"] = jp.name
                info["pending"] = pend_now

    snap["progress"] = progress()
    snap["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    save_snapshot(snap)
    info["snapshot"] = snap
    return info



# ============ HTTP ============
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?")[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        q[k] = v
            if path == "/api/scan":
                added = ensure_fetch()
                body = json.dumps({
                    "ok": True, "time": dt.datetime.now().isoformat(timespec="seconds"),
                    "added": added, "progress": progress(),
                    "candidates": all_events(scope="watch"),
                }, ensure_ascii=False).encode("utf-8")
                self._send(200, body)
            elif path == "/api/snapshot":
                self._send(200, json.dumps(load_snapshot(), ensure_ascii=False).encode("utf-8"))
            elif path == "/api/progress":
                self._send(200, json.dumps(progress(), ensure_ascii=False).encode("utf-8"))
            elif path == "/api/pending":
                conn = dbconn()
                sql = "SELECT id,date,title,content FROM telegrams WHERE status='pending'"
                args = []
                if q.get("date"):
                    sql += " AND date=?"
                    args.append(q["date"])
                sql += " ORDER BY date DESC, id LIMIT ?"
                args.append(int(q.get("limit") or 100))
                rows = [dict(r) for r in conn.execute(sql, args)]
                conn.close()
                self._send(200, json.dumps({"pending": rows}, ensure_ascii=False).encode("utf-8"))
            elif path == "/api/events":
                self._send(200, json.dumps({"events": all_events(q.get("tag"), q.get("status"), q.get("scope", "watch"))},
                                           ensure_ascii=False).encode("utf-8"))
            elif path == "/api/ping":
                self._send(200, json.dumps({"ok": True, "port": PORT}).encode())
            else:
                msg = ("事件雷达 v3 语义引擎运行中。\n"
                       "GET /api/scan       网页数据（candidates+progress）\n"
                       "GET /api/progress   判读进度\n"
                       "GET /api/pending    待判电报（?date=YYYY-MM-DD&limit=N）\n"
                       "GET /api/events     事件表（?tag=IPO&status=待落地）\n"
                       "GET /api/ping       存活检测").encode("utf-8")
                self._send(200, msg, "text/plain; charset=utf-8")
        except Exception:
            _ERR("[GET] %s 处理异常: %s", self.path, traceback.format_exc())
            traceback.print_exc()
            self._send(500, json.dumps({"ok": False, "error": "server error"}, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            if path != "/api/mutate":
                self._send(404, json.dumps({"ok": False, "error": "not found"}, ensure_ascii=False).encode("utf-8"))
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            info = mutate_state(payload)
            self._send(200, json.dumps(info, ensure_ascii=False).encode("utf-8"))
        except Exception:
            _ERR("[POST] %s 处理异常: %s", self.path, traceback.format_exc())
            traceback.print_exc()
            self._send(500, json.dumps({"ok": False, "error": "server error"}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    init_db()
    _LOG("事件雷达 v3（语义引擎）启动：http://127.0.0.1:%d", PORT)
    print(f"事件雷达 v3（语义引擎）启动：http://127.0.0.1:{PORT}")
    print("抓取层：财联社电报实时+历史存档 → telegrams 表（hash去重，无文字规则）")
    print("判读层：LLM 逐批语义判读（annotate.py dump/apply），进度见 /api/progress")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
