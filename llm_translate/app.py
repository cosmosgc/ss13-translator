from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from pathlib import Path

import httpx
from flask import Flask, jsonify, request, render_template

from llm_translate.cache import TranslateCache
from llm_translate.config import load_config

# Reuse review_ui's well-tested scanner logic
from review_ui.scanner import (
    FileResult,
    LineStatus,
    TranslatableString,
    check_variables_safe,
    collect_files,
    scan_file,
)
from review_ui.cache import _restore_original_tokens
from review_ui.llm import (
    _extract_from_message,
    make_llm_config,
    check_llm_connection,
    translate_with_llm,
)

# --- Globals ---

cfg = load_config()
cache = TranslateCache(cfg.cache_dir)
llm_cfg = make_llm_config(cfg)

_lock = threading.Lock()
_file_items: dict[str, FileItem] = {}
_all_strings: dict[str, TranslatableString] = {}
_scan_done = False
_scan_progress = {"total": 0, "done": 0, "phase": ""}

_batch = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": [],
    "cancelled": False,
}

_batch_cancel = threading.Event()

app = Flask(__name__)


# --- Helpers ---

class FileItem:
    def __init__(self, path: Path, result: FileResult):
        self.path = path
        self.result = result
        self.translatable: list[TranslatableString] = result.strings


def _ts_to_dict(ts: TranslatableString) -> dict:
    return {
        "file_rel": ts.file_rel,
        "line_number": ts.line_number,
        "line_text": ts.line_text,
        "quote": ts.quote,
        "content": ts.content,
        "original_content": ts.original_content,
        "start": ts.start,
        "end": ts.end,
        "status": ts.status.name,
    }


def _file_hash(path: Path) -> str:
    try:
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest()[:16]
    except Exception:
        return ""


def _read_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text.splitlines(keepends=True)
    except Exception:
        return []


def _build_join_map(lines: list[str]) -> tuple[list[tuple[int, int]], list[str]]:
    """Build merge groups and joined lines for DM continuation support.
    Returns (merge_groups, joined_lines)."""
    merge_groups: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip("\n\r")
        if stripped.endswith("\\") and i + 1 < len(lines):
            merge_groups.append((i, 2))
            i += 2
        else:
            merge_groups.append((i, 1))
            i += 1

    joined_lines: list[str] = []
    for first, count in merge_groups:
        if count == 1:
            joined_lines.append(lines[first])
        else:
            l1 = lines[first].rstrip("\n\r")
            l2 = lines[first + 1]
            joined_lines.append(l1[:-1] + l2.lstrip() + "\n")
    return merge_groups, joined_lines


def _unjoin_lines(merge_groups: list[tuple[int, int]], joined_lines: list[str], original_lines: list[str]) -> list[str]:
    """Collapse all DM continuations into single lines and remove empties."""
    result: list[str] = []
    for gi, (first, count) in enumerate(merge_groups):
        result.append(joined_lines[gi])
    return [l for l in result if l.strip()] or result[:1]


