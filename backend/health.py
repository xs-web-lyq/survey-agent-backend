"""Process-level runtime health state shared by lifecycle and probes."""

from __future__ import annotations

import time
import hmac
from dataclasses import dataclass, field

from backend import db
from backend.config import settings


@dataclass
class RuntimeHealth:
    started_at: float = field(default_factory=time.time)
    rag_status: str = "not_started"
    rag_error_code: str = ""
    rag_checked_at: float | None = None

    def set_rag(self, status: str, error_code: str = "") -> None:
        self.rag_status = status
        self.rag_error_code = error_code
        self.rag_checked_at = time.time()


runtime_health = RuntimeHealth()


def readiness_snapshot() -> tuple[bool, dict]:
    database = db.database_status()
    path_errors = settings.validate_paths()
    config_ok = not (settings.is_production and "*" in settings.cors_origins)
    rag_ok = runtime_health.rag_status in {"ready", "deferred"}
    components = {
        "database": database,
        "paths": {"ok": not path_errors, "missing_count": len(path_errors)},
        "rag": {
            "ok": rag_ok,
            "status": runtime_health.rag_status,
            "error_code": runtime_health.rag_error_code or None,
        },
        "configuration": {"ok": config_ok},
    }
    ready = all(bool(component["ok"]) for component in components.values())
    return ready, components


def admin_access_status(token: str | None) -> tuple[bool, int, str]:
    if settings.admin_token:
        allowed = bool(token) and hmac.compare_digest(token, settings.admin_token)
        return allowed, 200 if allowed else 401, "" if allowed else "invalid admin token"
    if settings.is_production:
        return False, 403, "model preflight is disabled until ADMIN_TOKEN is set"
    return True, 200, ""
