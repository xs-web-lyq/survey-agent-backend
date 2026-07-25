"""启动自检:验证配置路径与知识库连通性(无需 LLM/embedding)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings

print("KB:", settings.kb_name)
errs = settings.validate_paths()
print("路径校验:", "全部通过" if not errs else errs)

import asyncio

from backend.rag_client import list_documents

docs = asyncio.run(list_documents())
print(f"文献数: {len(docs)}")
for d in docs[:3]:
    name = d["file_path"].replace("\\", "/").split("/")[-1]
    print(" -", name[:70], f"({d['chunks_count']} chunks)")
