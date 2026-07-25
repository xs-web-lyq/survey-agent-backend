import asyncio
import json
from types import SimpleNamespace

from backend import llm
from backend.config import settings
from backend.health import admin_access_status, readiness_snapshot, runtime_health


def test_readiness_reports_components_without_internal_paths(monkeypatch):
    from backend import health

    monkeypatch.setattr(health.db, "database_status", lambda: {
        "ok": True, "schema_version": 2,
    })
    monkeypatch.setattr(type(settings), "validate_paths", lambda self: [])
    runtime_health.set_rag("ready")
    ready, components = readiness_snapshot()
    assert ready is True

    monkeypatch.setattr(
        type(settings), "validate_paths", lambda self: ["secret/path/missing"],
    )
    ready, components = readiness_snapshot()
    assert ready is False
    assert components["paths"] == {"ok": False, "missing_count": 1}
    assert "secret/path" not in json.dumps(components)


def test_production_preflight_requires_admin_token(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "admin_token", "")
    allowed, status, _ = admin_access_status(None)
    assert allowed is False
    assert status == 403

    monkeypatch.setattr(settings, "admin_token", "expected")
    allowed, status, _ = admin_access_status("expected")
    assert allowed is True
    assert status == 200


def test_model_preflight_uses_minimal_completion(monkeypatch):
    observed = {}

    class Completions:
        async def create(self, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(settings, "llm_binding_type", "openai")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    monkeypatch.setattr(llm, "_openai_client", lambda: client)
    result = asyncio.run(llm.preflight(timeout_seconds=1))
    assert result["ok"] is True
    assert result["model"] == "test-model"
    assert observed["max_tokens"] == 4
