from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    original_root: Path
    target_root: Path
    source_lang: str = "en"
    target_lang: str = "pt-BR"
    include_exts: set[str] = field(default_factory=lambda: {".dm", ".js", ".jsx", ".ts", ".tsx"})
    exclude_dirs: tuple[str, ...] = ()
    cache_dir: Path = Path("./.cache/llm_translate")
    llm_api_base: str = "http://127.0.0.1:1234/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "local-model"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 8192
    llm_timeout: int = 300


def _resolve_root(key: str, fallback: Path) -> Path:
    raw = os.getenv(key, "")
    if raw:
        p = Path(raw).resolve()
        if p.exists():
            return p
    raw = os.getenv("PROJECT_ROOT", "")
    if raw:
        p = Path(raw).resolve()
        if p.exists():
            return p
    return fallback


def load_config() -> Config:
    script_dir = Path(__file__).resolve().parent
    parent_dir = script_dir.parent

    # Load from local .env first (llm_translate/.env), then from project root (.env)
    local_dotenv = script_dir / ".env"
    if local_dotenv.exists():
        load_dotenv(local_dotenv)

    parent_dotenv = parent_dir / ".env"
    if parent_dotenv.exists():
        load_dotenv(parent_dotenv)

    load_dotenv()

    fallback = Path(parent_dir.parent).resolve()
    original_root = _resolve_root("ORIGINAL_ROOT", fallback)
    target_root = _resolve_root("TARGET_ROOT", fallback)

    include_exts = {
        ext.strip().lower()
        for ext in os.getenv("INCLUDE_EXTENSIONS", ".dm,.js,.jsx,.ts,.tsx").split(",")
        if ext.strip()
    }

    exclude_dirs = tuple(
        d.strip().lower().replace("\\", "/")
        for d in os.getenv("EXCLUDE_DIRS", "").split(",")
        if d.strip()
    )

    cache_dir = Path(os.getenv("CACHE_DIR", str(script_dir / ".cache"))).resolve()

    return Config(
        original_root=original_root,
        target_root=target_root,
        source_lang=os.getenv("SOURCE_LANG", "en"),
        target_lang=os.getenv("TARGET_LANG", "pt-BR"),
        include_exts=include_exts,
        exclude_dirs=exclude_dirs,
        cache_dir=cache_dir,
        llm_api_base=os.getenv("LLM_API_BASE", "http://127.0.0.1:1234/v1").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", "not-needed"),
        llm_model=os.getenv("LLM_MODEL", "local-model"),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "300")),
    )
