import datetime
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

from mcp.server.fastmcp import FastMCP

from .config import DB_PATH, BASE_DIR
from .database import get_connection, sync_data

# 初始化 FastMCP 服务器
mcp = FastMCP("wechat-mcp")

def _check_db_initialized() -> Optional[str]:
    """检查绿泡泡本地数据库与索引是否已初始化完成"""
    if not DB_PATH.exists():
        return f"绿泡泡本地索引数据库未找到 ({DB_PATH})。请先以管理员身份在终端运行 `wechat-init` 完成解密与索引构建。"
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
        if not cur.fetchone():
            conn.close()
            return "绿泡泡数据库存在但数据表未初始化。请在终端运行 `wechat-init --etl-only` 重建索引。"
        conn.close()
        return None
    except Exception as e:
        return f"绿泡泡数据库无法访问: {e}。请尝试重新运行 `wechat-init`。"

# 微信各版本 create_time 单位不一致：大于该阈值视为毫秒时间戳，否则视为秒
_MS_THRESHOLD = 10**11


def _ts_unit(ts: int) -> int:
    """判断时间戳单位：1000=毫秒，1=秒"""
    return 1000 if ts and ts > _MS_THRESHOLD else 1


def _normalize_ts(ts: int) -> float:
    """将秒级/毫秒级时间戳统一归一化为「秒」"""
    return ts / _ts_unit(ts) if ts else ts


def _ts_to_str(ts: int) -> str:
    """时间戳转人类可读时间"""
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(_normalize_ts(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _parse_time_bound(value: str) -> datetime.datetime:
    """解析时间参数：'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'"""
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间 '{value}'，支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS]")


def _time_range_to_ts(cur: sqlite3.Cursor, start: str, end: str) -> tuple:
    """将人类可读时间范围转换为与库内 create_time 单位一致的查询区间"""
    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    unit = _ts_unit(max_ts)
    start_dt = _parse_time_bound(start)
    end_dt = _parse_time_bound(end)
    # 结束时间只给到日期时，自动覆盖当天全天
    if len((end or "").strip()) <= 10:
        end_dt += datetime.timedelta(days=1)
    if end_dt <= start_dt:
        raise ValueError("结束时间必须晚于起始时间")
    return int(start_dt.timestamp() * unit), int(end_dt.timestamp() * unit)


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符（% _ \\），使用户输入按字面量匹配"""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@mcp.tool()
def search_wechat_messages(query: str, session_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """全文搜索绿泡泡聊天记录。
    
    Args:
        query: 检索关键词（支持多词空格搜索）
        session_id: 可选，限定搜索特定的群聊（@chatroom）或联系人（绿泡泡号）
        limit: 返回数量，默认 20 条
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    # 使用 LIKE 子串匹配而非 FTS5 MATCH：内置 unicode61 分词器会把连续中文当成
    # 单个 token（搜「缓存」匹配不到「我缓存命中都降到98%」），中文召回率极低；
    # 个人数据量（十万级）下 LIKE 全表扫描为百毫秒级，此处以正确性优先。
    tokens = [t for t in (query or "").split() if t]
    if not tokens:
        conn.close()
        return []

    sql = """
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE 1=1
    """
    params: List[Any] = []
    for token in tokens:
        sql += " AND m.content LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(token)}%")
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)

    sql += " ORDER BY m.create_time DESC LIMIT ?"
    params.append(limit)
    
    rows = cur.execute(sql, params).fetchall()
    
    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "session_id": r["session_username"],
            "session_name": r["session_name"] or r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        })
    conn.close()
    return results

