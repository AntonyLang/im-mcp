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
    except Exception as exc:
        logger.warning(f"zstd 解压失败（输入 {len(data)} 字节）: {exc}")
        return b""

TYPE_LABELS = {
    1: "文本",
    3: "图片",
    34: "语音",
    37: "好友请求",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "应用消息",
    50: "通话",
    51: "视频号",
    52: "视频号",
    10000: "系统",
    10002: "系统",
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
    62: "拍一拍",
}

def _clean_xml_text(value: str) -> str:
    """去掉 CDATA 包裹并还原常见 XML 实体"""
    text = (value or "").strip()
    text = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", text, flags=re.DOTALL)
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .strip()
    )


def extract_tag_from_xml(xml_text: str, tag: str) -> str:
    """提取指定标签内的首个非空文本（跳过自闭合与空标签）"""
    if not xml_text:
        return ""
    pattern = rf"<{tag}((?:\s[^>]*)?)>(.*?)</{tag}>"
    for match in re.finditer(pattern, xml_text, re.DOTALL):
        if match.group(1).rstrip().endswith("/"):
            continue  # 自闭合标签 <tag />，跳过以免吞掉后续正文
        value = _clean_xml_text(match.group(2))
        if value:
            return value
    return ""


def extract_title_from_xml(xml_text: str) -> str:
    """从 appmsg XML 中提取 <title> 内容"""
    return extract_tag_from_xml(xml_text, "title")

def build_content(local_type: int, raw_content, raw_source) -> str:
    """将 raw_content / raw_source 根据 local_type 构建成可读字符串"""
    if local_type == 1:
        if isinstance(raw_content, bytes):
            return maybe_zstd_decompress(raw_content).decode("utf-8", errors="ignore")
        return str(raw_content or "")

    # 系统消息（撤回 / 入群提示等）：正文多为可读文本，部分 type 为 sysmsg XML
    if local_type in (10000, 10002):
        raw = maybe_zstd_decompress(raw_content or raw_source)
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw or "")
        text = text.strip()
        if "<sysmsg" in text:
            for tag in ("content", "text", "replacemsg", "plain"):
                value = extract_tag_from_xml(text, tag)
                if value:
                    return value
        return text or "[系统]"

    label = TYPE_LABELS.get(local_type)
    if label:
        return f"[{label}]"

    # appmsg 处理
    decompressed = maybe_zstd_decompress(raw_content or raw_source)
    xml_text = decompressed.decode("utf-8", errors="ignore") if decompressed else ""
    app_type = (local_type >> 32) if local_type > 0xFFFFFFFF else 0
    sub_label = APPMSG_LABELS.get(app_type, "消息")

    if xml_text:
        # 引用消息：<title> 是本次回复内容，<refermsg> 里才是被引用原文与作者
        if app_type == 57:
            reply = extract_tag_from_xml(xml_text, "title")
            quoted = extract_tag_from_xml(xml_text, "content")
            who = extract_tag_from_xml(xml_text, "displayname")
            if quoted:
                head = f"[引用 {who}: {quoted}]" if who else f"[引用: {quoted}]"
                return f"{head} {reply}" if reply else head
            if reply:
                return f"[引用] {reply}"

        title = extract_tag_from_xml(xml_text, "title")
        if title:
            return f"[{sub_label}] {title}"
        if "<appmsg" in xml_text:
            return f"[{sub_label}]"

    return f"[{sub_label}]" if app_type else "[多媒体消息]"