def _apply_string(
    ts: TranslatableString, translation: str,
    target_root: Path, lines_buffer: list[str],
    merge_groups: list[tuple[int, int]] | None = None,
    joined_lines: list[str] | None = None,
) -> bool:
    """Apply a single translation to the in-memory lines buffer. Returns True on success.
    If merge_groups/joined_lines are provided, uses them (avoids recomputing)."""
    rel = ts.file_rel
    line_idx = ts.line_number - 1

    # If merge groups provided, find the correct joined line index
    if merge_groups is not None and joined_lines is not None:
        jl_idx = None
        for gi, (first, count) in enumerate(merge_groups):
            if first <= line_idx < first + count:
                jl_idx = gi
                break
        if jl_idx is None or jl_idx >= len(joined_lines):
            return False
        line_text = joined_lines[jl_idx].rstrip("\n\r")
        lines_ref = joined_lines
        line_ref_idx = jl_idx
    else:
        if line_idx < 0 or line_idx >= len(lines_buffer):
            return False
        line_text = lines_buffer[line_idx].rstrip("\n\r")
        lines_ref = lines_buffer
        line_ref_idx = line_idx

    safe_translation = _escape_quote(translation, ts.quote)
    old_quoted = ts.quote + ts.content + ts.quote
    new_quoted = ts.quote + safe_translation + ts.quote

    # Try position-based match first
    if 0 <= ts.start < ts.end <= len(line_text):
        actual = line_text[ts.start:ts.end]
        if actual == old_quoted:
            new_line_text = line_text[:ts.start] + new_quoted + line_text[ts.end:]
            eol = lines_ref[line_ref_idx][len(line_text):]
            lines_ref[line_ref_idx] = new_line_text + eol

            with _lock:
                fi = _file_items.get(rel)
                if fi:
                    for ft in fi.translatable:
                        if (ft.line_number == ts.line_number
                                and ft.original_content == ts.original_content):
                            ft.content = translation
                            ft.status = LineStatus.TRANSLATED
                            break
            return True

    # Fallback: content-based search in the line
    idx = line_text.find(old_quoted)
    if idx >= 0:
        new_line_text = line_text[:idx] + new_quoted + line_text[idx + len(old_quoted):]
        eol = lines_ref[line_ref_idx][len(line_text):]
        lines_ref[line_ref_idx] = new_line_text + eol

        with _lock:
            fi = _file_items.get(rel)
            if fi:
                for ft in fi.translatable:
                    if (ft.line_number == ts.line_number
                            and ft.original_content == ts.original_content):
                        ft.content = translation
                        ft.status = LineStatus.TRANSLATED
                        break
        return True

    return False


def _flush_file(rel: str, lines: list[str]) -> int:
    """Write modified lines to disk if any changed. Returns bytes written or 0."""
    abs_path = cfg.target_root / rel
    try:
        old = abs_path.read_text(encoding="utf-8")
    except Exception:
        return 0
    new = "".join(lines)
    if old == new:
        return 0
    abs_path.write_text(new, encoding="utf-8")
    return len(new)


# --- Token-level checks and restoration ---

_TOKEN_RE = re.compile(
    r"\[.*?\]"
    r"|\\(?:[Tt]hem(?:selves)?|[Tt]heir|[Hh]im(?:self)?|[Hh]e(?:self)?|[Hh]is|[Ii]tself|[Oo]urselves|[Yy]ourselves)"
    r"|\\[nrt\"\'\\]"
    r"|<[^>]+>"
    r"|%(?:\d+\$)?[sdif]|%[sdif]"
)




def _escape_quote(text: str, quote: str) -> str:
    """Escape unescaped occurrences of `quote` in text."""
    if quote not in text:
        return text
    escaped_quote = "\\" + quote
    placeholder = "\x00EQ\x00"
    temp = text.replace(escaped_quote, placeholder)
    temp = temp.replace(quote, escaped_quote)
    return temp.replace(placeholder, escaped_quote)


def post_process(original: str, translation: str, quote: str = '"') -> tuple[str, list[str]]:
    """
    Apply all post-processing fixes to the LLM translation.
    Returns (fixed_text, issues_fixed).
    """
    fixes = []
    result = translation

    if not original or not result:
        return result, fixes

    # 1. Restore original brackets, macros, escapes, printf tokens
    restored = _restore_original_tokens(original, result)
    if restored != result:
        result = restored
        fixes.append("tokens_restored")

    # 2. Escape delimiter quote if present in translation
    escaped = _escape_quote(result, quote)
    if escaped != result:
        result = escaped
        fixes.append(f"escaped_{quote}")

    # 3. Strip stray backslash from article macros left in translation
    stripped_backslash = re.sub(r"\\([Tt]he|[Aa]n?)\b", r"\1", result)
    if stripped_backslash != result:
        result = stripped_backslash
        fixes.append("stripped_article_backslash")

    # 4. Fix commas: if original has commas at positions where the
    # translation has a different structure, try to restore originals
    orig_commas = [i for i, c in enumerate(original) if c == ","]
    trans_commas = [i for i, c in enumerate(result) if c == ","]
    if len(orig_commas) > len(trans_commas):
        result = result.rstrip() + ","
        fixes.append("comma_restored")

    # 5. Strip leading/trailing whitespace inside quoted content
    stripped = result.strip()
    if stripped != result and stripped:
        result = stripped
        fixes.append("stripped_whitespace")

    # 6. Verify tokens match exactly
    orig_tokens = _TOKEN_RE.findall(original)
    trans_tokens = _TOKEN_RE.findall(result)
    if orig_tokens != trans_tokens:
        fixes.append("token_mismatch_still_present")

    return result, fixes


