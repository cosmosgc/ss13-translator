from __future__ import annotations

import json
import threading
from pathlib import Path


class TranslateCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        self.translations: dict[str, str] = {}
        self._trans_path = cache_dir / "translations.json"

        self.file_hashes: dict[str, str] = {}
        self._hash_path = cache_dir / "file_hashes.json"

        self.scan_results: dict[str, list[dict]] = {}
        self._results_path = cache_dir / "scan_results.json"

        self.load()

    def load(self) -> None:
        self._load_json(self._trans_path, self.translations)
        self._load_json(self._hash_path, self.file_hashes)
        self._load_json(self._results_path, self.scan_results)

    def save(self) -> None:
        self._save_json(self._trans_path, self.translations)
        self._save_json(self._hash_path, self.file_hashes)
        self._save_json(self._results_path, self.scan_results)

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

    def get_translation(self, source_key: str) -> str | None:
        with self._lock:
            return self.translations.get(source_key)

    def set_translation(self, source_key: str, translation: str) -> None:
        with self._lock:
            self.translations[source_key] = translation

    def get_file_hash(self, rel: str) -> str | None:
        with self._lock:
            return self.file_hashes.get(rel)

    def set_file_hash(self, rel: str, hash_val: str) -> None:
        with self._lock:
            self.file_hashes[rel] = hash_val

    def get_scan_result(self, rel: str) -> list[dict] | None:
        with self._lock:
            return self.scan_results.get(rel)

    def set_scan_result(self, rel: str, data: list[dict]) -> None:
        with self._lock:
            self.scan_results[rel] = data
