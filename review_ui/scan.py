"""
Headless scan: compares original (backup) vs target project and prints stats.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from review_ui.cache import ReviewCache
from review_ui.config import load_config
from review_ui.scanner import (
    STATUS_EMOJI,
    STATUS_LABEL,
    LineStatus,
    collect_files,
    scan_file,
)


# ASCII-safe status labels for console output
_ASCII_LABEL = {
    "ORIGINAL": "[ORIG]",
    "TRANSLATED": "[TRNS]",
    "LLM_TWEAKED": "[LLM]",
    "USER_MODIFIED": "[USER]",
    "BROKEN": "[BRKN]",
    "UNKNOWN": "[UNKN]",
}

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    cfg = load_config()

    print(f"Original: {cfg.original_root}")
    print(f"Target:   {cfg.target_root}")
    print(f"Source:   {cfg.source_lang}  ->  {cfg.target_lang}")
    print()

    if not cfg.original_root.exists():
        print(f"ERROR: Original root not found: {cfg.original_root}")
        sys.exit(1)
    if not cfg.target_root.exists():
        print(f"ERROR: Target root not found: {cfg.target_root}")
        sys.exit(1)

    cache = ReviewCache(cfg.cache_dir)
    files = collect_files(cfg)
    total = len(files)

    print(f"Files to scan: {total}")
    print(f"{'='*60}")
    print(f"{'Status':>10}  {'Lines':>6}  File")
    print(f"{'-'*60}")

    status_totals: dict[str, int] = {s.name: 0 for s in LineStatus}
    file_count = 0

    t0 = time.time()

    for idx, fpath in enumerate(files):
        try:
            result = scan_file(fpath, cfg.original_root, cfg, cache.llm_cache, cache.user_cache)
        except Exception:
            continue
        if not result.strings:
            continue

        file_count += 1

        file_statuses: dict[str, int] = {}
        for ts in result.strings:
            file_statuses[ts.status.name] = file_statuses.get(ts.status.name, 0) + 1
            status_totals[ts.status.name] = status_totals.get(ts.status.name, 0) + 1

        summary = " ".join(
            f"{_ASCII_LABEL.get(s, '?')}{c}"
            for s, c in sorted(file_statuses.items())
        )
        print(f"{summary:>22}  {result.file_rel}")

        if (idx + 1) % 500 == 0:
            elapsed = time.time() - t0
            print(f"  ... {idx+1}/{total} files scanned ({elapsed:.1f}s)", file=sys.stderr)

    elapsed = time.time() - t0

    print(f"{'='*60}")
    print(f"Files with translatable strings: {file_count} / {total}")
    print(f"Total translatable strings:      {sum(status_totals.values())}")
    for s in LineStatus:
        c = status_totals.get(s.name, 0)
        if c > 0:
            print(f"  {_ASCII_LABEL.get(s.name, '?'):>6} {c:>6}")
    print(f"Time: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
