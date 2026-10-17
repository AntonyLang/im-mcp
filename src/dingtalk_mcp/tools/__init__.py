"""DingTalk tools and initialization CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dingtalk_mcp.decrypt import decrypt_dingtalk, detect_dingtalk_user

DEFAULT_DECRYPTED_DIR = Path.home() / ".dingtalk_mcp" / "decrypted"


def main() -> None:
    """Friendly one-click init CLI for DingTalk database synchronization & decryption."""
    parser = argparse.ArgumentParser(
        prog="dingtalk-init",
        description="DuangDuang数据库离线解密与同步工具 (One-click DingTalk DB Initializer)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_DECRYPTED_DIR),
        help=f"解密后 SQLite 数据库保存目录 (默认: {DEFAULT_DECRYPTED_DIR})",
    )
    args = parser.parse_args()
    out_dir = Path(args.output_dir).resolve()

    print("=" * 60)
    print("🚀 DuangDuang数据库初始化向导 (dingtalk-init)")
    print("=" * 60)
    print(f"[*] 提示: 请确保 Windows DuangDuang客户端已正常登录。")
    print(f"[*] 目标保存目录: {out_dir}")
    print("-" * 60)

    try:
        data_dir, uid, ver, salt = detect_dingtalk_user()
        if not data_dir or not uid:
            print("[ERROR] 未能检测到活跃的DuangDuang用户数据目录，请确认DuangDuang已登录。")
            sys.exit(1)

        print(f"[*] 检测到DuangDuang用户 UID: {uid} (Version: V{ver})")
        print(f"[*] 正在执行离线拷贝、AES 解密与 WAL 合并...")
        out_db = decrypt_dingtalk(data_dir, uid, ver, salt, str(out_dir))
        print("=" * 60)
        print(f"🎉 DuangDuang数据初始化同步成功！")
        print(f"[*] 数据库位置: {Path(out_db).resolve()}")
        print(f"[*] 现在您可以直接启动 dingtalk-mcp 服务进行查询了。")
        print("=" * 60)
    except KeyboardInterrupt:
        print("\n[!] 操作已取消。")
        sys.exit(130)
    except Exception as e:
        print(f"\n[ERROR] DuangDuang数据同步失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
