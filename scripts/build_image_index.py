"""离线构建 caption → 图片路径 索引。

扫描 MinerU 解析产物(content_list.json)中的 image/chart/table 块,
提取 caption 文本与 img_path 的映射,输出 data/image_index.json:

    {
      "entries": [
        {"caption": "图1.1 连铸工艺流程示意图", "caption_norm": "...",
         "img": "<解析目录相对路径>/images/xxx.jpg", "doc": "<解析目录名>"}
      ]
    }

用法(在 survey_agent 根目录):
    python scripts/build_image_index.py

依赖 .env 的 PARSER_OUTPUT_DIRS(分号分隔的多个 MinerU 输出根目录)。
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings  # noqa: E402


def norm_caption(text: str) -> str:
    """caption 归一化:去空白/全半角统一,用于匹配。"""
    text = re.sub(r"\s+", "", text)
    text = text.replace("．", ".").replace("（", "(").replace("）", ")")
    return text.lower()


def extract_img_and_captions(b: dict) -> tuple[str, list[str]]:
    """兼容 MinerU v1 与 v2 两种 content_list schema。

    v1: {"type": "image", "img_path": "...", "image_caption": ["文本", ...]}
    v2: {"type": "image", "content": {"image_source": {"path": "..."},
         "image_caption": [{"type": "text", "content": "文本"}, ...]}}
    """
    # v1 扁平结构
    img_path = b.get("img_path", "")
    captions_raw = b.get("image_caption") or b.get("table_caption") or []
    # v2 嵌套结构
    if not img_path and isinstance(b.get("content"), dict):
        inner = b["content"]
        img_path = (inner.get("image_source") or {}).get("path", "") \
            or (inner.get("table_source") or {}).get("path", "")
        captions_raw = inner.get("image_caption") or inner.get("table_caption") or []
    captions = []
    for cap in captions_raw:
        if isinstance(cap, dict):
            cap = cap.get("content", "")
        cap = (cap or "").strip()
        if cap:
            captions.append(cap)
    return img_path, captions


def build() -> None:
    roots = settings.parser_output_dirs
    if not roots:
        print("PARSER_OUTPUT_DIRS 未配置,退出")
        return

    entries = []
    seen_files = 0
    for root in roots:
        root = Path(root)
        if not root.exists():
            print(f"跳过不存在的目录: {root}")
            continue
        # 同目录 v1 与 v2 都扫(schema 兼容),条目按 (caption, img) 去重
        cl_list = list(root.rglob("*_content_list*.json"))
        for cl in cl_list:
            seen_files += 1
            try:
                blocks = json.loads(cl.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            # _v2 版本按页分组(list of list),展平
            flat: list = []
            for b in blocks if isinstance(blocks, list) else []:
                if isinstance(b, list):
                    flat.extend(x for x in b if isinstance(x, dict))
                elif isinstance(b, dict):
                    flat.append(b)
            img_dir = cl.parent  # images/ 相对于 content_list 所在目录
            doc_name = cl.parent.parent.name  # mineru_v4_result 的上级 = 论文目录
            for b in flat:
                if b.get("type") not in ("image", "chart", "table"):
                    continue
                img_path, captions = extract_img_and_captions(b)
                if not img_path or not captions:
                    continue
                abs_img = img_dir / img_path
                if not abs_img.exists():
                    continue
                for cap in captions:
                    if len(cap) < 4:
                        continue
                    entries.append({
                        "caption": cap,
                        "caption_norm": norm_caption(cap),
                        # 存绝对路径的 POSIX 形式;服务端校验后再发
                        "img": str(abs_img.resolve()).replace("\\", "/"),
                        "doc": doc_name,
                    })

    out = settings.data_dir / "image_index.json"
    # (caption_norm, img) 去重
    seen: set[tuple[str, str]] = set()
    deduped = []
    for e in entries:
        key = (e["caption_norm"], e["img"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    out.write_text(
        json.dumps({"entries": deduped}, ensure_ascii=False), encoding="utf-8"
    )
    print(f"扫描 content_list: {seen_files} 个")
    print(f"索引条目: {len(deduped)}(caption→图片,去重后)")
    print(f"输出: {out}")


if __name__ == "__main__":
    build()