@mcp.tool()
def list_wechat_sessions(keyword: Optional[str] = None, session_type: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """查询绿泡泡会话列表（用于查找特定联系人或群聊的唯一 ID）。
    
    Args:
        keyword: 会话名称搜索关键字（群名/人名/备注）
        session_type: 会话类型筛选，可选：'room'（群聊）、'c2c'（单聊）、'gh'（公众号）
        limit: 返回记录数，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    sql = "SELECT username, type, display_name, message_count, last_time, last_msg_preview FROM sessions WHERE 1=1"
    params = []
    
    if keyword:
        sql += " AND (display_name LIKE ? OR username LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if session_type:
        sql += " AND type = ?"
        params.append(session_type)
        
    sql += " ORDER BY last_time DESC LIMIT ?"
    params.append(limit)
    
    rows = cur.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "session_id": r["username"],
            "display_name": r["display_name"],
            "type": r["type"],
            "message_count": r["message_count"],
            "last_time": _ts_to_str(r["last_time"]),
            "last_message": r["last_msg_preview"],
        })
    conn.close()
    return results

@mcp.tool()
def get_session_history(session_id: str, limit: int = 50, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """获取指定群聊或联系人的聊天记录（按时间正序排列，适合上下文理解与总结）。
    
    Args:
        session_id: 会话的唯一 ID（如群聊 57684484208@chatroom 或个人绿泡泡号）
        limit: 消息条数，默认 50，最大 200
        days: 可选，限定最近 N 天内的消息
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    limit = min(limit, 200)
    sql = """
        SELECT m.id, m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.session_username = ?
    """
    params = [session_id]
    
    if days:
        cur.execute("SELECT max(create_time) FROM messages WHERE session_username = ?", (session_id,))
        max_ts = cur.fetchone()[0] or 0
        start_ts = max_ts - int(days * 24 * 3600 * _ts_unit(max_ts))
        sql += " AND m.create_time >= ?"
        params.append(start_ts)
        
    sql += " ORDER BY m.create_time DESC LIMIT ?"
    params.append(limit)
    
    rows = cur.execute(sql, params).fetchall()
    
    # 转换为正序输出，便于大模型按时间线阅读
    results = []
    for r in reversed(rows):
        results.append({
            "id": r["id"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        })
    conn.close()
    return results

@mcp.tool()
def search_shared_files(keyword: Optional[str] = None, session_id: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """查找绿泡泡聊天中传输的文件（如 PDF、Excel、Word 等文档记录）。
    
    Args:
        keyword: 文件名关键字搜索（如 '理财', '报告', 'xlsx'）
        session_id: 可选，限定特定群聊或联系人
        limit: 返回数量，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    sql = """
        SELECT m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.content LIKE '[文件]%'
    """
    params = []
    
    if keyword:
        sql += " AND m.content LIKE ?"
        params.append(f"%{keyword}%")
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)
        
    sql += " ORDER BY m.create_time DESC LIMIT ?"
    params.append(limit)
    
    rows = cur.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "session_name": r["session_name"] or r["session_username"],
            "session_id": r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "file_name": r["content"].replace("[文件] ", ""),
        })
    conn.close()
    return results

@mcp.tool()
def get_messages_by_date(date: str, session_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """按指定单日（YYYY-MM-DD）查询聊天记录（用于日报分析、事件回溯等）。
    
    Args:
        date: 日期，格式如 '2026-10-16'
        session_id: 可选，限定特定群聊或联系人
        limit: 返回消息数量，默认 100
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        dt_start = datetime.datetime.strptime(date, "%Y-%m-%d")
        dt_end = dt_start + datetime.timedelta(days=1)
    except Exception as e:
        return [{"error": f"Invalid date format: {e}"}]

    # create_time 单位随微信版本而异（秒 / 毫秒），必须按实际单位换算查询区间
    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    unit = _ts_unit(max_ts)
    start_ts = int(dt_start.timestamp() * unit)
    end_ts = int(dt_end.timestamp() * unit)
        
    sql = """
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.create_time >= ? AND m.create_time < ?
    """
    params = [start_ts, end_ts]
    
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)
        
    sql += " ORDER BY m.create_time ASC LIMIT ?"
    params.append(limit)
    
    rows = cur.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "session_name": r["session_name"] or r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        })
    conn.close()
    return results

@mcp.tool()
def sync_wechat_database(force: bool = False) -> Dict[str, Any]:
    """触发数据导入同步，使本地 MCP 数据库保持最新状态。
    
    Args:
        force: 是否强制全量同步
    """
    etl_result = sync_data(force=force)
    return {
        "etl_step": etl_result
    }

