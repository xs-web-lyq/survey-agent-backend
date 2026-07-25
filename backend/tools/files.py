"""工作区文件工具:路径边界强制校验的 read/write/list。

综述 agent 只允许在 workspace/<task_id>/ 内读写。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from backend.config import settings


class WorkspaceFS:
    """单任务工作区文件系统(所有路径相对于任务根目录)。"""

    def __init__(self, task_id: str):
        self.root = (settings.workspace_dir / task_id).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel_path: str) -> Path:
        p = (self.root / rel_path).resolve()
        if not p.is_relative_to(self.root):
            raise PermissionError(f"路径越界: {rel_path}")
        return p

    def write(self, rel_path: str, content: str) -> str:
        p = self._resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return rel_path

    def write_atomic(self, rel_path: str, content: str) -> str:
        """在同一目录写临时文件后原子替换，避免检查点只写入一半。"""
        p = self._resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=p.parent,
                prefix=f".{p.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, p)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
        return rel_path

    def read(self, rel_path: str) -> str:
        return self._resolve(rel_path).read_text(encoding="utf-8")

    def move(self, source: str, destination: str) -> str:
        """在任务工作区内移动文件；用于归档已经失效但仍需保留的检查点。"""
        src = self._resolve(source)
        dst = self._resolve(destination)
        if not src.exists():
            raise FileNotFoundError(source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return destination

    def exists(self, rel_path: str) -> bool:
        try:
            return self._resolve(rel_path).exists()
        except PermissionError:
            return False

    def append(self, rel_path: str, content: str) -> str:
        p = self._resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return rel_path

    def list(self, rel_dir: str = ".") -> list[dict]:
        d = self._resolve(rel_dir)
        if not d.exists():
            return []
        out = []
        for p in sorted(d.rglob("*")):
            if p.is_file():
                out.append({
                    "path": str(p.relative_to(self.root)).replace("\\", "/"),
                    "size": p.stat().st_size,
                })
        return out
