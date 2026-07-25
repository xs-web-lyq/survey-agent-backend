import asyncio
from types import SimpleNamespace

from backend import rag_client
from backend.tools import retrieval


def _candidate(chunk_id: str, source: str, score: float) -> dict:
    return {
        "__id__": chunk_id,
        "file_path": rf"D:\kb\{source}",
        "content": f"evidence from {source}",
        "distance": score,
    }


def test_scoped_search_uses_scope_first_candidates(monkeypatch):
    async def fake_aquery_data(*_args, **_kwargs):
        raise AssertionError("scoped search must not depend on LLM-backed aquery_data")

    async def fake_scoped_query(*_args, **_kwargs):
        return [
            _candidate("a-1", "paper-a.pdf", 0.91),
            _candidate("a-2", "paper-a.pdf", 0.88),
            _candidate("b-1", "paper-b.pdf", 0.84),
        ]

    monkeypatch.setattr(retrieval.rag_client, "aquery_data", fake_aquery_data)
    monkeypatch.setattr(
        retrieval.rag_client, "query_chunks_scoped", fake_scoped_query
    )

    result = asyncio.run(
        retrieval.search_evidence(
            "mold electromagnetic stirring",
            chunk_top_k=3,
            allowed_sources={"paper-a.pdf", "paper-b.pdf"},
        )
    )

    assert [chunk["chunk_id"] for chunk in result["chunks"]] == [
        "a-1",
        "b-1",
        "a-2",
    ]
    assert {chunk["source"] for chunk in result["chunks"]} == {
        "paper-a.pdf",
        "paper-b.pdf",
    }
    assert result["chunks"][0]["score"] == 0.91


def test_unrestricted_search_keeps_lightrag_order(monkeypatch):
    async def fake_aquery_data(*_args, **_kwargs):
        return {
            "data": {
                "chunks": [
                    _candidate("first", "paper-a.pdf", 0.9),
                    _candidate("second", "paper-a.pdf", 0.8),
                ],
                "entities": [],
                "relationships": [],
            }
        }

    async def scoped_query_must_not_run(*_args, **_kwargs):
        raise AssertionError("unrestricted search must not use scoped retrieval")

    monkeypatch.setattr(retrieval.rag_client, "aquery_data", fake_aquery_data)
    monkeypatch.setattr(
        retrieval.rag_client, "query_chunks_scoped", scoped_query_must_not_run
    )

    result = asyncio.run(
        retrieval.search_evidence("mold electromagnetic stirring", chunk_top_k=2)
    )

    assert [chunk["chunk_id"] for chunk in result["chunks"]] == [
        "first",
        "second",
    ]


def test_rag_adapter_filters_before_vector_ranking(monkeypatch):
    items = [
        _candidate("inside", "paper-a.pdf", 0.81),
        _candidate("outside", "unrelated.pdf", 0.99),
    ]
    observed = {}

    class FakeClient:
        def query(self, **kwargs):
            observed.update(kwargs)
            return [
                {**item, "__metrics__": item["distance"]}
                for item in items
                if kwargs["filter_lambda"](item)
            ]

    async def fake_embedding(_texts, **_kwargs):
        return [[0.1, 0.2]]

    async def fake_get_client():
        return FakeClient()

    fake_vdb = SimpleNamespace(
        embedding_func=fake_embedding,
        _get_client=fake_get_client,
        cosine_better_than_threshold=0.2,
    )

    async def fake_get_rag():
        return SimpleNamespace(
            lightrag=SimpleNamespace(chunks_vdb=fake_vdb)
        )

    monkeypatch.setattr(rag_client, "get_rag", fake_get_rag)

    result = asyncio.run(
        rag_client.query_chunks_scoped(
            "mold electromagnetic stirring",
            allowed_sources={"paper-a.pdf"},
            top_k=4,
        )
    )

    assert [chunk["id"] for chunk in result] == ["inside"]
    assert result[0]["distance"] == 0.81
    assert observed["top_k"] == 4
