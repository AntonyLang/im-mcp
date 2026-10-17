import os
import sys
import tempfile
import unittest
import shutil
from pathlib import Path

# 将 src 加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from im_clean import get_cleanup_targets, format_size


class TestImClean(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(format_size(500), "500.0 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(format_size(1024 * 1024 * 1024), "1.0 GB")

    def test_cleanup_targets_structure(self):
        targets = get_cleanup_targets()
        self.assertIn("wechat", targets)
        self.assertIn("dingtalk", targets)
        self.assertIsInstance(targets["wechat"], list)
        self.assertIsInstance(targets["dingtalk"], list)


if __name__ == "__main__":
    unittest.main()
