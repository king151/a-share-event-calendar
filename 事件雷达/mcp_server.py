# -*- coding: utf-8 -*-
"""
A股大事件日历 · 事件雷达 MCP Server（零依赖手写协议层 + 零依赖数据层）

用途（2026-10-27 优化报告 P0-1）：
  把雷达的事件数据 + 判读流水线统计暴露为 MCP 工具，让 Codex/Cursor/Claude 等
  Agent 直接查询结构化事件流——卡位 Agent 数据层，不新增任何 pip 依赖。

零依赖两层（2026-10-27 新用户实测改造）：
  · 协议层手写 stdio JSON-RPC 2.0（不装 mcp 库）
  · 数据层不再 import radar_server（它顶层 import akshare 会把依赖传染给 MCP-only 用户）
    改为直接 sqlite3 只读读 radar.db；radar.db 不存在（新用户还没跑雷达）时
    自动回退 docs/demo.json 的 185 条示例事件，并带 mode 提示引导跑雷达。

协议：MCP stdio transport = JSON-RPC 2.0 消息，UTF-8，按行分隔（每行一条消息）。

工具（4 个）：
  query_events  查事件（tag/status/market/keyword/scope 过滤，默认正式日历 watch）
  get_event     事件详情（timeline/watch/sources 全量）
  judge_stats   判读流水线统计（总数/已判/丢弃/粗滤/待判/事件数/当日消化率；demo 模式给出引导）
  month_view    按月聚合事件（by=land 落地日 / signal 披露日）

注册进宿主（示例，Claude Desktop / 其他 MCP 客户端配置文件）：
  {
    "mcpServers": {
      "a-share-event-calendar": {
        "command": "pythonw",
        "args": ["<本仓库路径>/事件雷达/mcp_server.py"]
      }
    }
  }

口径：所有输出尾部带"仅供研究参考，不构成投资建议"。
日志：mcp.log（stdio 被协议占用，任何报错都走文件留痕）。
"""
import json
import sys
import sqlite3
import traceback
import logging
import datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent

# stdio 被协议独占：编码统一 UTF-8，print 一律禁用（报错进日志文件）
_logger = logging.getLogger("mcp")
_logger.setLevel(logging.INFO)
_h = logging.FileHandler(HERE / "mcp.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_logger.addHandler(_h)
_ERR = _logger.error
_LOG = _logger.info

SERVER_NAME = "a-share-event-calendar-radar"
SERVER_VERSION = "1.1.0"
DISCLAIMER = "数据仅供研究参考，不构成投资建议。来源：财联社电报 · LLM判读 · 开源项目 github.com/king151/a-share-event-calendar"
DB_FILE = HERE / "radar.db"
DEMO_FILE = HERE.parent / "docs" / "demo.json"

tools_SCHEMA = [
    {
        "name": "query_events",
        "description": "查询 A股大事件日历的结构化事件（财联社电报经 LLM 判读生成，含披露日/落地日/金额/标签/状态）。默认只返回正式日历（watch）事件；未运行过雷达时自动使用仓库内置示例事件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "标签过滤，如 IPO/并购重组/大额合同/政策法规"},
                "status": {"type": "string", "description": "状态过滤：待落地 / 已落地 / 关注中 / 已失效/辟谣"},
                "market": {"type": "string", "description": "市场视野：A股（默认）/ 港股 / 外资"},
                "keyword": {"type": "string", "description": "名称/备注关键词模糊匹配（如 中报/重组/Anthropic）"},
                "scope": {"type": "string", "description": "watch=正式日历（默认）/ all=含存档 / archive=仅存档"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 30，最大 200"},
            },
        },
    },
    {
        "name": "get_event",
        "description": "按事件 id 取单事件全量详情（含时间线、关注点 watch、信源 sources）。",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string", "description": "事件 id（query_events 返回的 id 字段）"}},
        },
    },
    {
        "name": "judge_stats",
        "description": "事件雷达判读流水线统计：电报总数/已判/丢弃/粗滤/待判/事件数，以及逐日消化率漏斗（最新一天）。未运行过雷达时返回 demo 模式说明。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "month_view",
        "description": "按月聚合事件：给一个月份（YYYY-MM），返回按日分组的事件清单。by=land 按落地日聚合（默认），by=signal 按披露日聚合。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM，留空取当前月"},
                "by": {"type": "string", "description": "land（落地日，默认）/ signal（披露日）"},
                "scope": {"type": "string", "description": "watch（默认）/ all"},
            },
        },
    },
]


