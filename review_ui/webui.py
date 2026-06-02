"""
SS13 Translation Review — Flask Web UI
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

import flask
from flask import Flask, jsonify, request, render_template

from review_ui.cache import ReviewCache
from review_ui.config import load_config
from review_ui import llm
from review_ui.llm import check_llm_connection, make_llm_config, translate_with_llm
from review_ui.scanner import (
    STATUS_EMOJI,
    STATUS_LABEL,
    FileResult,
    LineStatus,
    TranslatableString,
    _join_dm_continuations,
    check_variables_safe,
    collect_files,
    scan_file,
)

# --- Globals ---

cfg = load_config()
cache = ReviewCache(cfg.cache_dir)
llm_cfg = make_llm_config(cfg)

_lock = threading.Lock()
file_items: dict[str, FileItem] = {}
_all_lines_list: list[TranslatableString] = []
status_counts: dict[str, int] = {}
path_tree: dict = {}
llm_connected = False
scan_complete = False
scan_running = False
scan_progress = {"total": 0, "done": 0, "phase": ""}
batch_job = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": [],
    "scope": "",
    "cancelled": False,
}
batch_cancel_event = threading.Event()

reasoning_mode = True  # True = full reasoning (slow), False = quick extraction (fast)
overwrite_mode = False  # False = skip lines with existing LLM translation, True = re-translate all

app = Flask(__name__)


# --- Serialization ---

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
        "llm_translation": ts.llm_translation,
        "user_translation": ts.user_translation,
    }


def _ts_from_dict(d: dict) -> TranslatableString:
    return TranslatableString(
        file_rel=d["file_rel"],
        line_number=d["line_number"],
        line_text=d["line_text"],
        quote=d["quote"],
        content=d["content"],
        original_content=d["original_content"],
        start=d["start"],
        end=d["end"],
        status=LineStatus[d["status"]],
        llm_translation=d.get("llm_translation"),
        user_translation=d.get("user_translation"),
    )


def _cached_scan_entries_valid(file_rel: str, ts_list: list[TranslatableString]) -> bool:
    """Reject stale scan cache rows whose saved string no longer exists on disk."""
    abs_path = cfg.target_root / file_rel
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False

    if file_rel.endswith(".dm"):
        text, line_map = _join_dm_continuations(text)
        line_lookup = {
            original_lineno: joined_lineno
            for joined_lineno, original_lineno in line_map.items()
        }
    else:
        line_lookup = {}
    lines = text.splitlines()

    for ts in ts_list:
        line_number = line_lookup.get(ts.line_number, ts.line_number)
        line_idx = line_number - 1
        if line_idx < 0 or line_idx >= len(lines):
            return False
        line_text = lines[line_idx]
        if ts.start < 0 or ts.end > len(line_text):
            return False
        expected_quoted = ts.quote + ts.content + ts.quote
        if line_text[ts.start:ts.end] != expected_quoted:
            return False
    return True


def _cached_scan_entries_suspicious(ts_list: list[TranslatableString]) -> bool:
    return any(not ts.original_content.strip() for ts in ts_list)


def _refresh_file_scan(file_rel: str) -> list[TranslatableString]:
    result = scan_file(
        cfg.target_root / file_rel, cfg.original_root, cfg,
        cache.llm_cache, cache.user_cache,
    )
    cache.set_scan(file_rel, [_ts_to_dict(ts) for ts in result.strings])

    with _lock:
        fi = FileItem(cfg.target_root / file_rel, result)
        file_items[file_rel] = fi

    return result.strings


# --- Background scan ---

def _run_scan():
    global scan_complete, scan_running, scan_progress, file_items, _all_lines_list, status_counts, path_tree, _lock

    scan_running = True
    scan_complete = False
    scan_progress["phase"] = "listing files..."
    scan_progress["total"] = 0
    scan_progress["done"] = 0
    try:
        files = collect_files(cfg)
        scan_progress["total"] = len(files)
        scan_progress["phase"] = "diffing"

        local_items: dict[str, FileItem] = {}
        local_lines: list[TranslatableString] = []
        local_counts: dict[str, int] = {}
        local_tree: dict = {}

        for idx, fpath in enumerate(files):
            scan_progress["done"] = idx + 1

            try:
                result = scan_file(
                    fpath, cfg.original_root, cfg,
                    cache.llm_cache, cache.user_cache,
                )
            except Exception:
                continue
            if not result.strings:
                continue

            fi = FileItem(fpath, result)
            local_items[result.file_rel] = fi
            local_lines.extend(result.strings)

            for ts in result.strings:
                local_counts[ts.status.name] = local_counts.get(ts.status.name, 0) + 1

            rel = result.file_rel.replace("\\", "/")
            parts = rel.split("/")
            node = local_tree
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = None

            # Save per-file scan result to cache
            cache.set_scan(result.file_rel, [_ts_to_dict(ts) for ts in result.strings])

        with _lock:
            file_items = local_items
            _all_lines_list = local_lines
            status_counts = local_counts
            path_tree = local_tree

        cache.save()
        scan_complete = True
        scan_progress["phase"] = "done"
    finally:
        scan_running = False


def _try_load_from_cache() -> bool:
    """Load scan results from persisted cache instead of re-scanning."""
    global file_items, _all_lines_list, status_counts, path_tree, scan_complete

    scan_data = cache.scan_cache
    if not scan_data:
        return False

    local_items: dict[str, FileItem] = {}
    local_lines: list[TranslatableString] = []
    local_counts: dict[str, int] = {}
    local_tree: dict = {}
    refreshed_any = False

    for file_rel, strings_data in scan_data.items():
        if not strings_data:
            continue
        ts_list = [_ts_from_dict(s) for s in strings_data]
        if _cached_scan_entries_suspicious(ts_list) and not _cached_scan_entries_valid(file_rel, ts_list):
            try:
                ts_list = _refresh_file_scan(file_rel)
                refreshed_any = True
            except Exception:
                continue
            if not ts_list:
                continue

        for ts in ts_list:
            local_lines.append(ts)
            local_counts[ts.status.name] = local_counts.get(ts.status.name, 0) + 1

        rel = file_rel.replace("\\", "/")
        parts = rel.split("/")
        node = local_tree
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = None

        # Reconstruct FileItem (path not critical for UI-only usage)
        fi = FileItem(cfg.target_root / file_rel, FileResult(file_rel=file_rel))
        fi.translatable = ts_list
        local_items[file_rel] = fi

    if not local_items:
        return False

    with _lock:
        file_items = local_items
        _all_lines_list = local_lines
        status_counts = local_counts
        path_tree = local_tree

    scan_complete = True
    if refreshed_any:
        cache.save()
    return True


def _get_tree_node(path_parts: list[str]) -> dict | None:
    """Walk path_tree to find the node at the given path."""
    node = path_tree
    for p in path_parts:
        if not isinstance(node, dict):
            return None
        node = node.get(p)
        if node is None:
            return None
    return node


def _filter_translatable(strings: list[TranslatableString]) -> list[TranslatableString]:
    """Filter strings depending on overwrite_mode. False = skip already LLM'd lines."""
    if overwrite_mode:
        return [ts for ts in strings if ts.original_content.strip()]
    return [
        ts for ts in strings
        if ts.original_content.strip() and ts.status in (LineStatus.ORIGINAL, LineStatus.TRANSLATED)
    ]