# --- LLM batch translation ---

_BATCH_SYSTEM_PROMPT = """You are a translator for a game codebase. Translate from {source_lang} to {target_lang}.

Each line shows either:
  LINE: "ENGLISH_TEXT"
  — translate this to {target_lang}
or:
  LINE: "ENGLISH_TEXT" -> "EXISTING_TRANSLATION"  
  — the existing translation may have errors; fix and improve it using the English as a reference

Rules:
- Translate ONLY the text content between the outer quotes.
- Keep ALL [brackets], HTML <tags>, \\escapes, %tokens, and macros like \\him exactly as-is.
- Article macros like \\The, \\the, \\a, \\an become normal words (remove the backslash).
- Preserve indent and line structure.
- Output each translation on a line prefixed with its line number and colon.

CRITICAL: Output ONLY the numbered translations. No thinking, no explanation."""


def _build_batch_prompt(strings: list[TranslatableString]) -> str:
    lines = []
    for s in strings:
        eng = s.original_content.replace('\\"', '"').replace("\\'", "'")
        cur = s.content.replace('\\"', '"').replace("\\'", "'")
        if eng and cur and cur != eng:
            lines.append(f'{s.line_number}: "{eng}" -> "{cur}"')
        elif eng:
            lines.append(f'{s.line_number}: "{eng}"')
        else:
            lines.append(f'{s.line_number}: "{cur}" (improve this translation)')
    return "\n".join(lines)


def _parse_batch_response(
    response: str, strings: list[TranslatableString],
) -> dict[int, str]:
    """Parse the LLM batch response into {line_number: translation}."""
    results: dict[int, str] = {}
    lines = response.strip().split("\n")
    for line in lines:
        line = line.strip()
        m = re.match(r"^(\d+):\s*", line)
        if not m:
            continue
        num = int(m.group(1))
        rest = line[m.end():]
        # Strip surrounding quotes if present
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ('"', "'", "`"):
            rest = rest[1:-1]
        results[num] = rest
    return results


async def _translate_batch(
    strings: list[TranslatableString],
) -> list[dict]:
    """Translate a batch of strings via LLM. Returns list of result dicts."""
    if not strings:
        return []

    prompt_text = _build_batch_prompt(strings)
    system = _BATCH_SYSTEM_PROMPT.format(
        source_lang=cfg.source_lang, target_lang=cfg.target_lang,
    )

    try:
        async with httpx.AsyncClient(timeout=llm_cfg.timeout) as client:
            resp = await client.post(
                f"{llm_cfg.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm_cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": llm_cfg.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt_text},
                    ],
                    "temperature": llm_cfg.temperature,
                    "max_tokens": llm_cfg.max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            raw = _extract_from_message(msg) or ""
    except Exception:
        return [{"error": "llm_failed"}] * len(strings)

    parsed = _parse_batch_response(raw, strings)

    results = []
    for s in strings:
        raw_trans = parsed.get(s.line_number, "")
        if not raw_trans:
            results.append({
                "line_number": s.line_number,
                "original": s.original_content,
                "translation": None,
                "error": "missing_from_response",
            })
            continue

        # Post-process
        fixed, fixes = post_process(s.original_content, raw_trans, s.quote)

        # Verify safety
        safe, issues = check_variables_safe(s.original_content, fixed)

        results.append({
            "line_number": s.line_number,
            "original": s.original_content,
            "translation": fixed,
            "safe": safe,
            "issues": issues,
            "fixes": fixes,
        })
    return results


