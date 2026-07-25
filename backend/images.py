"""图片回链服务:caption 索引查询 + 安全的图片文件发送。

image_index.json 由 scripts/build_image_index.py 离线生成。
chunk 文本里含 "图1.1 xxx" / "Fig. 3 xxx" 之类 caption,
用归一化子串匹配把 chunk 关联回 MinerU 解析出的原图。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from backend.config import settings

_index: list[dict] | None = None
_by_token: dict[str, str] | None = None  # img token -> abs path


def _norm(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    text = text.replace("．", ".").replace("（", "(").replace("）", ")")
    return text.lower()


def _load() -> tuple[list[dict], dict[str, str]]:
    global _index, _by_token
    if _index is not None:
        return _index, _by_token
    f = settings.data_dir / "image_index.json"
    _index, _by_token = [], {}
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        _index = data.get("entries", [])
        allowed_roots = [Path(p).resolve() for p in settings.parser_output_dirs]
        for e in _index:
            p = Path(e["img"])
            # 只登记位于允许目录内的文件(防索引文件被篡改后任意读)
            if not any(p.resolve().is_relative_to(r) for r in allowed_roots
                       if r.exists()):
                continue
            token = hashlib.sha1(e["img"].encode()).hexdigest()[:16]
            e["token"] = token
            _by_token[token] = e["img"]
    return _index, _by_token


# caption 引导词模式:图1.1 / 表2 / Fig. 3 / Figure 4 / Table 5
_CAP_RE = re.compile(
    r"(?:图|表)\s?\d+(?:[\.．-]\d+)?[^\n]{0,80}|(?:Fig(?:ure)?|Table)\.?\s?\d+[^\n]{0,80}",
    re.IGNORECASE,
)
# 合法索引 caption:必须以图/表编号开头(过滤 OCR 碎片)
_CAP_LEAD_RE = re.compile(
    r"^(?:图|表)\s?\d|^(?:fig(?:ure)?|table)\.?\s?\d", re.IGNORECASE,
)


def find_images_for_text(text: str, *, limit: int = 4) -> list[dict]:
    """从 chunk/引用预览文本中提取 caption,返回匹配的图片 [{token, caption, doc}]。"""
    index, _ = _load()
    if not index or not text:
        return []
    found: list[dict] = []
    seen_tokens: set[str] = set()
    seen_captions: set[str] = set()  # 同 caption 不重复出图(v1/v2 重复解析)
    for m in _CAP_RE.finditer(text):
        cap_norm = _norm(m.group(0))
        if len(cap_norm) < 8:
            continue
        for e in index:
            if "token" not in e or e["token"] in seen_tokens:
                continue
            if not _CAP_LEAD_RE.match(e["caption"]):
                continue
            en = e["caption_norm"]
            # 双向子串,且要求索引 caption 足够长,避免碎 caption 误配
            if len(en) >= 8 and (en in cap_norm or cap_norm in en):
                if en in seen_captions:
                    continue
                seen_captions.add(en)
                seen_tokens.add(e["token"])
                found.append({"token": e["token"], "caption": e["caption"],
                              "doc": e["doc"]})
                if len(found) >= limit:
                    return found
    return found


def resolve_token(token: str) -> Path | None:
    """token → 图片绝对路径(不存在返回 None)。"""
    _, by_token = _load()
    p = by_token.get(token)
    if not p:
        return None
    path = Path(p)
    return path if path.is_file() else None
