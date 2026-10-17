import sqlite3
import re
import hashlib
from pathlib import Path
import logging

from .config import DB_PATH, get_decrypted_dir
from .adapters import route_sync

logger = logging.getLogger(__name__)

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_schema():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            username TEXT PRIMARY KEY,
            type TEXT,
            display_name TEXT,
            message_count INTEGER DEFAULT 0,
            first_time INTEGER,
            last_time INTEGER,
            last_msg_preview TEXT
        );

        CREATE TABLE IF NOT EXISTS contacts (
            username TEXT PRIMARY KEY,
            alias TEXT,
            display_name TEXT,
            type INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chatroom_members (
            chatroom_username TEXT,
            member_username TEXT,
            display_name TEXT,
            PRIMARY KEY (chatroom_username, member_username)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT,
            local_id INTEGER,
            session_username TEXT,
            sender_username TEXT,
            create_time INTEGER,
            local_type INTEGER,
            content TEXT,
            UNIQUE(source_file, session_username, local_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session_time ON messages(session_username, create_time);
        CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(create_time);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            content='messages',
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
        END;
    """)
    conn.commit()
    conn.close()

def compute_fingerprint(decrypted_dir: Path) -> str:
    """按文件修改时间与大小计算指纹"""
    items = []
    for p in sorted(decrypted_dir.rglob("*.db")):
        try:
            stat = p.stat()
            items.append(f"{p.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except Exception:
            pass
    return hashlib.md5("\n".join(items).encode()).hexdigest()

def sync_data(force: bool = False) -> dict:
    """将解密后的 sqlite 数据同步到统一中间库"""
    decrypted_dir = get_decrypted_dir()
    if not decrypted_dir.exists():
        return {"status": "error", "message": f"Decrypted dir {decrypted_dir} not found"}

    init_schema()
    conn = get_connection()
    cur = conn.cursor()

    fp = compute_fingerprint(decrypted_dir)
    cur.execute("SELECT value FROM meta WHERE key='fingerprint'")
    row = cur.fetchone()
    if not force and row and row[0] == fp:
        conn.close()
        return {"status": "skipped", "message": "No new database changes"}

    # 按版本路由执行解密数据库同步 (v3 / v4 / 扩展版本)
    adapter_used = route_sync(decrypted_dir, cur)

    # 更新会话统计与 FTS 索引
    cur.execute("""
        UPDATE sessions SET message_count = (
            SELECT count(*) FROM messages WHERE messages.session_username = sessions.username
        )
    """)

    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fingerprint', ?)", (fp,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Database synchronized successfully (adapter: {adapter_used})"}
