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
    
    # 构造 FTS 查询
    # 清理查询词中可能的特殊字符
    clean_query = query.replace('"', '""')
    
    sql = """
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages_fts fts
        JOIN messages m ON fts.rowid = m.id
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE messages_fts MATCH ?
    """
    params = [clean_query]
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

def main():
    mcp.run()

if __name__ == "__main__":
    main()