def _iter_scope_strings(scope_type: str, scope_value: str) -> list[TranslatableString]:
    with _lock:
        if scope_type == "file":
            fi = file_items.get(scope_value)
            return list(fi.translatable) if fi else []
        if scope_type == "dir":
            prefix = scope_value.replace("\\", "/") + "/"
            return [
                ts
                for rel, fi in file_items.items()
                if rel.replace("\\", "/").startswith(prefix)
                for ts in fi.translatable
            ]
        if scope_type == "all":
            return [ts for fi in file_items.values() for ts in fi.translatable]
    return []


def _repair_issue_for(ts: TranslatableString) -> tuple[bool, list[str]]:
    issues: list[str] = []
    has_issue = ts.status == LineStatus.BROKEN
    if not ts.original_content.strip():
        has_issue = True
        issues.append("No matched English source")
    elif ts.content:
        safe, current_issues = check_variables_safe(ts.original_content, ts.content)
        if not safe:
            has_issue = True
            issues.extend(current_issues)

    candidate = ts.user_translation or ts.llm_translation
    if ts.original_content.strip() and candidate:
        safe, candidate_issues = check_variables_safe(ts.original_content, candidate)
        if not safe:
            has_issue = True
            issues.extend(f"Candidate {issue}" for issue in candidate_issues)

    if ts.status == LineStatus.BROKEN and not issues:
        issues.append("Marked broken")
    return has_issue, issues


