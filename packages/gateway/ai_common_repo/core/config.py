"""Typed, layered configuration.

Resolution order (last wins):
  1. packaged configs/default.yaml
  2. user config (AICR_CONFIG env, or ~/.config/ai-common-repo/config.yaml)
  3. AICR_* environment scalar overrides (AICR_SERVER__PORT=29000)

Nothing in the codebase reads os.environ or literals for tunables — they all
flow through `AppConfig`. This is the single source of truth.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from . import constants as C


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 28787


class StorageConfig(BaseModel):
    db_path: str = "~/.local/share/ai-common-repo/memory.db"
    raw_log_dir: str = "~/.local/share/ai-common-repo/sessions"
    # auto | bruteforce | sqlite-vec (see constants.VECTOR_BACKEND_*).
    vector_backend: str = "auto"

    @property
    def db_file(self) -> Path:
        return _expand(self.db_path)

    @property
    def raw_dir(self) -> Path:
        return _expand(self.raw_log_dir)


class IdentityConfig(BaseModel):
    default_user_id: str = "default"


class BackendConfig(BaseModel):
    provider: str = "openai_compatible"
    base_url: str
    api_key: str = "EMPTY"
    chat_model: str | None = None
    embedding_provider: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    timeout_s: float = 60.0


class ModelsConfig(BaseModel):
    default_backend: str = "local-ollama"
    backends: dict[str, BackendConfig] = Field(default_factory=dict)
    tasks: dict[str, str] = Field(default_factory=dict)

    def backend_for(self, task: str) -> str:
        return self.tasks.get(task, self.default_backend)


class EmbeddingConfig(BaseModel):
    # Vector dimension used by the offline hash-embedding fallback. The OpenAI-
    # compatible backend ignores this (its model fixes the dimension).
    dim: int = 256
    # Reciprocal-rank-fusion constant for hybrid search (RRF k).
    rrf_k: int = 60


class DocumentsConfig(BaseModel):
    # Ingest guards: reject oversized / over-chunked documents up front.
    max_content_chars: int = 500_000
    max_chunks: int = 2_000
    chunk_max_chars: int = 1_200
    chunk_overlap_chars: int = 150
    # Roots a file-path ingest may read from (expanded). Empty => path ingest
    # disabled (only inline content accepted).
    allowed_roots: list[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    max_context_items: int = 12
    max_context_chars: int = 12000
    profile_max_chars: int = 3000
    recent_max_items: int = 5
    enable_conflict_resolution: bool = True
    auto_extract: bool = True
    allow_heuristic_fallback: bool = True
    # When true, ingest defers extraction/profile rebuild to the job queue.
    # When false (default), those run inline so the gateway works without a
    # running worker (tests / offline).
    async_jobs: bool = False
    # Auto-forget: memories past valid_until (or below the confidence floor) are
    # expired by a periodic job. floor=0 disables confidence-based forgetting.
    forget_enabled: bool = True
    forget_confidence_floor: float = 0.0
    forget_interval_s: int = 3600
    # Include document chunks (not just memories) in recall/search fusion.
    include_documents: bool = True


class WorkersConfig(BaseModel):
    # Off by default: the gateway only spawns background threads when explicitly
    # enabled (server lifespan / standalone `worker` entrypoint).
    enabled: bool = False
    poll_interval_s: float = 0.5
    max_attempts: int = 3
    backoff_base_s: float = 1.0
    backoff_max_s: float = 300.0


class GithubConnectorConfig(BaseModel):
    enabled: bool = False
    # Token is read from this env var at sync time only — never persisted.
    token_env: str = "GITHUB_TOKEN"
    base_url: str = "https://api.github.com"
    repos: list[str] = Field(default_factory=list)  # "owner/name"
    user_id: str | None = None
    project_id: str | None = None
    poll_interval_s: int = 900
    # Per-resource page cap so a first sync of a huge repo stays bounded.
    max_items_per_sync: int = 50
    timeout_s: float = 20.0

    @property
    def allowed_hosts(self) -> list[str]:
        return _hosts_of(self.base_url)


class NotionConnectorConfig(BaseModel):
    enabled: bool = False
    token_env: str = "NOTION_TOKEN"
    base_url: str = "https://api.notion.com"
    databases: list[str] = Field(default_factory=list)
    user_id: str | None = None
    project_id: str | None = None
    poll_interval_s: int = 900
    max_items_per_sync: int = 50
    timeout_s: float = 20.0
    api_version: str = "2022-06-28"

    @property
    def allowed_hosts(self) -> list[str]:
        return _hosts_of(self.base_url)


class ConnectorsConfig(BaseModel):
    github: GithubConnectorConfig = Field(default_factory=GithubConnectorConfig)
    notion: NotionConnectorConfig = Field(default_factory=NotionConnectorConfig)


class HarnessConfig(BaseModel):
    # Executor namespace for the contract-inbox queue (HARNESS_COMMON_SPEC §4.1).
    # Tasks may only target these names; each harness runs a pull worker for its
    # own name. "agy" is reserved ahead of a use decision.
    executors: list[str] = Field(
        default_factory=lambda: ["claudecode", "openclaw", "opencode", "agy"])
    default_timeout_s: int = 1800
    # A claimed/running task whose claim is older than stale_multiplier×timeout_s
    # returns to pending (at most max_requeues times) so a dead runner cannot
    # strand it. There are no automatic retries beyond that.
    stale_multiplier: int = 2
    max_requeues: int = 1


class PrivacyConfig(BaseModel):
    mode: str = "local-first"
    redact_secrets: bool = True
    external_model_requires_redaction: bool = True
    store_raw_prompts: bool = True
    store_raw_outputs: bool = False
    raw_session_retention_days: int = 14
    deny_paths: list[str] = Field(default_factory=list)
    secret_patterns: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    adapters: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------
def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def _hosts_of(url: str) -> list[str]:
    from urllib.parse import urlparse
    host = urlparse(url).hostname
    return [host] if host else []


def _packaged_default_path() -> Path:
    # configs/default.yaml at repo root (three levels up from this file's pkg).
    here = Path(__file__).resolve()
    repo_root = here.parents[4]  # .../ai-common-repo
    return repo_root / "configs" / C.PACKAGED_DEFAULT_CONFIG


def _user_config_path() -> Path:
    explicit = os.environ.get(C.ENV_CONFIG_PATH)
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(os.path.expanduser(base)) / "ai-common-repo" / C.DEFAULT_CONFIG_FILENAME


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _env_overrides() -> dict[str, Any]:
    """AICR_SERVER__PORT=29000 -> {'server': {'port': 29000}}."""
    out: dict[str, Any] = {}
    for key, val in os.environ.items():
        if not key.startswith(C.ENV_OVERRIDE_PREFIX) or key == C.ENV_CONFIG_PATH:
            continue
        path = key[len(C.ENV_OVERRIDE_PREFIX):].lower().split("__")
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = _coerce(val)
    return out


def load_config(extra_path: str | os.PathLike | None = None) -> AppConfig:
    merged = _read_yaml(_packaged_default_path())
    merged = _deep_merge(merged, _read_yaml(_user_config_path()))
    if extra_path:
        merged = _deep_merge(merged, _read_yaml(Path(extra_path)))
    merged = _deep_merge(merged, _env_overrides())
    return AppConfig.model_validate(merged)
