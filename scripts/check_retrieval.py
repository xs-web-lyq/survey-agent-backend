"""完整初始化自检:加载 embedding + LightRAG,执行一次真实检索(不调 LLM)。"""
import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from backend.rag_client import aquery_data

    print("初始化 RAG 并执行结构化检索(mode=mix, 不生成回答)...")
    result = await aquery_data("电磁搅拌对等轴晶率的影响", mode="mix", top_k=10, chunk_top_k=5)
    data = result.get("data", {})
    chunks = data.get("chunks", [])
    refs = data.get("references", [])
    print(f"status: {result.get('status')}")
    print(f"chunks: {len(chunks)}, entities: {len(data.get('entities', []))}, "
          f"relations: {len(data.get('relationships', []))}, references: {len(refs)}")
    for c in chunks[:3]:
        fp = (c.get("file_path") or "").replace("\\", "/").split("/")[-1]
        print(f" - [{c.get('reference_id')}] {fp[:50]} :: {c.get('content', '')[:60]}...")


asyncio.run(main())