def _repair_candidates(scope_type: str = "all", scope_value: str = "") -> list[TranslatableString]:
    result = []
    for ts in _iter_scope_strings(scope_type, scope_value):
        has_issue, _ = _repair_issue_for(ts)
        if has_issue:
            result.append(ts)
    return result


def _collect_dir_strings(dir_rel: str) -> list[TranslatableString]:
    prefix = dir_rel.replace("\\", "/") + "/"
    result: list[TranslatableString] = []
    with _lock:
        for rel, fi in file_items.items():
            if rel.replace("\\", "/").startswith(prefix):
                result.extend(fi.translatable)
    return _filter_translatable(result)


# --- Flask Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/test-llm")
def test_llm():
    return render_template("test_llm.html")


@app.route("/api/status")
def api_status():
    with _lock:
        counts = dict(status_counts)
        fs = len(file_items)
        total = len(_all_lines_list)

    return jsonify({
        "files": fs,
        "lines": total,
        "counts": counts,
        "llm_connected": llm_connected,
        "scan_complete": scan_complete,
        "scan_running": scan_running,
        "scan_progress": dict(scan_progress),
        "source_lang": cfg.source_lang,
        "target_lang": cfg.target_lang,
        "original_root": str(cfg.original_root),
        "target_root": str(cfg.target_root),
        "reasoning_mode": reasoning_mode,
        "overwrite_mode": overwrite_mode,
    })


@app.route("/api/tree")
def api_tree():
    parent = request.args.get("parent", "")
    if parent:
        parts = parent.split("/")
        node = _get_tree_node(parts)
    else:
        node = path_tree

    if not isinstance(node, dict):
        return jsonify([])

    entries = []
    for name, child in sorted(node.items()):
        rel = f"{parent}/{name}" if parent else name
        if child is None:
            # File
            fi = file_items.get(rel)
            summary = _status_summary(fi.translatable) if fi else ""
            entries.append({
                "name": name,
                "type": "file",
                "rel": rel,
                "summary": summary,
            })
        else:
            # Directory — aggregate status from all files under it
            prefix = rel.replace("\\", "/") + "/"
            all_strings: list[TranslatableString] = []
            with _lock:
                for frel, fi in file_items.items():
                    if frel.replace("\\", "/").startswith(prefix):
                        all_strings.extend(fi.translatable)
            summary = _status_summary(all_strings) if all_strings else ""
            entries.append({
                "name": name,
                "type": "dir",
                "rel": rel,
                "has_children": bool(child),
                "summary": summary,
            })

    return jsonify(entries)


def _status_summary(strings: list[TranslatableString]) -> str:
    ks: dict[str, int] = {}
    for ts in strings:
        ks[ts.status.name] = ks.get(ts.status.name, 0) + 1
    parts = []
    for s in LineStatus:
        c = ks.get(s.name, 0)
        if c:
            parts.append(f"{STATUS_EMOJI.get(s, '?')} {c}")
    return " ".join(parts)


@app.route("/api/search-files")
def api_search_files():
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify([])
    with _lock:
        results = [rel for rel in file_items if q in rel.lower()]
    return jsonify(sorted(results)[:200])


@app.route("/api/search-strings")
def api_search_strings():
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify([])
    results = []
    with _lock:
        for fi in file_items.values():
            for ts in fi.translatable:
                if q in ts.original_content.lower() or q in ts.content.lower():
                    safe, issues = check_variables_safe(ts.original_content, ts.content)
                    results.append({
                        "file_rel": ts.file_rel,
                        "line_number": ts.line_number,
                        "original_content": ts.original_content,
                        "content": ts.content,
                        "status_emoji": STATUS_EMOJI.get(ts.status, "?"),
                        "status_label": STATUS_LABEL.get(ts.status, "?"),
                        "safe": safe,
                        "issues": issues,
                    })
    return jsonify(sorted(results, key=lambda r: r["file_rel"])[:200])