# ============ 数据层（零依赖：sqlite3 直读 / demo.json 回退） ============
_DEMO_CACHE = None
def _demo_events():
    """docs/demo.json → 事件列表（首次解析后缓存）。文件缺失返回 None。"""
    global _DEMO_CACHE
    if _DEMO_CACHE is None:
        if not DEMO_FILE.exists():
            return None
        with DEMO_FILE.open(encoding="utf-8") as f:
            d = json.load(f)
        evs = d.get("events") if isinstance(d, dict) else d
        _DEMO_CACHE = evs if isinstance(evs, list) else []
    return _DEMO_CACHE


_TEL_TS_CACHE = None
def _tel_ts_map(conn):
    """电报 id → 发布时间戳（db 模式补 sigTime 用，进程内缓存）。"""
    global _TEL_TS_CACHE
    if _TEL_TS_CACHE is None:
        _TEL_TS_CACHE = {r[0]: r[1] for r in conn.execute(
            "SELECT id, ts FROM telegrams WHERE ts IS NOT NULL AND ts > 0")}
    return _TEL_TS_CACHE


def _j(v, dft=None):
    try:
        return json.loads(v) if v else (dft or [])
    except Exception:
        return dft or []


def _row_to_api(r, conn=None):
    ev = {
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
    }
    if conn is not None:
        ev.update({
            "sources": _j(r["sources"]), "timeline": _j(r["timeline"]),
            "watch": _j(r["watch"]), "notes": r["notes"], "evidence": _j(r["evidence"]),
        })
        ts_list = [_tel_ts_map(conn)[i] for i in _j(r["evidence"]) if i in _tel_ts_map(conn)]
        if ts_list:
            ev["sigTime"] = dt.datetime.fromtimestamp(min(ts_list)).strftime("%Y-%m-%d %H:%M")
    else:
        for k, dft in (("sources", []), ("timeline", []), ("watch", []), ("evidence", [])):
            ev[k] = r.get(k) or dft
        ev["notes"] = r.get("notes")
        ev["sigTime"] = r.get("sigTime")
    return ev


