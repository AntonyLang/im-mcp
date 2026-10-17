"""DingTalk local database decryptor for V2 and V3."""

import base64
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import re
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_V3_PBKDF2_SALT = b"666DingTalk888"[:8]  # "666DingT"
_V3_PBKDF2_ITERATIONS = 1000
_V3_PBKDF2_DKLEN = 32
_AES_PAGE_SIZE = 4096


def detect_dingtalk_user() -> Tuple[Optional[str], Optional[str], int, Optional[str]]:
    """Auto-detect DingTalk user data directory, UID, version, and salt.

    Returns:
        (data_dir, uid, version, salt)
    """
    env_uid = os.environ.get("DINGTALK_UID", "").strip()
    env_dir = os.environ.get("DINGTALK_DATA_DIR", "").strip()

    if env_uid and env_dir:
        ver = 3 if "_v3" in os.path.basename(env_dir) else 2
        salt = read_v3_salt(env_dir) if ver == 3 else None
        return env_dir, env_uid, ver, salt

    search_bases = []
    appdata = os.environ.get("APPDATA", "")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")

    if appdata:
        search_bases.append(os.path.join(appdata, "DingTalk"))
    if local_appdata and local_appdata != appdata:
        search_bases.append(os.path.join(local_appdata, "DingTalk"))
    if userprofile:
        search_bases.append(os.path.join(userprofile, "AppData", "Roaming", "DingTalk"))
        search_bases.append(os.path.join(userprofile, "AppData", "Local", "DingTalk"))

    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        search_bases.append(
            os.path.join(
                home,
                "Library",
                "Containers",
                "5ZSL2CJU2T.com.dingtalk.mac",
                "Data",
                "Library",
                "Application Support",
                "DingTalkMac",
            )
        )
        search_bases.append(os.path.join(home, "Library", "Application Support", "DingTalk"))
    elif sys.platform.startswith("linux"):
        search_bases.append(os.path.join(home, ".config", "DingTalk"))
        search_bases.append(os.path.join(home, ".local", "share", "DingTalk"))

    found_dirs = []
    for base in search_bases:
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            if entry.endswith(("_v2", "_v3")):
                full_path = os.path.join(base, entry)
                if os.path.isdir(full_path):
                    db_file = os.path.join(full_path, "DBFiles", "dingtalk.db")
                    if os.path.exists(db_file):
                        folder_id = entry.rsplit("_v", 1)[0]
                        version = 3 if entry.endswith("_v3") else 2
                        mtime = os.path.getmtime(db_file)
                        found_dirs.append((folder_id, full_path, mtime, version))

    if not found_dirs:
        return None, None, 2, None

    # Prefer most recently updated directory
    found_dirs.sort(key=lambda x: (x[3], x[2]), reverse=True)
    folder_id, path, _, version = found_dirs[0]

    uid = folder_id
    salt = None
    if version == 3:
        salt = read_v3_salt(path)
        # If user explicitly supplied DINGTALK_UID, use it
        if env_uid:
            uid = env_uid
        else:
            # V3 folder name is a hash, whereas PBKDF2 requires the real integer UID.
            # Try auto-detecting the real UID from DingTalk log files.
            detected_uid = find_v3_uid_from_logs(path, salt)
            if detected_uid:
                logger.info(f"Auto-detected DingTalk V3 real UID: {detected_uid}")
                uid = detected_uid

    return path, uid, version, salt


