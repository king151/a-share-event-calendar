# -*- coding: utf-8 -*-
"""
A股大事件日历 · 事件雷达 MCP Server（零依赖手写协议层）

用途（2026-10-27 优化报告 P0-1）：
  把雷达的事件数据 + 判读流水线统计暴露为 MCP 工具，让 Codex/Cursor/Claude 等
  Agent 直接查询结构化事件流——卡位 Agent 数据层，不新增任何 pip 依赖。

协议：MCP stdio transport = JSON-RPC 2.0 消息，UTF-8，按行分隔（每行一条消息）。
握手：client initialize → server 回协议版本+能力；tools/list；tools/call。

工具（4 个）：
  query_events  查事件（tag/status/market/keyword/scope 过滤，默认正式日历 watch）
  get_event     事件详情（timeline/watch/sources 全量）
  judge_stats   判读流水线统计（总数/已判/丢弃/粗滤/待判/事件数/当日消化率）
  month_view    按月聚合事件（by=land 落地日 / signal 披露日）

注册进宿主（示例，Claude Desktop / 其他 MCP 客户端配置文件）：
  {
    "mcpServers": {
      "a-share-event-calendar": {
        "command": "D:/Python310/pythonw.exe",
        "args": ["F:/随心/事件雷达/mcp_server.py"]
      }
    }
  }

口径：所有输出尾部带"仅供研究参考，不构成投资建议"。
日志：mcp.log（stdio 被协议占用，任何报错都走文件留痕）。
"""
import json
import sys
import io
import traceback
import logging
import datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# stdio 被协议独占：编码统一 UTF-8，print 一律禁用（报错进日志文件）
_logger = logging.getLogger("mcp")
_logger.setLevel(logging.INFO)
_h = logging.FileHandler(HERE / "mcp.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_logger.addHandler(_h)
_ERR = _logger.error
_LOG = _logger.info

SERVER_NAME = "a-share-event-calendar-radar"
SERVER_VERSION = "1.0.0"
DISCLAIMER = "数据仅供研究参考，不构成投资建议。来源：财联社电报 · LLM判读 · 开源项目 github.com/king151/a-share-event-calendar"

tools_SCHEMA = [
    {
        "name": "query_events",
        "description": "查询 A股大事件日历的结构化事件（财联社电报经 LLM 判读生成，含披露日/落地日/金额/标签/状态）。默认只返回正式日历（watch）事件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "标签过滤，如 IPO/并购重组/大额合同/政策法规"},
                "status": {"type": "string", "description": "状态过滤：待落地 / 已落地 / 关注中 / 已失效/辟谣"},
                "market": {"type": "string", "description": "市场视野：A股（默认）/ 港股 / 外资"},
                "keyword": {"type": "string", "description": "名称关键词模糊匹配（如 中报/重组/Anthropic）"},
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
        "description": "事件雷达判读流水线统计：电报总数/已判/丢弃/粗滤/待判/事件数，以及逐日消化率漏斗（最新一天）。",
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


def _compact(ev):
    """列表视图瘦身：不含 timeline/watch/sources/notes 长文。"""
    out = {k: ev.get(k) for k in (
        "id", "name", "category", "tags", "importance", "level",
        "signalDate", "landDate", "sigTime", "status", "market",
        "amount", "amountYi", "sector")}
    return out


def tool_query_events(args):
    import radar_server as rs
    scope = args.get("scope") or "watch"
    evs = rs.all_events(args.get("tag"), args.get("status"), scope)
    market = args.get("market")
    kw = (args.get("keyword") or "").strip()
    if market:
        evs = [e for e in evs if e.get("market") == market]
    if kw:
        evs = [e for e in evs if kw in (e.get("name") or "") or kw in (e.get("notes") or "")]
    try:
        limit = max(1, min(int(args.get("limit") or 30), 200))
    except Exception:
        limit = 30
    return {"count": len(evs), "returned": min(len(evs), limit),
            "events": [_compact(e) for e in evs[:limit]], "disclaimer": DISCLAIMER}


def tool_get_event(args):
    import radar_server as rs
    eid = str(args.get("id") or "")
    if not eid:
        return {"error": "缺少 id 参数", "disclaimer": DISCLAIMER}
    for scope in ("watch", "archive"):
        for e in rs.all_events(None, None, scope):
            if e.get("id") == eid:
                e["disclaimer"] = DISCLAIMER
                return e
    return {"error": "未找到事件 " + eid, "disclaimer": DISCLAIMER}


def tool_judge_stats(args):
    import radar_server as rs
    p = rs.progress()
    out = {k: p[k] for k in ("total", "judged", "dropped", "filtered", "pending", "events")}
    out["filteredHi"] = p.get("filteredHi")
    d = (p.get("digestByDate") or [{}])[0]
    try:
        t = int(d.get("t") or 0)
        out["latestDate"] = d.get("date")
        out["latestDigestRate"] = round((int(d.get("j") or 0) + int(d.get("d") or 0)
                                         + int(d.get("fh") or 0)) / t, 3) if t else None
    except Exception:
        pass
    out["disclaimer"] = DISCLAIMER
    return out


def tool_month_view(args):
    import radar_server as rs
    month = args.get("month") or dt.date.today().strftime("%Y-%m")
    by = "signal" if str(args.get("by") or "land").lower().startswith("sig") else "land"
    scope = args.get("scope") or "watch"
    evs = rs.all_events(None, None, scope)
    days = {}
    for e in evs:
        d = (e.get("landDate") if by == "land" else e.get("signalDate")) or ""
        if not d or not d.startswith(month):
            continue
        days.setdefault(d, []).append(_compact(e))
    return {"month": month, "by": by, "count": sum(len(v) for v in days.values()),
            "days": {k: days[k] for k in sorted(days)}, "disclaimer": DISCLAIMER}


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
                                "4 个工具：query_events/get_event/judge_stats/month_view。" + DISCLAIMER}}
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
    _LOG("MCP server 启动（stdio，按行 JSON-RPC）")
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
