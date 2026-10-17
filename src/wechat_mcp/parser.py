import re
import logging

logger = logging.getLogger(__name__)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

try:
    import zstandard as _zstd
    _ZSTD_CTX = _zstd.ZstdDecompressor()
    HAS_ZSTD = True
except ImportError:
    _ZSTD_CTX = None
    HAS_ZSTD = False

def maybe_zstd_decompress(data) -> bytes:
    """若是 zstd 压缩包则解压，否则原样返回"""
    if not data:
        return b""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="ignore")
    if data[:4] != ZSTD_MAGIC:
        return bytes(data)
    if not HAS_ZSTD:
        return b""
    try:
        return _ZSTD_CTX.decompress(data)
    except Exception:
        return b""

TYPE_LABELS = {
    1: "文本",
    3: "图片",
    34: "语音",
    43: "视频",
    47: "表情",
    48: "位置",
    50: "通话",
    51: "视频号",
    52: "视频号",
    10000: "系统",
}

APPMSG_LABELS = {
    1: "链接",
    2: "相册",
    3: "图片",
    4: "视频",
    5: "链接",
    6: "文件",
    7: "位置",
    8: "表情",
    9: "图文",
    10: "音乐",
    11: "音乐",
    12: "名片",
    14: "打招呼",
    15: "频道",
    16: "群接龙",
    17: "实时语音",
    18: "企业绿泡泡",
    19: "聊天记录",
    20: "卡券",
    24: "群体预览",
    25: "群公告",
    33: "小程序",
    34: "小程序",
    35: "小程序",
    36: "视频号",
    57: "引用",
}

def extract_title_from_xml(xml_text: str) -> str:
    """从 appmsg XML 中提取 <title> 内容"""
    if not xml_text:
        return ""
    m = re.search(r"<title>(.*?)</title>", xml_text, re.DOTALL)
    if not m:
        return ""
    val = m.group(1).strip()
    val = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", val, flags=re.DOTALL)
    val = val.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    return val.strip()

def build_content(local_type: int, raw_content, raw_source) -> str:
    """将 raw_content / raw_source 根据 local_type 构建成可读字符串"""
    if local_type == 1:
        if isinstance(raw_content, bytes):
            return maybe_zstd_decompress(raw_content).decode("utf-8", errors="ignore")
        return str(raw_content or "")

    label = TYPE_LABELS.get(local_type)
    if label and local_type != 10000:
        return f"[{label}]"

    # appmsg 处理
    decompressed = maybe_zstd_decompress(raw_content or raw_source)
    xml_text = decompressed.decode("utf-8", errors="ignore") if decompressed else ""
    app_type = (local_type >> 32) if local_type > 0xFFFFFFFF else 0
    sub_label = APPMSG_LABELS.get(app_type, "消息")

    if xml_text:
        title = extract_title_from_xml(xml_text)
        if title:
            return f"[{sub_label}] {title}"
        if "<appmsg" in xml_text:
            return f"[{sub_label}]"

    return f"[{sub_label}]" if app_type else "[多媒体消息]"