@mcp.tool()
def search_contacts(keyword: str, limit: int = 30) -> List[Dict[str, Any]]:
    """搜索绿泡泡联系人（按昵称 / 备注 / 微信号匹配）。

    Args:
        keyword: 搜索关键字（昵称、备注或微信号片段）
        limit: 返回数量，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    if not (keyword or "").strip():
        return [{"error": "keyword 不能为空"}]
    conn = get_connection()
    cur = conn.cursor()
    kw = f"%{_escape_like(keyword.strip())}%"
    rows = cur.execute("""
        SELECT username, alias, display_name, type
        FROM contacts
        WHERE display_name LIKE ? ESCAPE '\\' OR alias LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\'
        ORDER BY CASE WHEN alias IS NULL OR alias = '' THEN 1 ELSE 0 END, display_name
        LIMIT ?
    """, (kw, kw, kw, limit)).fetchall()
    conn.close()
    return [
        {
            "username": r["username"],
            "display_name": r["display_name"],
            "alias": r["alias"] or "",
            "type": r["type"],
        }
        for r in rows
    ]

@mcp.tool()
def list_chatroom_members(chatroom_id: str, keyword: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """列出指定群聊的成员名单（自动关联联系人昵称与微信号）。

    Args:
        chatroom_id: 群聊唯一 ID（形如 12345678@chatroom，可用 list_wechat_sessions 查询）
        keyword: 可选，按成员昵称 / 微信号 / username 过滤
        limit: 返回数量，默认 200
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    if not (chatroom_id or "").strip():
        return {"error": "chatroom_id 不能为空"}
    conn = get_connection()
    cur = conn.cursor()

    total = cur.execute(
        "SELECT COUNT(*) FROM chatroom_members WHERE chatroom_username = ?", (chatroom_id,)
    ).fetchone()[0]
    if total == 0:
        conn.close()
        return {"error": f"未找到群 {chatroom_id} 的成员记录，请确认群 ID（可用 list_wechat_sessions 查询）"}

    sql = """
        SELECT cm.member_username,
               COALESCE(NULLIF(c.display_name, ''), cm.member_username) AS display_name,
               COALESCE(c.alias, '') AS alias
        FROM chatroom_members cm
        LEFT JOIN contacts c ON c.username = cm.member_username
        WHERE cm.chatroom_username = ?
    """
    params: List[Any] = [chatroom_id]
    if keyword and keyword.strip():
        sql += " AND (c.display_name LIKE ? ESCAPE '\\' OR c.alias LIKE ? ESCAPE '\\' OR cm.member_username LIKE ? ESCAPE '\\')"
        kw = f"%{_escape_like(keyword.strip())}%"
        params.extend([kw, kw, kw])
    sql += " ORDER BY display_name LIMIT ?"
    params.append(limit)

    rows = cur.execute(sql, params).fetchall()
    session_row = cur.execute(
        "SELECT display_name FROM sessions WHERE username = ?", (chatroom_id,)
    ).fetchone()
    conn.close()

    return {
        "chatroom_id": chatroom_id,
        "chatroom_name": session_row["display_name"] if session_row else chatroom_id,
        "member_count": total,
        "returned": len(rows),
        "members": [
            {
                "username": r["member_username"],
                "display_name": r["display_name"],
                "alias": r["alias"],
            }
            for r in rows
        ],
    }

