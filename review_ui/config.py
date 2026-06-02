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
    cache_dir: Path = Path("./.cache/review_ui")
    llm_api_base: str = "http://127.0.0.1:1234/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "local-model"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 6144
    llm_timeout: int = 240


def load_config() -> Config:
    script_dir = Path(__file__).resolve().parent
    parent_dir = script_dir.parent

    dotenv_path = parent_dir / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)

    load_dotenv()

    original_root = Path(os.getenv("ORIGINAL_ROOT", "")).resolve()
    target_root = Path(os.getenv("TARGET_ROOT", "")).resolve()

    if not original_root.exists():
        original_root = Path(os.getenv("PROJECT_ROOT", str(parent_dir.parent))).resolve()
    if not target_root.exists():
        target_root = Path(os.getenv("PROJECT_ROOT", str(parent_dir.parent))).resolve()

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

    cache_dir = Path(os.getenv("CACHE_DIR", str(script_dir / ".cache" / "review_ui"))).resolve()

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
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "6144")),
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "240")),
    )