@app.route("/api/llm-queue")
def api_llm_queue():
    """Return all lines with LLM translations across all files."""
    results = []
    with _lock:
        for fi in file_items.values():
            for ts in fi.translatable:
                if ts.llm_translation:
                    safe, issues = check_variables_safe(ts.original_content, ts.content)
                    results.append({
                        "file_rel": ts.file_rel,
                        "line_number": ts.line_number,
                        "original_content": ts.original_content,
                        "content": ts.content,
                        "llm_translation": ts.llm_translation,
                        "status_emoji": STATUS_EMOJI.get(ts.status, "?"),
                        "status_label": STATUS_LABEL.get(ts.status, "?"),
                        "safe": safe,
                        "issues": issues,
                    })
    return jsonify(sorted(results, key=lambda r: (r["file_rel"], r["line_number"])))


@app.route("/api/repair-queue")
def api_repair_queue():
    """Return all lines that are broken, unsafe, or missing source mapping."""
    scope_type = request.args.get("scope_type", "all")
    scope_value = request.args.get("scope_value", "")
    results = []
    for ts in _repair_candidates(scope_type, scope_value):
        _, issues = _repair_issue_for(ts)
        fixable = bool(ts.original_content.strip())
        results.append({
            "file_rel": ts.file_rel,
            "line_number": ts.line_number,
            "status": ts.status.name,
            "status_emoji": STATUS_EMOJI.get(ts.status, "?"),
            "status_label": STATUS_LABEL.get(ts.status, "?"),
            "original_content": ts.original_content,
            "content": ts.content,
            "llm_translation": ts.llm_translation,
            "user_translation": ts.user_translation,
            "safe": not issues,
            "issues": issues,
            "fixable": fixable,
        })
    return jsonify(sorted(results, key=lambda r: (r["file_rel"], r["line_number"])))


@app.route("/api/file/<path:rel>")
def api_file(rel: str):
    with _lock:
        fi = file_items.get(rel)

    if not fi:
        return jsonify({"error": "File not found"}), 404
    if not _cached_scan_entries_valid(rel, fi.translatable):
        try:
            _refresh_file_scan(rel)
        except Exception:
            pass
        with _lock:
            fi = file_items.get(rel)
        if not fi:
            return jsonify({"error": "File not found"}), 404

    strings_data = []
    for ts in fi.translatable:
        safe, issues = check_variables_safe(ts.original_content, ts.content)
        strings_data.append({
            "line_number": ts.line_number,
            "status": ts.status.name,
            "status_emoji": STATUS_EMOJI.get(ts.status, "?"),
            "status_label": STATUS_LABEL.get(ts.status, "?"),
            "original_content": ts.original_content,
            "content": ts.content,
            "llm_translation": ts.llm_translation,
            "user_translation": ts.user_translation,
            "safe": safe,
            "issues": issues,
        })

    return jsonify({
        "file_rel": rel,
        "strings": strings_data,
    })


@app.route("/api/translate-line", methods=["POST"])
async def api_translate_line():
    global llm_connected

    data = request.get_json(force=True)
    file_rel = data.get("file_rel", "")
    line_number = data.get("line_number", 0)
    source = data.get("source", "")

    if not source:
        try:
            refreshed = _refresh_file_scan(file_rel)
        except Exception:
            refreshed = []
        for ts in refreshed:
            if ts.line_number == line_number and ts.original_content.strip():
                source = ts.original_content
                break
        if not source:
            return jsonify({"error": "No source text"}), 400
    if not llm_connected:
        # Try reconnecting
        llm_connected = await check_llm_connection(llm_cfg)
        if not llm_connected:
            return jsonify({"error": "LLM not connected"}), 503

    translation = await translate_with_llm(
        source, llm_cfg, cfg.source_lang, cfg.target_lang, reasoning_mode,
    )

    if translation is None:
        return jsonify({"error": "Translation failed - model returned empty response. Check LM Studio."}), 500

    cache_key = f"{file_rel}:{line_number}:{source}"
    safe, issues = check_variables_safe(source, translation)
    if not safe:
        # Retry with stricter prompt on first failure
        translation2 = await translate_with_llm(
            source, llm_cfg, cfg.source_lang, cfg.target_lang, reasoning_mode,
            strict=True, safety_issues=issues,
        )
        if translation2:
            safe2, issues2 = check_variables_safe(source, translation2)
            if safe2:
                translation = translation2
                safe, issues = True, []

    cache.set_llm_translation(cache_key, translation)

    status = LineStatus.LLM_TWEAKED if safe else LineStatus.BROKEN

    # Update in-memory state
    with _lock:
        fi = file_items.get(file_rel)
        if fi:
            for ts in fi.translatable:
                if ts.line_number == line_number and ts.original_content == source:
                    ts.llm_translation = translation
                    ts.status = status
                    break

    cache.save()

    return jsonify({
        "translation": translation,
        "safe": safe,
        "issues": issues,
        "status": status.name,
        "status_emoji": STATUS_EMOJI.get(status, "?"),
    })