@mcp.tool()
def get_messages_by_range(start: str, end: str, session_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """按自定义时间范围查询聊天记录（支持跨天与任意时段，比按单日查询更灵活）。

    Args:
        start: 起始时间，'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 结束时间，同上；只给日期时自动覆盖当天全天
        session_id: 可选，限定某个群聊或联系人
        limit: 返回数量，默认 100
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    try:
        start_ts, end_ts = _time_range_to_ts(cur, start, end)
    except ValueError as exc:
        conn.close()
        return [{"error": str(exc)}]

    sql = """
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.create_time >= ? AND m.create_time < ?
    """
    params: List[Any] = [start_ts, end_ts]
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)
    sql += " ORDER BY m.create_time ASC LIMIT ?"
    params.append(limit)

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "session_id": r["session_username"],
            "session_name": r["session_name"] or r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        }
        for r in rows
    ]

@mcp.tool()
def get_chat_activity_stats(session_id: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    """统计聊天活跃度：消息总量、按小时与按天分布、发言最多的人。

    Args:
        session_id: 可选，限定某个群聊或联系人（不传则统计全部会话）
        start: 可选，起始时间 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 可选，结束时间，同上（需与 start 同时提供）
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    conn = get_connection()
    cur = conn.cursor()

    where = ["1=1"]
    params: List[Any] = []
    if session_id:
        where.append("m.session_username = ?")
        params.append(session_id)
    if start and end:
        try:
            start_ts, end_ts = _time_range_to_ts(cur, start, end)
        except ValueError as exc:
            conn.close()
            return {"error": str(exc)}
        where.append("m.create_time >= ? AND m.create_time < ?")
        params.extend([start_ts, end_ts])
    where_sql = " AND ".join(where)

    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    divisor = _ts_unit(max_ts)

    total = cur.execute(f"SELECT COUNT(*) FROM messages m WHERE {where_sql}", params).fetchone()[0]
    hourly = cur.execute(f"""
        SELECT strftime('%H', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS h,
               COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY h ORDER BY h
    """, params).fetchall()
    daily = cur.execute(f"""
        SELECT strftime('%Y-%m-%d', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS d,
               COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY d ORDER BY d
    """, params).fetchall()
    top_senders = cur.execute(f"""
        SELECT COALESCE(NULLIF(c.display_name, ''), m.sender_username) AS name,
               m.sender_username AS username, COUNT(*) AS n
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE {where_sql}
        GROUP BY m.sender_username
        ORDER BY n DESC LIMIT 10
    """, params).fetchall()
    conn.close()

    return {
        "session_id": session_id or "(全部会话)",
        "time_range": {"start": start, "end": end} if (start and end) else None,
        "total_messages": total,
        "hourly_distribution": {r["h"]: r["n"] for r in hourly},
        "daily_distribution": {r["d"]: r["n"] for r in daily},
        "top_senders": [
            {"display_name": r["name"], "username": r["username"], "message_count": r["n"]}
            for r in top_senders
        ],
    }

@mcp.tool()
def search_media_files(keyword: Optional[str] = None, kind: Optional[str] = None, month: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """查找聊天中传输过的图片 / 文件 / 视频，并返回其磁盘路径。

    Args:
        keyword: 可选，按文件名关键字搜索
        kind: 可选，类型筛选：image（图片）、file（文件）、video（视频）
        month: 可选，按月份筛选，格式 'YYYY-MM'
        limit: 返回数量，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()

    sql = """
        SELECT md5, kind, file_name, file_size, modify_time, month, rel_path, abs_path
        FROM media_files WHERE 1=1
    """
    params: List[Any] = []
    if keyword and keyword.strip():
        sql += " AND file_name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword.strip())}%")
    if kind and kind.strip():
        sql += " AND kind = ?"
        params.append(kind.strip().lower())
    if month and month.strip():
        sql += " AND month = ?"
        params.append(month.strip())
    sql += " ORDER BY modify_time DESC LIMIT ?"
    params.append(limit)

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "md5": r["md5"],
            "kind": r["kind"],
            "file_name": r["file_name"],
            "file_size": r["file_size"],
            "time": _ts_to_str(r["modify_time"]),
            "month": r["month"],
            "rel_path": r["rel_path"],
            "abs_path": r["abs_path"],
            "exists": bool(r["abs_path"]) and Path(r["abs_path"]).is_file(),
        }
        for r in rows
    ]

@mcp.tool()
def list_favorites(keyword: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """查询绿泡泡收藏条目（标题 / 描述 / 来源）。

    Args:
        keyword: 可选，按标题或描述搜索
        limit: 返回数量，默认 20
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()

    sql = "SELECT local_id, type, update_time, title, description, from_user FROM favorites WHERE 1=1"
    params: List[Any] = []
    if keyword and keyword.strip():
        kw = f"%{_escape_like(keyword.strip())}%"
        sql += " AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        params.extend([kw, kw])
    sql += " ORDER BY update_time DESC LIMIT ?"
    params.append(limit)

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "local_id": r["local_id"],
            "type": r["type"],
            "time": _ts_to_str(r["update_time"]),
            "title": r["title"],
            "description": r["description"],
            "from_user": r["from_user"],
        }
        for r in rows
    ]

def main():
    mcp.run()

if __name__ == "__main__":
    main()
