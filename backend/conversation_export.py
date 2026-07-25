"""Markdown export for a single persisted conversation."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _date(value: float | None) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M")


def conversation_to_markdown(conversation: dict[str, Any]) -> str:
    """Render a transcript with citations and auditable message metadata."""
    title = (conversation.get("title") or "未命名对话").strip()
    lines = [
        f"# {title}", "",
        f"- 会话 ID：`{conversation.get('id', '')}`",
        f"- 知识库：`{conversation.get('kb_name', '')}`",
        f"- 创建时间：{_date(conversation.get('created_at'))}", "",
        "---", "",
    ]
    for index, message in enumerate(conversation.get("messages") or [], start=1):
        role = "用户" if message.get("role") == "user" else "助手"
        lines.extend([f"## {index}. {role}", "", str(message.get("content") or "").rstrip(), ""])
        if message.get("role") == "assistant":
            route = message.get("route_used") or message.get("route_requested")
            model = message.get("model")
            if route or model:
                details = []
                if route:
                    details.append(f"链路：{route}")
                if model:
                    details.append(f"模型：{model}")
                lines.extend([f"> {' · '.join(details)}", ""])
            citations = message.get("citations") or []
            if citations:
                lines.extend(["### 引用", ""])
                for citation in citations:
                    source = citation.get("source") or "未知来源"
                    page = citation.get("page_range") or {}
                    page_label = page.get("label") if isinstance(page, dict) else None
                    suffix = f"（{page_label}）" if page_label else ""
                    lines.append(
                        f"{citation.get('n', '')}. **{source}**{suffix} "
                        f"`{citation.get('chunk_id', '')}`"
                    )
                    preview = str(citation.get("preview") or "").strip()
                    if preview:
                        lines.append(f"   > {preview.replace(chr(10), ' ')}")
                lines.append("")
        lines.extend(["---", ""])
    return "\n".join(lines).rstrip() + "\n"