@app.route("/api/test-translate", methods=["POST"])
async def api_test_translate():
    """Debug endpoint: translate text and return full LLM response for inspection."""
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    prompt = llm.TRANSLATION_SYSTEM_PROMPT.format(source_lang=cfg.source_lang, target_lang=cfg.target_lang)

    try:
        import httpx
        _max_tokens = max(llm_cfg.max_tokens, 6144) if reasoning_mode else 1536
        request_body = {
            "model": llm_cfg.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Translate this to {cfg.target_lang}: {text}"},
            ],
            "temperature": llm_cfg.temperature,
            "max_tokens": _max_tokens,
        }
        async with httpx.AsyncClient(timeout=llm_cfg.timeout) as client:
            resp = await client.post(
                f"{llm_cfg.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {llm_cfg.api_key}", "Content-Type": "application/json"},
                json=request_body,
            )

            raw_resp = resp.json()
            msg = raw_resp.get("choices", [{}])[0].get("message", {})
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()

            extracted = llm._extract_from_message(msg)

            safe, issues = check_variables_safe(text, extracted) if extracted else (False, ["No translation"])

            return jsonify({
                "text": text,
                "translation": extracted,
                "safe": safe,
                "issues": issues,
                "content_raw": content,
                "reasoning_content": reasoning,
                "raw_response": raw_resp,
                "llm_connected": True,
                "model": raw_resp.get("model", "unknown"),
                "_debug_sent_max_tokens": request_body["max_tokens"],
            })

    except httpx.TimeoutException:
        return jsonify({"error": "LLM request timed out", "text": text}), 504
    except httpx.HTTPStatusError as e:
        return jsonify({"error": f"LLM HTTP {e.response.status_code}", "detail": str(e), "text": text}), 502
    except Exception as e:
        return jsonify({"error": str(e), "text": text}), 500


@app.route("/api/batch-translate", methods=["POST"])
def api_batch_translate():
    global llm_connected, batch_job

    if not scan_complete:
        return jsonify({"error": "Scan not complete yet"}), 503

    if not llm_connected:
        return jsonify({"error": "LLM not connected"}), 503

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    scope_type = data.get("scope_type", "")
    scope_value = data.get("scope_value", "")
    repair_only = bool(data.get("repair_only", False))

    if repair_only:
        todo = [ts for ts in _repair_candidates(scope_type, scope_value) if ts.original_content.strip()]
    elif scope_type == "file":
        with _lock:
            fi = file_items.get(scope_value)
        todo = _filter_translatable(fi.translatable) if fi else []
    elif scope_type == "dir":
        todo = _collect_dir_strings(scope_value)
    else:
        return jsonify({"error": "Invalid scope"}), 400

    if not todo:
        return jsonify({"error": "No lines to translate"}), 400

    batch_job = {
        "running": True,
        "total": len(todo),
        "done": 0,
        "errors": [],
        "results": [],
        "current": "",
        "scope": scope_value or scope_type,
        "cancelled": False,
        "repair_only": repair_only,
    }
    batch_cancel_event.clear()

    thread = threading.Thread(target=_run_batch, args=(todo.copy(), repair_only), daemon=True)
    thread.start()

    return jsonify({
        "total": len(todo),
        "scope": scope_value,
        "status": "started",
    })