def find_v3_uid_from_logs(data_dir: str, salt: Optional[str]) -> Optional[str]:
    """Scan DingTalk log files for candidate UIDs and verify against the DB header."""
    if not salt:
        return None

    db_path = os.path.join(data_dir, "DBFiles", "dingtalk.db")
    if not os.path.isfile(db_path):
        return None

    try:
        with open(db_path, "rb") as f:
            sample_page = f.read(_AES_PAGE_SIZE)
        if len(sample_page) < _AES_PAGE_SIZE:
            return None
    except Exception as e:
        logger.warning(f"Failed to read sample page from {db_path}: {e}")
        return None

    # Search for log directories relative to data_dir or parent DingTalk root
    parent_dir = os.path.dirname(data_dir)
    possible_log_dirs = [
        os.path.join(parent_dir, "log"),
        os.path.join(data_dir, "log"),
        os.path.join(parent_dir, "holmeslogs"),
    ]

    candidates: Set[str] = set()
    uid_patterns = [
        re.compile(r'(?:real_uid|myOpenId|uid|userId|openId)[\"\'\s:=]+([0-9]{6,12})', re.IGNORECASE),
        re.compile(r'success\s+uid:([0-9]{6,12})', re.IGNORECASE),
    ]

    for log_dir in possible_log_dirs:
        if not os.path.isdir(log_dir):
            continue
        try:
            for root, _, files in os.walk(log_dir):
                for file_name in files:
                    if file_name.endswith((".log", ".txt")):
                        file_path = os.path.join(root, file_name)
                        try:
                            with open(file_path, "r", encoding="utf-8", errors="ignore") as lf:
                                for line in lf:
                                    for pattern in uid_patterns:
                                        for match in pattern.finditer(line):
                                            cand = match.group(1)
                                            if cand not in candidates:
                                                candidates.add(cand)
                                                if _verify_v3_uid(cand, salt, sample_page):
                                                    return cand
                        except Exception:
                            continue
        except Exception:
            continue

    # Try all gathered candidates if not returned early
    for cand in candidates:
        if _verify_v3_uid(cand, salt, sample_page):
            return cand

    return None


def _verify_v3_uid(uid: str, salt: str, sample_page: bytes) -> bool:
    """Check if the candidate UID successfully decrypts page 1 to a valid SQLite header."""
    try:
        from Crypto.Cipher import AES

        key = generate_key(uid, version=3, salt_hex=salt)
        cipher = AES.new(key, AES.MODE_ECB)
        dec = bytearray()
        for i in range(0, min(len(sample_page), _AES_PAGE_SIZE), 16):
            dec.extend(cipher.decrypt(sample_page[i : i + 16]))
        return bytes(dec[:16]) == b"SQLite format 3\x00"
    except Exception:
        return False


def read_v3_salt(data_dir: str) -> Optional[str]:
    """Read the salt from V3's user_config file."""
    config_path = os.path.join(data_dir, "user_config")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        decoded = base64.b64decode(raw).decode("utf-8")
        data = json.loads(decoded)
        return data.get("salt")
    except Exception as e:
        logger.warning(f"Failed to read V3 salt: {e}")
        return None


def generate_key(uid: str, version: int = 2, salt_hex: Optional[str] = None) -> bytes:
    """Generate AES-128-ECB decryption key for DingTalk."""
    if version == 3:
        if not salt_hex:
            raise ValueError("V3 salt is required for key derivation")
        pwd = (uid + salt_hex).encode("utf-8")
        dk = hashlib.pbkdf2_hmac("sha1", pwd, _V3_PBKDF2_SALT, _V3_PBKDF2_ITERATIONS, dklen=_V3_PBKDF2_DKLEN)
        md5_hex = hashlib.md5(dk).hexdigest()
        return md5_hex[:16].encode("ascii")
    else:
        md5_hex = hashlib.md5(uid.encode("utf-8")).hexdigest()
        return md5_hex[:16].encode("ascii")


def decrypt_db_file(src_path: str, dst_path: str, key: bytes, is_wal: bool = False) -> None:
    """Decrypt a DingTalk SQLite or WAL file page by page using AES-128-ECB."""
    from Crypto.Cipher import AES

    cipher = AES.new(key, AES.MODE_ECB)

    with open(src_path, "rb") as f:
        data = f.read()

    result = bytearray()
    if not is_wal:
        # Standard SQLite DB file: 4096-byte pages
        for offset in range(0, len(data), _AES_PAGE_SIZE):
            page = data[offset : offset + _AES_PAGE_SIZE]
            if len(page) == _AES_PAGE_SIZE:
                for i in range(0, _AES_PAGE_SIZE, 16):
                    result.extend(cipher.decrypt(page[i : i + 16]))
            else:
                result.extend(page)
    else:
        # SQLite WAL file: 32-byte plaintext header, then frames (24-byte plaintext header + 4096-byte page)
        if len(data) >= 32:
            result.extend(data[:32])
            frame_size = 24 + _AES_PAGE_SIZE
            for offset in range(32, len(data), frame_size):
                frame = data[offset : offset + frame_size]
                if len(frame) == frame_size:
                    frame_header = frame[:24]
                    page_data = frame[24:]
                    result.extend(frame_header)
                    for i in range(0, _AES_PAGE_SIZE, 16):
                        result.extend(cipher.decrypt(page_data[i : i + 16]))
                else:
                    result.extend(frame)
        else:
            result.extend(data)

    with open(dst_path, "wb") as f:
        f.write(result)