async def _translate_single(
    source: str,
) -> str | None:
    """Translate a single string via LLM (with retry)."""
    result = await translate_with_llm(
        source, llm_cfg, cfg.source_lang, cfg.target_lang,
    )
    if result:
        fixed, _ = post_process(source, result)
        return fixed
    return None


# --- Background scan ---

def _run_scan_thread(force: bool = False):
    """Scan target files and diff against original.
    
    If force=False (quick), skips files whose target hash hasn't changed
    and reuses cached scan results. If force=True (hard), re-scans everything.
    """
    global _scan_done, _scan_progress, _file_items, _all_strings

    _scan_progress["phase"] = "listing files..." if force else "quick listing..."
    _scan_progress["total"] = 0
    _scan_progress["done"] = 0
    _scan_done = False

    files = collect_files(cfg)
    _scan_progress["total"] = len(files)
    _scan_progress["phase"] = "hard scan" if force else "quick scan"

    local_items: dict[str, FileItem] = {}
    local_strings: dict[str, TranslatableString] = {}
    FLUSH_INTERVAL = 500

    for idx, fpath in enumerate(files):
        _scan_progress["done"] = idx + 1
        rel = fpath.relative_to(cfg.target_root).as_posix()

        if not force:
            # Quick scan: skip if target hash unchanged and cached results exist
            cur_hash = _file_hash(fpath)
            cached_hash = cache.get_file_hash(rel)
            cached_result = cache.get_scan_result(rel)
            if cached_hash == cur_hash and cached_result:
                _restore_cached_file(rel, cached_result, local_items, local_strings)
                if idx > 0 and idx % FLUSH_INTERVAL == 0:
                    with _lock:
                        _file_items = dict(local_items)
                        _all_strings = dict(local_strings)
                continue

        try:
            result = scan_file(fpath, cfg.original_root, cfg)
        except Exception:
            continue

        if result.strings:
            fi = FileItem(fpath, result)
            local_items[result.file_rel] = fi
            for ts in result.strings:
                key = f"{ts.file_rel}:{ts.line_number}"
                local_strings[key] = ts

            # Cache the scan results
            cache.set_scan_result(rel, [_ts_to_dict(ts) for ts in result.strings])
            cache.set_file_hash(rel, _file_hash(fpath))

        # Periodically flush partial results so UI can show progress
        if idx > 0 and idx % FLUSH_INTERVAL == 0:
            with _lock:
                _file_items = dict(local_items)
                _all_strings = dict(local_strings)

    with _lock:
        _file_items = local_items
        _all_strings = local_strings

    _scan_progress["phase"] = "done"
    _scan_done = True
    cache.save()


def _run_batch_strings(strings: list[TranslatableString]):
    """Translate a specific list of strings and apply directly."""
    if not strings:
        return
    rel = strings[0].file_rel
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_batch_strings_worker(rel, strings))
    finally:
        loop.close()


async def _batch_strings_worker(rel: str, strings: list[TranslatableString]):
    global _batch

    todo = [ts for ts in strings if ts.status != LineStatus.BROKEN]
    if not todo:
        return

    _batch["running"] = True
    _batch["total"] = len(todo)
    _batch["done"] = 0
    _batch["errors"] = []
    _batch["cancelled"] = False
    _batch_cancel.clear()

    raw_lines = _read_file(cfg.target_root / rel)
    if not raw_lines:
        _batch["running"] = False
        return

    # Build join map for DM continuation support
    merge_groups, joined_lines = _build_join_map(raw_lines)
    results = await _translate_batch(todo)

    for r in results:
        if _batch_cancel.is_set():
            _batch["cancelled"] = True
            break
        if r.get("error"):
            _batch["errors"].append(f"L{r['line_number']}: {r['error']}")
            _batch["done"] += 1
            continue

        ts_key = f"{rel}:{r['line_number']}"
        ts = _all_strings.get(ts_key)
        if ts and r["translation"]:
            cache_key = f"{rel}:{ts.line_number}:{ts.original_content}"
            cache.set_translation(cache_key, r["translation"])
            _apply_string(ts, r["translation"], cfg.target_root, raw_lines, merge_groups, joined_lines)

        _batch["done"] += 1

    # Un-join back to original line structure
    final_lines = _unjoin_lines(merge_groups, joined_lines, raw_lines)
    _flush_file(rel, final_lines)
    cache.save()
    _batch["running"] = False


