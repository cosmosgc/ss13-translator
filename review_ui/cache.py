from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any


# DM text macros that must be preserved (from translate_ss13.py)
_DM_MACRO_RE = re.compile(r'\\(?:[Tt]hem(?:selves)?|[Tt]heir|[Tt]he|[Hh]im(?:self)?|[Hh]e(?:self)?|[Hh]is|[Aa]n|[Aa]|[Ii]tself|[Oo]urselves|[Yy]ourselves)')
# Standard escape sequences
_DM_ESCAPE_RE = re.compile(r'\\[nrt"\'\\]')
# Printf-style format specifiers
_DM_PRINTF_RE = re.compile(r'%(?:\d+\$)?[sdif]|%[sdif]')
# Combined token pattern — DM macros first so \the isn't split as \t
_DM_TOKEN_RE = re.compile(
    r'\[.*?\]'
    r'|\\(?:[Tt]hem(?:selves)?|[Tt]heir|[Tt]he|[Hh]im(?:self)?|[Hh]e(?:self)?|[Hh]is|[Aa]n|[Aa]|[Ii]tself|[Oo]urselves|[Yy]ourselves)'
    r'|\\[nrt"\'\\]'
    r'|%(?:\d+\$)?[sdif]|%[sdif]'
)


def _restore_original_tokens(original: str, translation: str) -> str:
    """Restore original DM tokens (brackets, macros, escapes, printf) if the model changed them.

    Uses a consume-based approach: for each original token, consumes an instance from the
    translation (matching by text) or finds a replacement token. Unmatched tokens are appended.
    """
    if not original or not translation:
        return translation
    from collections import Counter

    result = translation
    orig_tokens = _DM_TOKEN_RE.findall(original)
    trans_tokens = _DM_TOKEN_RE.findall(result)

    # Count how many of each token we need vs have
    need = Counter(orig_tokens)
    have = Counter(trans_tokens)

    # Tokens that appear in translation but NOT in original — candidates for replacement
    changed = [t for t in trans_tokens if t not in need]

    for orig_tok in orig_tokens:
        if need[orig_tok] <= 0:
            continue  # already fully accounted for
        if have.get(orig_tok, 0) > 0:
            # Consume one matching instance from translation
            have[orig_tok] -= 1
            need[orig_tok] -= 1
            continue
        # Token is missing from translation — try to replace a changed token
        replaced = False
        for i, changed_tok in enumerate(changed):
            if changed_tok in result and changed_tok != orig_tok:
                result = result.replace(changed_tok, orig_tok, 1)
                changed.pop(i)
                need[orig_tok] -= 1
                replaced = True
                break
        if not replaced:
            # Don't append escape sequences — the model likely convered them to bare chars
            if orig_tok.startswith("\\") and len(orig_tok) == 2:
                continue
            # Append at end as fallback
            result = result.rstrip() + " " + orig_tok
            need[orig_tok] -= 1

    return result


class ReviewCache:
    """Manages LLM translation cache and user-edit cache."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # LLM translation cache: source_text -> translated_text
        self.llm_cache: dict[str, str] = {}
        self._llm_path = cache_dir / "llm_cache.json"

        # User edit cache: "file_rel:lineno:content" -> edited_content
        self.user_cache: dict[str, str] = {}
        self._user_path = cache_dir / "user_cache.json"

        # File scan results cache: file_rel -> list of serialized strings
        self.scan_cache: dict[str, list[dict[str, Any]]] = {}
        self._scan_path = cache_dir / "scan_cache.json"

        self.load()

    def load(self) -> None:
        self._load_json(self._llm_path, self.llm_cache)
        self._load_json(self._user_path, self.user_cache)
        self._load_json(self._scan_path, self.scan_cache)

    def save(self) -> None:
        self._save_json(self._llm_path, self.llm_cache)
        self._save_json(self._user_path, self.user_cache)
        self._save_json(self._scan_path, self.scan_cache)

    def _load_json(self, path: Path, target: dict) -> None:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    target.update(data)
            except Exception:
                pass

    def _save_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_llm_translation(self, source: str) -> str | None:
        with self._lock:
            raw = self.llm_cache.get(source)
            if raw is None:
                return None
            fixed = _restore_original_tokens(source, raw)
            if fixed != raw:
                self.llm_cache[source] = fixed
            return fixed

    def set_llm_translation(self, source: str, translation: str) -> None:
        with self._lock:
            self.llm_cache[source] = _restore_original_tokens(source, translation)

    def get_user_edit(self, key: str) -> str | None:
        with self._lock:
            return self.user_cache.get(key)

    def set_user_edit(self, key: str, value: str) -> None:
        with self._lock:
            self.user_cache[key] = value

    def get_scan(self, file_rel: str) -> list[dict[str, Any]] | None:
        with self._lock:
            return self.scan_cache.get(file_rel)

    def set_scan(self, file_rel: str, data: list[dict[str, Any]]) -> None:
        with self._lock:
            self.scan_cache[file_rel] = data
