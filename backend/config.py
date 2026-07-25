"""集中配置:所有路径与凭据的唯一来源。

迁移原则:任何其他模块禁止硬编码路径/URL/key,一律 `from backend.config import settings`。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 工程根目录(本文件位于 backend/ 下)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 外部依赖路径(迁移时改 .env)----
    rag_anything_repo: Path
    rag_storage_dir: Path
    academic_index_path: Path | None = None
    # MinerU 解析产物根目录(分号分隔多个;图片回链用,可留空)
    parser_output_dirs_raw: str = Field(default="", alias="PARSER_OUTPUT_DIRS")

    # ---- LLM ----
    # 协议类型:openai(/chat/completions 兼容端点)| anthropic(messages 协议中转站)
    llm_binding_type: str = "openai"
    llm_binding_api_key: str
    llm_binding_host: str
    llm_model: str = "qwen3.7-plus"
    writer_model: str = ""  # 空 = 复用 llm_model
    llm_max_retries: int = 2
    llm_retry_base_seconds: float = 0.8

    # ---- Embedding(本地 SentenceTransformers)----
    embedding_model_name: str = "BAAI/bge-large-zh-v1.5"
    embedding_dim: int = 1024
    embedding_device: str = "cpu"

    # ---- Conversation memory ----
    memory_recent_messages: int = 8
    memory_compact_after_messages: int = 16
    memory_compact_after_chars: int = 24000
    memory_max_durable_items: int = 5
    memory_idle_extract_seconds: int = 600
    # Deterministic rewriting is fast and handles common follow-ups. Enable the
    # LLM fallback only when the configured provider has acceptable latency.
    memory_llm_rewrite: bool = False

    # ---- 应用目录 ----
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    data_dir: Path = PROJECT_ROOT / "data"
    # 前后端独立仓库部署时可指向外部构建目录。
    frontend_dist_dir: Path = PROJECT_ROOT / "frontend" / "dist"

    # ---- 服务 ----
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    app_env: str = "development"
    debug: bool = False
    startup_rag_warmup: bool = True
    cors_origins_raw: str = Field(
        default="http://127.0.0.1:8000,http://localhost:8000",
        alias="CORS_ORIGINS",
    )
    admin_token: str = ""
    model_preflight_timeout_seconds: float = 15.0

    @property
    def effective_writer_model(self) -> str:
        return self.writer_model or self.llm_model

    @property
    def parser_output_dirs(self) -> list[Path]:
        return [Path(p.strip()) for p in self.parser_output_dirs_raw.split(";")
                if p.strip()]

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins_raw.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def kb_name(self) -> str:
        return self.rag_storage_dir.name

    def ensure_dirs(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def validate_paths(self) -> list[str]:
        """返回缺失路径的错误列表(供启动自检)。"""
        errors = []
        if not self.rag_anything_repo.exists():
            errors.append(f"RAG_ANYTHING_REPO 不存在: {self.rag_anything_repo}")
        if not self.rag_storage_dir.exists():
            errors.append(f"RAG_STORAGE_DIR 不存在: {self.rag_storage_dir}")
        if self.academic_index_path and not self.academic_index_path.exists():
            errors.append(f"ACADEMIC_INDEX_PATH 不存在: {self.academic_index_path}")
        return errors


settings = Settings()
settings.ensure_dirs()