def _load_cached_results() -> bool:
    """Load all cached scan results from disk into memory without scanning."""
    global _scan_done, _scan_progress, _file_items, _all_strings

    if not cache.scan_results:
        return False

    local_items: dict[str, FileItem] = {}
    local_strings: dict[str, TranslatableString] = {}

    for rel, strings_data in cache.scan_results.items():
        if strings_data:
            _restore_cached_file(rel, strings_data, local_items, local_strings)

    with _lock:
        _file_items = local_items
        _all_strings = local_strings

    _scan_progress["phase"] = "done"
    _scan_done = True
    return True


def _restore_cached_file(
    rel: str, cached: list[dict],
    local_items: dict[str, FileItem],
    local_strings: dict[str, TranslatableString],
) -> None:
    """Restore a file's translatable strings from cached scan results."""
    from review_ui.scanner import LineStatus

    ts_list: list[TranslatableString] = []
    for d in cached:
        ts = TranslatableString(
            file_rel=d["file_rel"],
            line_number=d["line_number"],
            line_text=d.get("line_text", ""),
            quote=d["quote"],
            content=d["content"],
            original_content=d["original_content"],
            start=d.get("start", 0),
            end=d.get("end", 0),
            status=LineStatus[d["status"]],
        )
        ts_list.append(ts)
        key = f"{d['file_rel']}:{d['line_number']}"
        local_strings[key] = ts

    from review_ui.scanner import FileResult as ScanFileResult
    fake_result = ScanFileResult(file_rel=rel, strings=ts_list)
    fi = FileItem(cfg.target_root / rel, fake_result)
    local_items[rel] = fi


def _run_batch_file(rel: str):
    """Translate a single file's strings and apply directly."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_batch_file_worker(rel))
    finally:
        loop.close()


def _run_batch_all():
    """Translate all pending files."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_batch_all_worker())
    finally:
        loop.close()


async def _batch_file_worker(rel: str):
    global _batch
    fi = _file_items.get(rel)
    if not fi:
        return

    todo = [
        ts for ts in fi.translatable
        if ts.status != LineStatus.BROKEN
    ]
    if not todo:
        return

    _batch["running"] = True
    _batch["total"] = len(todo)
    _batch["done"] = 0
    _batch["errors"] = []
    _batch["cancelled"] = False
    _batch_cancel.clear()

    raw_lines = _read_file(cfg.target_root / rel)
    if not raw_lines:
        _batch["running"] = False
        return

    merge_groups, joined_lines = _build_join_map(raw_lines)
    results = await _translate_batch(todo)

    for r in results:
        if _batch_cancel.is_set():
            _batch["cancelled"] = True
            break
        if r.get("error"):
            _batch["errors"].append(f"L{r['line_number']}: {r['error']}")
            _batch["done"] += 1
            continue

        ts_key = f"{rel}:{r['line_number']}"
        ts = _all_strings.get(ts_key)
        if ts and r["translation"]:
            cache_key = f"{rel}:{ts.line_number}:{ts.original_content}"
            cache.set_translation(cache_key, r["translation"])
            _apply_string(ts, r["translation"], cfg.target_root, raw_lines, merge_groups, joined_lines)

        _batch["done"] += 1

    final_lines = _unjoin_lines(merge_groups, joined_lines, raw_lines)
    _flush_file(rel, final_lines)
    cache.save()
    _batch["running"] = False


async def _batch_all_worker():
    with _lock:
        rels = list(_file_items.keys())

    for rel in rels:
        if _batch_cancel.is_set():
            _batch["cancelled"] = True
            break
        if _batch.get("running"):
            await asyncio.sleep(0.1)
            continue
        await _batch_file_worker(rel)


