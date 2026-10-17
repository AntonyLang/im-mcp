"""绿泡泡 3.x 数据库解析同步模块

绿泡泡 3.x 数据特征：
- 数据库位于 MicroMsg.db, MSG0.db, MSG1.db, MediaMSG0.db 等
- MicroMsg.db:
    - Contact 表: UserName, Alias, Remark, NickName, Type
    - ChatRoom 表: ChatRoomName, UserName, DisplayName
- MSG*.db:
    - MSG 表集中存储所有聊天记录: localId, StrTalker, IsSender, CreateTime, Type, SubType, StrContent, BytesExtra, CompressContent
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def is_v3_layout(decrypted_dir: Path) -> bool:
    """判断是否为绿泡泡 3.x 解密结构"""
    patterns = [
        decrypted_dir / "MicroMsg.db",
        decrypted_dir / "Msg" / "MicroMsg.db",
        decrypted_dir / "Multi" / "MSG0.db",
        decrypted_dir / "Msg" / "Multi" / "MSG0.db",
        decrypted_dir / "MSG0.db",
    ]
    return any(p.exists() for p in patterns)


def sync_v3(decrypted_dir: Path, cur: sqlite3.Cursor) -> None:
    """执行绿泡泡 3.x 数据库同步"""
    # 查找 MicroMsg.db
    micromsg_candidates = list(decrypted_dir.rglob("MicroMsg.db"))
    for db_file in micromsg_candidates:
        try:
            c_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            c_conn.row_factory = sqlite3.Row
            tables = [r[0] for r in c_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]

            # 联系人表 (Contact)
            if "Contact" in tables:
                for r in c_conn.execute("SELECT UserName, Alias, Remark, NickName, Type FROM Contact"):
                    uname = r["UserName"]
                    if not uname:
                        continue
                    disp = r["Remark"] or r["NickName"] or r["Alias"] or uname
                    cur.execute("""
                        INSERT OR REPLACE INTO contacts (username, alias, display_name, type)
                        VALUES (?, ?, ?, ?)
                    """, (uname, r["Alias"], disp, r["Type"] or 0))

            # 群成员表 (ChatRoom)
            if "ChatRoom" in tables:
                for r in c_conn.execute("SELECT ChatRoomName, UserName, DisplayName FROM ChatRoom"):
                    if r["ChatRoomName"] and r["UserName"]:
                        cur.execute("""
                            INSERT OR REPLACE INTO chatroom_members (chatroom_username, member_username, display_name)
                            VALUES (?, ?, ?)
                        """, (r["ChatRoomName"], r["UserName"], r["DisplayName"]))

            # 会话表 (Session)
            if "Session" in tables:
                cols = [r[1] for r in c_conn.execute("PRAGMA table_info(Session)").fetchall()]
                time_col = "CreateTime" if "CreateTime" in cols else "0"
                content_col = "Content" if "Content" in cols else "''"
                for r in c_conn.execute(f"SELECT strUsrName, {time_col} AS last_time, {content_col} AS summary FROM Session"):
                    uname = r["strUsrName"]
                    if not uname:
                        continue
                    stype = "room" if "@chatroom" in uname or "@openim" in uname else ("gh" if uname.startswith("gh_") else "c2c")
                    cur.execute("SELECT display_name FROM contacts WHERE username = ?", (uname,))
                    c_row = cur.fetchone()
                    dname = c_row[0] if c_row else uname

                    cur.execute("""
                        INSERT OR REPLACE INTO sessions (username, type, display_name, last_time, last_msg_preview)
                        VALUES (?, ?, ?, ?, ?)
                    """, (uname, stype, dname, r["last_time"], r["summary"]))

            c_conn.close()
        except Exception as e:
            logger.warning(f"Error parsing 3.x MicroMsg.db {db_file}: {e}")

    # 查找 MSG*.db
    msg_db_files = sorted(set(list(decrypted_dir.rglob("MSG*.db")) + list(decrypted_dir.rglob("ChatMsg.db"))))
    for db_file in msg_db_files:
        try:
            m_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            m_conn.row_factory = sqlite3.Row
            tables = [r[0] for r in m_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            target_table = "MSG" if "MSG" in tables else ("ChatCRMsg" if "ChatCRMsg" in tables else None)

            if target_table:
                cols = [r[1] for r in m_conn.execute(f"PRAGMA table_info('{target_table}')").fetchall()]
                talker_col = "StrTalker" if "StrTalker" in cols else ("TalkerId" if "TalkerId" in cols else "''")
                content_col = "StrContent" if "StrContent" in cols else "''"
                type_col = "Type" if "Type" in cols else "1"
                time_col = "CreateTime" if "CreateTime" in cols else "0"

                for r in m_conn.execute(f"SELECT localId, {talker_col} AS StrTalker, IsSender, {type_col} AS Type, {time_col} AS CreateTime, {content_col} AS StrContent FROM '{target_table}'"):
                    session_name = r["StrTalker"]
                    if not session_name:
                        continue
                    is_sender = r["IsSender"]
                    sender_name = "me" if is_sender == 1 else session_name
                    raw_content = r["StrContent"] or ""

                    cur.execute("""
                        INSERT OR IGNORE INTO messages 
                        (source_file, local_id, session_username, sender_username, create_time, local_type, content)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (db_file.name, r["localId"], session_name, sender_name, r["CreateTime"], r["Type"], raw_content))

            m_conn.close()
        except Exception as e:
            logger.warning(f"Error parsing 3.x MSG db {db_file}: {e}")