def _merge_wal_frames_into_db(db_path: str, enc_wal_path: str, cipher) -> None:
    """Fallback merge: directly replay decrypted WAL frames into database pages.

    This handles cases where the host SQLite library refuses to checkpoint due to
    WAL header checksum differences in encrypted/raw streams.
    """
    import struct

    pages = {}
    with open(db_path, "rb") as f:
        db_data = f.read()
    for i in range(0, len(db_data), _AES_PAGE_SIZE):
        pg = i // _AES_PAGE_SIZE + 1
        pages[pg] = db_data[i : i + _AES_PAGE_SIZE]

    with open(enc_wal_path, "rb") as f:
        hdr = f.read(32)
        if len(hdr) < 32:
            return
        frame_size = 24 + _AES_PAGE_SIZE
        while True:
            fhdr = f.read(24)
            if len(fhdr) < 24:
                break
            pgno, commit, _, _, _, _ = struct.unpack(">IIIIII", fhdr)
            page_data = f.read(_AES_PAGE_SIZE)
            if len(page_data) < _AES_PAGE_SIZE:
                break
            dec_page = bytearray()
            for j in range(0, _AES_PAGE_SIZE, 16):
                dec_page.extend(cipher.decrypt(page_data[j : j + 16]))
            pages[pgno] = bytes(dec_page)

    if not pages:
        return

    max_p = max(pages.keys())
    with open(db_path, "wb") as f:
        for p in range(1, max_p + 1):
            if p in pages:
                f.write(pages[p])
            else:
                f.write(b"\x00" * _AES_PAGE_SIZE)


def decrypt_dingtalk(
    data_dir: str,
    uid: str,
    version: int,
    salt: Optional[str],
    output_dir: str,
) -> str:
    """Safely copy, decrypt and checkpoint DingTalk database into output_dir.

    Returns the path to the decrypted dingtalk.db.
    """
    from Crypto.Cipher import AES

    key = generate_key(uid, version, salt)
    cipher = AES.new(key, AES.MODE_ECB)
    os.makedirs(output_dir, exist_ok=True)

    db_dir = os.path.join(data_dir, "DBFiles")
    src_db = os.path.join(db_dir, "dingtalk.db")
    src_wal = os.path.join(db_dir, "dingtalk.db-wal")

    if not os.path.isfile(src_db):
        raise FileNotFoundError(f"dingtalk.db not found in {db_dir}")

    # Copy to temp first to avoid locking
    temp_dir = tempfile.mkdtemp(prefix="dt_decrypt_")
    try:
        temp_enc_db = os.path.join(temp_dir, "enc.db")
        temp_enc_wal = os.path.join(temp_dir, "enc.db-wal")
        shutil.copy2(src_db, temp_enc_db)
        has_wal = False
        if os.path.isfile(src_wal) and os.path.getsize(src_wal) > 0:
            shutil.copy2(src_wal, temp_enc_wal)
            has_wal = True

        out_db = os.path.join(output_dir, "dingtalk.db")
        out_wal = os.path.join(output_dir, "dingtalk.db-wal")

        decrypt_db_file(temp_enc_db, out_db, key, is_wal=False)

        # Check valid header
        with open(out_db, "rb") as f:
            header = f.read(16)
        if header != b"SQLite format 3\x00":
            raise RuntimeError(
                f"Decryption failed: invalid SQLite header. Please check DingTalk UID ({uid})."
            )

        if has_wal:
            decrypt_db_file(temp_enc_wal, out_wal, key, is_wal=True)
            # Try SQLite native WAL checkpoint
            checkpoint_ok = False
            try:
                conn = sqlite3.connect(out_db)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                # Verify if tables were imported
                tbl_cnt = conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table';"
                ).fetchone()[0]
                conn.close()
                if tbl_cnt > 0:
                    checkpoint_ok = True
            except Exception as e:
                logger.warning(f"Native WAL checkpoint warning: {e}")

            # If native checkpoint produced 0 tables or failed due to WAL checksum mismatch,
            # replay WAL frames directly to assemble the full database.
            if not checkpoint_ok:
                logger.info("Applying direct WAL frame reassembly fallback...")
                _merge_wal_frames_into_db(out_db, temp_enc_wal, cipher)
                if os.path.exists(out_wal):
                    try:
                        os.remove(out_wal)
                    except Exception:
                        pass

        return out_db
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