# --- Flask Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    counts = {"ORIGINAL": 0, "TRANSLATED": 0, "BROKEN": 0}
    total = 0
    with _lock:
        for ts in _all_strings.values():
            counts[ts.status.name] = counts.get(ts.status.name, 0) + 1
            total += 1

    phase = _scan_progress.get("phase", "")
    scan_mode = "hard" if "hard" in phase else ("quick" if "quick" in phase else "")

    return jsonify({
        "files": len(_file_items),
        "strings": total,
        "counts": counts,
        "scan_done": _scan_done,
        "scan_progress": dict(_scan_progress),
        "scan_mode": scan_mode,
        "source_lang": cfg.source_lang,
        "target_lang": cfg.target_lang,
        "original_root": str(cfg.original_root),
        "target_root": str(cfg.target_root),
        "llm_model": llm_cfg.model,
        "llm_api_base": llm_cfg.api_base,
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(force=True) or {}
    mode = data.get("mode", "quick")
    force = mode == "hard"

    phase = _scan_progress.get("phase", "")
    if phase not in ("done", ""):
        # Scan already running — caller can poll /api/status for completion
        return jsonify({"status": "already_running", "mode": "quick" if "quick" in phase else "hard"})

    thread = threading.Thread(target=_run_scan_thread, kwargs={"force": force}, daemon=True)
    thread.start()
    return jsonify({"status": "started", "mode": mode})


@app.route("/api/refresh-cache", methods=["POST"])
def api_refresh_cache():
    """Reload cached scan results from disk without re-scanning."""
    cache.load()
    ok = _load_cached_results()
    return jsonify({"status": "ok" if ok else "no_cache"})


@app.route("/api/files")
def api_files():
    q = request.args.get("q", "").strip().lower()
    scope = request.args.get("scope", "").strip().lower()
    with _lock:
        items = []
        for rel, fi in sorted(_file_items.items()):
            if q and q not in rel.lower():
                continue
            orig_count = sum(1 for ts in fi.translatable if ts.status == LineStatus.ORIGINAL)
            total = len(fi.translatable)
            if scope == "pending" and orig_count == 0:
                continue
            if scope == "done" and orig_count > 0:
                continue
            items.append({
                "rel": rel,
                "total": total,
                "original": orig_count,
                "translated": total - orig_count,
            })
    return jsonify(items)


@app.route("/api/file/<path:rel>")
def api_file(rel: str):
    with _lock:
        fi = _file_items.get(rel)
    if not fi:
        return jsonify({"error": "not_found"}), 404

    strings = []
    for ts in fi.translatable:
        safe, issues = check_variables_safe(ts.original_content, ts.content)
        strings.append({
            "line_number": ts.line_number,
            "line_text": ts.line_text,
            "quote": ts.quote,
            "content": ts.content,
            "original_content": ts.original_content,
            "status": ts.status.name,
            "safe": safe,
            "issues": issues,
        })
    return jsonify({"file_rel": rel, "strings": strings})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    data = request.get_json(force=True) or {}
    scope_type = data.get("scope", "file")
    scope_value = data.get("value", "")
    global _batch

    if _batch.get("running"):
        return jsonify({"error": "batch_already_running"}), 409

    if scope_type == "file":
        if scope_value not in _file_items:
            return jsonify({"error": "file_not_found"}), 404
        thread = threading.Thread(target=_run_batch_file, args=(scope_value,), daemon=True)
        thread.start()
        return jsonify({"status": "started", "scope": scope_value})

    elif scope_type == "dir":
        prefix = scope_value.replace("\\", "/") + "/"
        with _lock:
            matching = [r for r in _file_items if r.replace("\\", "/").startswith(prefix)]
        if not matching:
            return jsonify({"error": "no_files"}), 404

        def _run_dir():
            for rel in sorted(matching):
                if _batch_cancel.is_set():
                    _batch["cancelled"] = True
                    break
                _run_batch_file(rel)

        thread = threading.Thread(target=_run_dir, daemon=True)
        thread.start()
        return jsonify({"status": "started", "files": matching})

    elif scope_type == "all":
        thread = threading.Thread(target=_run_batch_all, daemon=True)
        thread.start()
        return jsonify({"status": "started"})

    return jsonify({"error": "invalid_scope"}), 400


@app.route("/api/translate/selected", methods=["POST"])
def api_translate_selected():
    """Translate specific selected strings by (file_rel, line_number)."""
    data = request.get_json(force=True) or {}
    items = data.get("items", [])

    if _batch.get("running"):
        return jsonify({"error": "batch_already_running"}), 409
    if not items:
        return jsonify({"error": "no_items"}), 400

    # Resolve each (file_rel, line_number) to a TranslatableString
    strings: list[TranslatableString] = []
    missing_keys: list[str] = []
    with _lock:
        for item in items:
            rel = item.get("file_rel", "")
            lineno = item.get("line_number", 0)
            key = f"{rel}:{lineno}"
            ts = _all_strings.get(key)
            if ts is None:
                # Fallback: look up via _file_items
                fi = _file_items.get(rel)
                if fi:
                    for ft in fi.translatable:
                        if ft.line_number == lineno:
                            ts = ft
                            break
            if ts is None:
                missing_keys.append(f"{key}=not_found")
                continue
            if ts.status == LineStatus.BROKEN:
                missing_keys.append(f"{key}=broken")
                continue
            strings.append(ts)

    if not strings:
        return jsonify({"error": "no_translatable_strings", "keys": missing_keys, "total_in_cache": len(_all_strings), "total_files": len(_file_items)}), 400

    thread = threading.Thread(target=_run_batch_strings, args=(strings,), daemon=True)
    thread.start()
    return jsonify({"status": "started", "count": len(strings)})


@app.route("/api/edit", methods=["POST"])
def api_edit():
    """Save a manual user edit for a specific string."""
    data = request.get_json(force=True) or {}
    file_rel = data.get("file_rel", "")
    line_number = data.get("line_number", 0)
    text = data.get("text", "")

    if not file_rel or not line_number or text is None:
        return jsonify({"error": "missing_fields"}), 400

    with _lock:
        ts = _all_strings.get(f"{file_rel}:{line_number}")
        if not ts:
            return jsonify({"error": "string_not_found"}), 404

    raw_lines = _read_file(cfg.target_root / file_rel)
    if not raw_lines:
        return jsonify({"error": "cannot_read_file"}), 500

    merge_groups, joined_lines = _build_join_map(raw_lines)
    if _apply_string(ts, text, cfg.target_root, raw_lines, merge_groups, joined_lines):
        final_lines = _unjoin_lines(merge_groups, joined_lines, raw_lines)
        _flush_file(file_rel, final_lines)
        cache.save()
        return jsonify({"status": "saved"})
    else:
        return jsonify({"error": "apply_failed"}), 500


@app.route("/api/translate/test", methods=["POST"])
async def api_translate_test():
    """Test-translate a single string (no file write)."""
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "no_text"}), 400

    result = await _translate_single(text)
    if not result:
        return jsonify({"error": "translation_failed"}), 500

    safe, issues = check_variables_safe(text, result)
    return jsonify({
        "original": text,
        "translation": result,
        "safe": safe,
        "issues": issues,
    })