def _run_batch(todo: list[TranslatableString], force_translate: bool = False):
    """Run batch translation in a thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_batch_worker(todo, force_translate=force_translate))
    finally:
        loop.close()


def _cached_translation_is_valid(source: str, translation: str) -> bool:
    """Reject cached translations that are clearly garbage from earlier broken extraction."""
    if not translation or len(translation) < 4:
        return False
    # Reject if it echoes system prompt text
    lowered = translation.lower()
    if "translate this" in lowered or "pt-br" in lowered:
        return False
    if "keep all" in lowered and ("brackets" in lowered or "tags" in lowered):
        return False
    if "thinking process" in lowered:
        return False
    # Reject if it's just an English word or short English fragment
    if translation.isascii() and len(translation.split()) <= 3:
        return False
    # Reject if it's just a single word (too short for a meaningful translation)
    if len(translation.split()) <= 1 and len(translation) < 10:
        return False
    return True


async def _batch_worker(todo: list[TranslatableString], force_translate: bool = False):
    global batch_job, llm_connected

    count = 0
    for ts in todo:
        if not llm_connected:
            break
        if batch_cancel_event.is_set():
            batch_job["cancelled"] = True
            break

        source = ts.original_content
        if not source.strip():
            batch_job["errors"].append(f"No source: {ts.file_rel}:{ts.line_number}")
            batch_job["done"] += 1
            continue
        batch_job["current"] = source
        cache_key = f"{ts.file_rel}:{ts.line_number}:{source}"
        cached = cache.get_llm_translation(cache_key)
        if not force_translate and cached and _cached_translation_is_valid(source, cached):
            translation = cached
        else:
            translation = await translate_with_llm(
                source, llm_cfg, cfg.source_lang, cfg.target_lang, reasoning_mode,
            )
            if translation is None:
                batch_job["errors"].append(f"Failed: {ts.file_rel}:{ts.line_number}")
                batch_job["done"] += 1
                continue
            safe, issues = check_variables_safe(source, translation)
            if not safe:
                # Retry with stricter prompt
                translation2 = await translate_with_llm(
                    source, llm_cfg, cfg.source_lang, cfg.target_lang, reasoning_mode,
                    strict=True, safety_issues=issues,
                )
                if translation2:
                    safe2, _ = check_variables_safe(source, translation2)
                    if safe2:
                        translation = translation2
            cache.set_llm_translation(cache_key, translation)

        safe, _ = check_variables_safe(source, translation)
        new_status = LineStatus.LLM_TWEAKED if safe else LineStatus.BROKEN

        with _lock:
            fi = file_items.get(ts.file_rel)
            if fi:
                for ft in fi.translatable:
                    if ft.line_number == ts.line_number and ft.original_content == source:
                        ft.llm_translation = translation
                        ft.status = new_status
                        break

        batch_job["results"].append({
            "file": ts.file_rel,
            "line": ts.line_number,
            "original": source,
            "translation": translation,
        })
        count += 1
        batch_job["done"] = count

        if count % 5 == 0:
            await asyncio.sleep(0)

    cache.save()
    batch_job["running"] = False


@app.route("/api/batch-progress")
def api_batch_progress():
    return jsonify(batch_job)


@app.route("/api/batch-cancel", methods=["POST"])
def api_batch_cancel():
    batch_cancel_event.set()
    return jsonify({"status": "cancelling"})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    global scan_running, scan_complete
    if scan_running:
        return jsonify({"status": "already_running"}), 409

    scan_running = True
    scan_complete = False
    scan_progress["phase"] = "starting..."
    scan_progress["total"] = 0
    scan_progress["done"] = 0

    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    """Apply cached translations to target files on disk."""
    data = request.get_json(force=True)
    scope_type = data.get("scope_type", "")  # "file" or "dir" or "all"
    scope_value = data.get("scope_value", "")

    # Collect files to process
    target_files: set[str] = set()
    with _lock:
        if scope_type == "file":
            if scope_value in file_items:
                target_files.add(scope_value)
        elif scope_type == "dir":
            prefix = scope_value.replace("\\", "/") + "/"
            for rel in file_items:
                if rel.replace("\\", "/").startswith(prefix):
                    target_files.add(rel)
        elif scope_type == "all":
            target_files = set(file_items.keys())

    if not target_files:
        return jsonify({"error": "No files to process"}), 400

    files_modified = 0
    lines_modified = 0
    skipped_files: list[dict[str, object]] = []

    for rel in sorted(target_files):
        fi = file_items.get(rel)
        if not fi:
            continue

        # Find strings with translations in this file
        todo = [
            ts for ts in fi.translatable
            if ts.llm_translation or ts.user_translation
        ]
        if not todo:
            continue

        invalid_entries = []
        for ts in todo:
            translation = ts.user_translation or ts.llm_translation
            if not translation:
                continue
            safe, issues = check_variables_safe(ts.original_content, translation)
            if ts.status == LineStatus.BROKEN or not safe:
                ts.status = LineStatus.BROKEN
                invalid_entries.append({
                    "line": ts.line_number,
                    "source": ts.original_content,
                    "translation": translation,
                    "issues": issues or ["Already marked broken"],
                })

        if invalid_entries:
            skipped_files.append({"file": rel, "errors": invalid_entries})
            continue

        abs_path = cfg.target_root / rel
        try:
            raw = abs_path.read_text(encoding="utf-8")
        except Exception:
            continue

        lines = raw.splitlines(keepends=True)
        changed = False

        # Pre-join DM continuation lines for multi-line string support
        # merge_groups[i] = (first_orig_line_idx, count) for each joined line
        merge_groups: list[tuple[int, int]] = []
        if rel.endswith(".dm"):
            i = 0
            while i < len(lines):
                stripped = lines[i].rstrip("\n\r")
                if stripped.endswith("\\") and i + 1 < len(lines):
                    merge_groups.append((i, 2))
                    i += 2
                else:
                    merge_groups.append((i, 1))
                    i += 1
        else:
            merge_groups = [(i, 1) for i in range(len(lines))]

        # Build joined_lines: one per merge group
        joined_lines: list[str] = []
        for first, count in merge_groups:
            if count == 1:
                joined_lines.append(lines[first])
            else:
                # Join: remove trailing \ and newline, lstrip continuation
                l1 = lines[first].rstrip("\n\r")
                l2 = lines[first + 1]
                joined = l1[:-1] + l2.lstrip()
                joined_lines.append(joined + "\n")

        # Process bottom-to-top so line numbers stay valid
        for ts in sorted(todo, key=lambda t: -t.line_number):
            line_idx = ts.line_number - 1
            # Find which merge group this line belongs to
            jl_idx = None
            for gi, (first, count) in enumerate(merge_groups):
                if first <= line_idx < first + count:
                    jl_idx = gi
                    break
            if jl_idx is None or jl_idx >= len(joined_lines):
                continue

            line_text = joined_lines[jl_idx].rstrip("\n\r")
            if ts.start < 0 or ts.end > len(line_text):
                continue

            # Verify the line still contains expected quoted string
            expected_quoted = ts.quote + ts.content + ts.quote
            actual_quoted = line_text[ts.start:ts.end]
            if actual_quoted != expected_quoted:
                continue

            translation = ts.user_translation or ts.llm_translation
            if not translation:
                continue

            # Replace content inside quotes
            new_quoted = ts.quote + translation + ts.quote
            new_line_text = line_text[:ts.start] + new_quoted + line_text[ts.end:]
            eol = joined_lines[jl_idx][len(line_text):]
            joined_lines[jl_idx] = new_line_text + eol
            changed = True
            lines_modified += 1

        # Un-join back to original line structure if DM file
        if changed and rel.endswith(".dm"):
            unjoined: list[str] = []
            for gi, (first, count) in enumerate(merge_groups):
                if count == 1:
                    unjoined.append(joined_lines[gi])
                else:
                    jl = joined_lines[gi].rstrip("\n\r")
                    eol = joined_lines[gi][len(jl):] or "\n"
                    orig_cont = lines[first + 1]
                    indent = orig_cont[: len(orig_cont) - len(orig_cont.lstrip())]
                    unjoined.append(jl + " \\" + eol)
                    unjoined.append(indent + eol)
            lines = unjoined

        if changed:
            abs_path.write_text("".join(lines), encoding="utf-8")
            files_modified += 1

    cache.save()

    return jsonify({
        "files_modified": files_modified,
        "lines_modified": lines_modified,
        "skipped_files": skipped_files,
    })


@app.route("/api/discard-translation", methods=["POST"])
def api_discard_translation():
    """Remove LLM translation from a line, reverting to ORIGINAL status."""
    data = request.get_json(force=True)
    file_rel = data.get("file_rel", "")
    line_number = data.get("line_number", 0)
    source = data.get("source", "")

    with _lock:
        fi = file_items.get(file_rel)
        if fi:
            for ts in fi.translatable:
                if ts.line_number == line_number and ts.original_content == source:
                    ts.llm_translation = ""
                    ts.user_translation = ""
                    ts.status = LineStatus.ORIGINAL
                    break

    if fi is None:
        return jsonify({"error": "File not found"}), 404

    cache.save()
    return jsonify({"status": "discarded"})


@app.route("/api/save-user-edit", methods=["POST"])
def api_save_user_edit():
    """Save a manual user translation edit."""
    data = request.get_json(force=True)
    file_rel = data.get("file_rel", "")
    line_number = data.get("line_number", 0)
    source = data.get("source", "")
    user_text = data.get("user_text", "")

    if not user_text:
        return jsonify({"error": "Empty translation"}), 400

    with _lock:
        fi = file_items.get(file_rel)
        if not fi:
            return jsonify({"error": "File not found"}), 404
        for ts in fi.translatable:
            if ts.line_number == line_number and ts.original_content == source:
                ts.user_translation = user_text
                ts.status = LineStatus.USER_MODIFIED
                break

    cache.save()
    return jsonify({"status": "saved"})


@app.route("/api/clear-llm-cache", methods=["POST"])
def api_clear_llm_cache():
    """Remove invalid LLM cache entries and reset affected lines to ORIGINAL."""
    cache_count = 0
    reset_count = 0

    # Remove invalid entries from cache
    for key in list(cache.llm_cache.keys()):
        val = cache.llm_cache[key]
        if not _cached_translation_is_valid("", val):
            del cache.llm_cache[key]
            cache_count += 1
    cache.save()

    # Reset in-memory status for lines that had LLM translations
    with _lock:
        for fi in file_items.values():
            for ts in fi.translatable:
                if ts.status in (LineStatus.LLM_TWEAKED, LineStatus.BROKEN) and ts.llm_translation:
                    ts.llm_translation = ""
                    ts.status = LineStatus.ORIGINAL
                    reset_count += 1

    return jsonify({"cache_cleared": cache_count, "lines_reset": reset_count})


@app.route("/api/save-cache", methods=["POST"])
def api_save_cache():
    cache.save()
    return jsonify({"status": "saved"})


@app.route("/api/open-vscode")
def api_open_vscode():
    file_rel = request.args.get("file", "")
    line = request.args.get("line", "1")
    abs_path = cfg.target_root / file_rel
    try:
        subprocess.Popen(["code", "--goto", f"{abs_path}:{line}"], shell=True)
        return jsonify({"status": "opened"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/toggle-reasoning", methods=["POST"])
def api_toggle_reasoning():
    global reasoning_mode
    data = request.get_json(force=True)
    reasoning_mode = data.get("enabled", not reasoning_mode)
    return jsonify({"reasoning_mode": reasoning_mode})


@app.route("/api/toggle-overwrite", methods=["POST"])
def api_toggle_overwrite():
    global overwrite_mode
    data = request.get_json(force=True)
    overwrite_mode = data.get("enabled", not overwrite_mode)
    return jsonify({"overwrite_mode": overwrite_mode})


# --- Startup ---

def start_background_scan():
    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()


def main():
    # Check LLM connection
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        global llm_connected
        llm_connected = loop.run_until_complete(check_llm_connection(llm_cfg))
        loop.close()
    except Exception:
        llm_connected = False

    # Try loading scan from cache first
    if _try_load_from_cache():
        print(f"Loaded {len(file_items)} files from scan cache.")
    else:
        print("No scan cache found. Starting background scan...")
        start_background_scan()

    print(f"Starting web UI at http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False, threaded=True)


# --- Helper class ---

class FileItem:
    def __init__(self, path: Path, result: FileResult):
        self.path = path
        self.result = result
        self.translatable: list[TranslatableString] = result.strings

    @property
    def rel(self) -> str:
        return self.result.file_rel