def load_events(scope="watch", tag=None, status=None, market=None, keyword=None, limit=200):
    """统一读接口：db 优先（全量实时），radar.db 不存在回退 demo.json（新用户首体验）。"""
    if DB_FILE.exists():
        conn = sqlite3.connect(str(DB_FILE), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT * FROM events"
            where, args = [], []
            if tag:
                where.append("tags LIKE ?"); args.append('%"' + tag + '"%')
            if status:
                where.append("status=?"); args.append(status)
            if market:
                where.append("(market=? OR (market IS NULL AND ?='A股'))"); args.extend([market, market])
            if keyword:
                where.append("(name LIKE ? OR notes LIKE ?)"); args.extend([f"%{keyword}%", f"%{keyword}%"])
            if scope == "watch":
                where.append("(scope='watch' OR scope IS NULL)")
            elif scope == "archive":
                where.append("(scope='archive')")
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY signal_date DESC LIMIT ?"
            args.append(max(1, min(int(limit or 200), 200)))
            rows = conn.execute(sql, args).fetchall()
            return "db", rows and [_row_to_api(r, conn) for r in rows]
        finally:
            conn.close()
    evs = _demo_events()
    if evs is None:
        return "none", None
    out = []
    for e in evs:
        if tag and tag not in (e.get("tags") or []):
            continue
        if status and e.get("status") != status:
            continue
        if market and (e.get("market") or "A股") != market:
            continue
        if keyword and keyword not in (e.get("name") or "") and keyword not in (e.get("notes") or ""):
            continue
        out.append(e)
        if len(out) >= max(1, min(int(limit or 200), 200)):
            break
    out.sort(key=lambda e: (e.get("signalDate") or ""), reverse=True)
    return "demo", out


def tool_query_events(args):
    scope = args.get("scope") or "watch"
    try:
        limit = max(1, min(int(args.get("limit") or 30), 200))
    except Exception:
        limit = 30
    if scope == "all":
        mode, evs = load_events("watch", tag=args.get("tag"), status=args.get("status"),
                                market=args.get("market"), keyword=args.get("keyword"), limit=200)
        mode2, evs2 = load_events("archive", tag=args.get("tag"), status=args.get("status"),
                                  market=args.get("market"), keyword=args.get("keyword"), limit=200)
        if evs is None and evs2 is not None:
            mode, evs = mode2, evs2
        elif evs2 is not None and evs is not None:
            evs = evs + evs2
            evs.sort(key=lambda e: (e.get("signalDate") or ""), reverse=True)
    else:
        mode, evs = load_events(scope, tag=args.get("tag"), status=args.get("status"),
                                market=args.get("market"), keyword=args.get("keyword"), limit=5000)
    if evs is None:
        return {"mode": "empty", "error": "未找到数据：本机还没有 radar.db，且仓库里也没有 docs/demo.json。"
                "请先运行 事件雷达/启动雷达.bat 启动雷达生成数据。", "disclaimer": DISCLAIMER}
    evs = evs[:limit]
    return {"count": len(evs), "returned": len(evs),
            "mode": mode,
            "note": "全量实时数据" if mode == "db" else
                    "示例事件（docs/demo.json）——未检测到 radar.db，运行 事件雷达/启动雷达.bat 后获得全量+实时",
            "events": [_compact(e, mode) for e in evs], "disclaimer": DISCLAIMER}


def _find_event(eid):
    for sc in ("watch", "archive"):
        mode, evs = load_events(sc)
        for e in evs or []:
            if e.get("id") == eid:
                return mode, e
    return None, None


def tool_get_event(args):
    eid = str(args.get("id") or "")
    if not eid:
        return {"error": "缺少 id 参数", "disclaimer": DISCLAIMER}
    mode, e = _find_event(eid)
    if e is None:
        return {"error": "未找到事件 " + eid, "disclaimer": DISCLAIMER}
    if mode == "demo":
        e["note"] = "示例事件（docs/demo.json）——运行 事件雷达/启动雷达.bat 后获得全量+实时"
    e["disclaimer"] = DISCLAIMER
    return e


def tool_judge_stats(args):
    if not DB_FILE.exists():
        return {"mode": "demo",
                "message": "radar.db 不存在（雷达从未在本机运行）——当前只能提供 " + str(len(_demo_events() or [])) +
                           " 条示例事件（docs/demo.json）。启动 事件雷达/启动雷达.bat 后，本工具返回全量电报/事件/判读流水线统计。",
                "events": len(_demo_events() or []), "disclaimer": DISCLAIMER}
    conn = sqlite3.connect(str(DB_FILE), timeout=10)
    try:
        q = lambda s: conn.execute(s).fetchone()[0]
        out = {"mode": "db",
               "total": q("SELECT COUNT(*) FROM telegrams"),
               "judged": q("SELECT COUNT(*) FROM telegrams WHERE status='judged'"),
               "dropped": q("SELECT COUNT(*) FROM telegrams WHERE status='dropped'"),
               "filtered": q("SELECT COUNT(*) FROM telegrams WHERE status='filtered'"),
               "events": q("SELECT COUNT(*) FROM events")}
        out["pending"] = out["total"] - out["judged"] - out["dropped"] - out["filtered"]
        r = conn.execute(
            "SELECT date, COUNT(*) t,"
            " SUM(CASE WHEN status='judged' THEN 1 ELSE 0 END) j,"
            " SUM(CASE WHEN status='dropped' THEN 1 ELSE 0 END) d,"
            " SUM(CASE WHEN status='filtered' AND IFNULL(fclass,'mid')='hi' THEN 1 ELSE 0 END) fh"
            " FROM telegrams GROUP BY date ORDER BY date DESC").fetchone()
        if r:
            t = int(r[1] or 0)
            out["latestDate"], out["latestDigestRate"] = r[0], (
                round((int(r[2] or 0) + int(r[3] or 0) + int(r[4] or 0)) / t, 3) if t else None)
        return out
    finally:
        conn.close()


def tool_month_view(args):
    month = args.get("month") or dt.date.today().strftime("%Y-%m")
    by = "signal" if str(args.get("by") or "land").lower().startswith("sig") else "land"
    scope = args.get("scope") or "watch"
    if scope == "all":
        mode, evs = load_events("watch")
        _m2, evs2 = load_events("archive")
        if evs2:
            evs = (evs or []) + evs2
    else:
        mode, evs = load_events(scope)
    if evs is None:
        return {"mode": "empty", "error": "未找到数据：请先运行 事件雷达/启动雷达.bat。", "disclaimer": DISCLAIMER}
    days = {}
    for e in evs:
        d = (e.get("landDate") if by == "land" else e.get("signalDate")) or ""
        if not d or not d.startswith(month):
            continue
        days.setdefault(d, []).append(_compact(e, mode))
    return {"month": month, "by": by, "mode": mode, "count": sum(len(v) for v in days.values()),
            "days": {k: days[k] for k in sorted(days)}, "disclaimer": DISCLAIMER}


def _compact(ev, mode="db"):
    """列表视图瘦身：不含 timeline/watch/sources/notes 长文。"""
    if mode == "demo":     # demo 事件已是 API 形态的 dict
        return {k: ev.get(k) for k in (
            "id", "name", "category", "tags", "importance", "level",
            "signalDate", "landDate", "sigTime", "status", "market",
            "amount", "amountYi", "sector")}
    return {k: ev.get(k) for k in (
        "id", "name", "category", "tags", "importance", "level",
        "signalDate", "landDate", "sigTime", "status", "market",
        "amount", "amountYi", "sector")}


TOOLS = {
    "query_events": tool_query_events,
    "get_event": tool_get_event,
    "judge_stats": tool_judge_stats,
    "month_view": tool_month_view,
}


def handle(msg):
    """单条 JSON-RPC 消息 → 回包 dict（通知返回 None）。"""
    method = msg.get("method", "")
    mid = msg.get("id")
    is_req = mid is not None
    try:
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion")
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": pv or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "A股大事件日历（开源）· 追踪'说好的大事落地了吗'。"
                                "4 个工具：query_events/get_event/judge_stats/month_view。"
                                "radar.db 不存在时自动提供 docs/demo.json 示例数据。" + DISCLAIMER}}
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools_SCHEMA}}
        if method == "tools/call":
            name = (msg.get("params") or {}).get("name", "")
            fn = TOOLS.get(name)
            if fn is None:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "tool not found: " + name}}
            try:
                data = fn((msg.get("params") or {}).get("arguments") or {})
            except Exception:
                _ERR("tool %s 执行异常: %s", name, traceback.format_exc())
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "tool error: " + name}], "isError": True}}
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}}
        if is_req:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "method not found: " + method}}
        return None
    except Exception:
        _ERR("消息处理异常 %s: %s", method, traceback.format_exc())
        if is_req:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "internal error"}}
        return None


def main():
    _LOG("MCP server 启动（stdio，按行 JSON-RPC；零依赖数据层 db/demo 回退）")
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    buf = sys.stdin.buffer
    out = sys.stdout.buffer
    for raw in buf:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            _ERR("非 JSON 行（%d 字节）已忽略：%.80r", len(line), line)
            continue
        reply = handle(msg)
        if reply is not None:
            try:
                out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
                out.flush()
            except Exception:
                _ERR("回包写出失败（宿主已断开？）: %s", traceback.format_exc())
                return
        else:
            _LOG("通知/无回包: %s", (msg.get("method") or "?"))
    _LOG("stdin EOF，退出")


if __name__ == "__main__":
    main()
