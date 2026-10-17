import os
import sys
import unittest
from pathlib import Path

# 将 src 加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dingtalk_mcp.decrypt import generate_key


class TestDingTalkDecrypt(unittest.TestCase):
    def test_generate_key_v2(self):
        # 钉钉 V2 AES 密钥必须是 16 字节
        dummy_uid = "12345678"
        key = generate_key(dummy_uid, version=2)
        self.assertEqual(len(key), 16)
        self.assertIsInstance(key, bytes)

    def test_generate_key_v3(self):
        # 钉钉 V3 结合 salt 生成 16 字节密钥
        dummy_uid = "12345678"
        dummy_salt = "abcd1234efgh5678"
        key = generate_key(dummy_uid, version=3, salt_hex=dummy_salt)
        self.assertEqual(len(key), 16)
        self.assertIsInstance(key, bytes)

    def test_generate_key_reproducibility(self):
        uid = "987654321"
        key1 = generate_key(uid, version=2)
        key2 = generate_key(uid, version=2)
        self.assertEqual(key1, key2)


if __name__ == "__main__":
    unittest.main()