@app.route("/api/progress")
def api_progress():
    with _lock:
        rels = list(_file_items.keys())
        done = sum(
            1 for ts in _all_strings.values()
            if ts.status != LineStatus.ORIGINAL
        )
        total = len(_all_strings)
    return jsonify({
        "batch": dict(_batch),
        "files": len(rels),
        "strings_total": total,
        "strings_done": done,
    })


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip().lower()
    status_filter = request.args.get("status", "").strip().upper()
    if not q and not status_filter:
        return jsonify([])

    results = []
    with _lock:
        for ts in _all_strings.values():
            if q and q not in ts.original_content.lower() and q not in ts.content.lower():
                continue
            if status_filter and ts.status.name != status_filter:
                continue
            safe, issues = check_variables_safe(ts.original_content, ts.content)
            results.append({
                "file_rel": ts.file_rel,
                "line_number": ts.line_number,
                "original": ts.original_content,
                "current": ts.content,
                "status": ts.status.name,
                "safe": safe,
                "issues": issues,
            })
    return jsonify(sorted(results, key=lambda r: (r["file_rel"], r["line_number"]))[:500])


@app.route("/api/issues")
def api_issues():
    """Find strings with issues (broken, unsafe, missing tokens)."""
    results = []
    with _lock:
        for ts in _all_strings.values():
            if not ts.original_content.strip():
                continue
            safe, issues = check_variables_safe(ts.original_content, ts.content)
            if ts.status == LineStatus.BROKEN or not safe:
                results.append({
                    "file_rel": ts.file_rel,
                    "line_number": ts.line_number,
                    "original": ts.original_content,
                    "current": ts.content,
                    "status": ts.status.name,
                    "safe": safe,
                    "issues": issues,
                })
    return jsonify(sorted(results, key=lambda r: (r["file_rel"], r["line_number"]))[:500])


