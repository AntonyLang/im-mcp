import os
import sys
import unittest
import sqlite3
from pathlib import Path

# 将 src 加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_mcp.config import get_decrypted_dir, DB_PATH
from wechat_mcp.parser import build_content, TYPE_LABELS, APPMSG_LABELS


class TestRealWeChatMessageParsing(unittest.TestCase):
    """使用本机真实解密的 WCDB SQLite 消息库直接驱动解析链路进行端到端测试。

    测试原则：
    1. 动态自愈：不硬编码任何具体联系人昵称、微信号、文本内容或路径；
    2. 覆盖广度：随机/抽样扫描真实库中的各类消息（文本、引用、系统、图片、文件、分享等）；
    3. 鲁棒性验证：验证 build_content 在面对数千条真实 payload 时 0 崩溃，且返回合法的字符串。
    """

    @classmethod
    def setUpClass(cls):
        cls.decrypted_dir = get_decrypted_dir()
        cls.message_dir = cls.decrypted_dir / "message"
        cls.has_real_dbs = cls.message_dir.exists() and any(cls.message_dir.glob("message_*.db"))

    def test_real_message_parsing_sampling(self):
        if not self.has_real_dbs:
            self.skipTest("本地未检测到真实解密的 message_*.db，跳过真实消息解析测试")

        db_files = list(self.message_dir.glob("message_*.db"))
        self.assertTrue(len(db_files) > 0)

        total_tested = 0
        tested_by_type: dict[int, int] = {}

        # 遍历前 3 个 message db，每个库抽取若干分表验证
        for db_path in db_files[:3]:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            tables = [
                r[0]
                for r in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
            ]

            for tbl in tables[:15]:  # 每个分库抽样 15 个会话表
                rows = cursor.execute(
                    f"SELECT local_id, local_type, message_content, source FROM {tbl} ORDER BY create_time DESC LIMIT 40"
                ).fetchall()

                for r in rows:
                    local_type = r["local_type"]
                    raw_content = r["message_content"]
                    raw_source = r["source"]

                    # 核心测试：调用真实的 parser 进行解压与内容构造
                    try:
                        content = build_content(local_type, raw_content, raw_source)
                    except Exception as e:
                        self.fail(f"解析真实消息失败 (table={tbl}, id={r['local_id']}, type={local_type}): {e}")

                    # 校验产物
                    self.assertIsInstance(content, str)
                    self.assertTrue(len(content) >= 0)

                    # 规则校验 1：群聊中的 wxid_xxx: 冗余前缀必须被彻底剥离
                    if local_type == 1 and content:
                        self.assertFalse(
                            content.startswith("wxid_") and ":" in content[:35],
                            f"群聊消息残留 wxid 前缀: {content[:30]}..."
                        )

                    # 规则校验 2：系统消息 (type=10000) 绝不能被错误降级为 [多媒体消息]
                    if local_type == 10000:
                        self.assertNotEqual(content, "[多媒体消息]")

                    total_tested += 1
                    tested_by_type[local_type] = tested_by_type.get(local_type, 0) + 1

            conn.close()

        # 确保抽样了足够数量和类型的真实消息
        self.assertGreater(total_tested, 100, f"真实消息抽样测试覆盖量不足 (实测: {total_tested})")
        print(f"\n[+] 真实数据测试验证完成: 成功解析 {total_tested} 条真实消息，覆盖 {len(tested_by_type)} 种消息类型")

    def test_real_app_db_fts_search(self):
        """测试本地实际生成的 app.db FTS 全文搜索与查询链路"""
        if not DB_PATH.exists():
            self.skipTest("本地尚未构建 app.db 索引，跳过此项")

        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cur = conn.cursor()

        # 检查 messages 表是否有实际数据
        total = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if total == 0:
            conn.close()
            self.skipTest("本地 app.db 暂无消息数据")

        # 抽样前 10 条带文本的消息
        sample_rows = cur.execute(
            "SELECT content FROM messages WHERE local_type=1 AND length(content) >= 4 LIMIT 10"
        ).fetchall()

        for (sample_text,) in sample_rows:
            # 截取其中 2 个字进行搜索验证
            keyword = sample_text[1:3].strip()
            if not keyword or keyword in ("\n", " "):
                continue

            # 使用 FastMCP 中的查询逻辑执行测试
            from wechat_mcp.server import search_wechat_messages
            results = search_wechat_messages(query=keyword, limit=5)
            self.assertIsInstance(results, list)
            # 既然取自已有的文本片段，必须至少能搜出匹配项
            self.assertGreater(len(results), 0, f"真实关键词 '{keyword}' 全文搜索召回失败")
            break

        conn.close()


if __name__ == "__main__":
    unittest.main()