@app.route("/api/postproc", methods=["POST"])
def api_postproc():
    """Run post-processing on a scope of strings."""
    data = request.get_json(force=True) or {}
    scope_type = data.get("scope", "all")
    scope_value = data.get("value", "")
    fix_type = data.get("fix", "all")

    todo: list[TranslatableString] = []
    with _lock:
        if scope_type == "file":
            fi = _file_items.get(scope_value)
            if fi:
                todo = list(fi.translatable)
        elif scope_type == "dir":
            prefix = scope_value.replace("\\", "/") + "/"
            for rel, fi in _file_items.items():
                if rel.replace("\\", "/").startswith(prefix):
                    todo.extend(fi.translatable)
        else:
            for fi in _file_items.values():
                todo.extend(fi.translatable)

    fixed_count = 0
    fixes_applied: list[dict] = []

    # Group by file for direct file writing
    by_file: dict[str, list[TranslatableString]] = {}
    for ts in todo:
        by_file.setdefault(ts.file_rel, []).append(ts)

    for rel, ts_list in by_file.items():
        raw_lines = _read_file(cfg.target_root / rel)
        if not raw_lines:
            continue
        merge_groups, joined_lines = _build_join_map(raw_lines)
        any_change = False
        for ts in ts_list:
            current = ts.content
            if not current:
                continue
            fixed, fixes = post_process(ts.original_content or "", current)
            if fixed != current:
                if _apply_string(ts, fixed, cfg.target_root, raw_lines, merge_groups, joined_lines):
                    fixed_count += 1
                    fixes_applied.append({
                        "file_rel": rel,
                        "line_number": ts.line_number,
                        "original": ts.original_content,
                        "old": current,
                        "new": fixed,
                        "fixes": fixes,
                    })
                    any_change = True
        if any_change:
            final_lines = _unjoin_lines(merge_groups, joined_lines, raw_lines)
            _flush_file(rel, final_lines)

    cache.save()
    return jsonify({"fixed": fixed_count, "items": fixes_applied})


@app.route("/api/llm-check")
async def api_llm_check():
    connected = await check_llm_connection(llm_cfg)
    return jsonify({"connected": connected})


# --- Startup ---

def main():
    import sys

    # Quick check
    print(f"Original: {cfg.original_root}")
    print(f"Target:   {cfg.target_root}")
    print(f"LLM:      {llm_cfg.api_base} / {llm_cfg.model}")

    if not cfg.original_root.exists():
        print(f"ERROR: Original root not found: {cfg.original_root}")
        sys.exit(1)
    if not cfg.target_root.exists():
        print(f"ERROR: Target root not found: {cfg.target_root}")
        sys.exit(1)

    # Load cached scan results from disk (no scanning on startup)
    loaded = _load_cached_results()
    if loaded:
        print(f"Loaded {len(_file_items)} files from scan cache.")
    else:
        print("No cached scan results found. Click Scan in the UI to start.")

    print(f"Starting web UI at http://127.0.0.1:5002")
    app.run(host="127.0.0.1", port=5002, debug=False, use_reloader=False, threaded=True)
