from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum, auto

from review_ui.config import Config


class LineStatus(Enum):
    ORIGINAL = auto()      # Content unchanged from original English
    TRANSLATED = auto()    # Content differs from original
    LLM_TWEAKED = auto()   # Differs, recorded in LLM cache
    USER_MODIFIED = auto() # Differs, recorded in user cache
    BROKEN = auto()        # Variables/brackets damaged


STATUS_EMOJI = {
    LineStatus.ORIGINAL: "\u2705",
    LineStatus.TRANSLATED: "\u2194\ufe0f",
    LineStatus.LLM_TWEAKED: "\U0001F916",
    LineStatus.USER_MODIFIED: "\U0001F464",
    LineStatus.BROKEN: "\u274c",
}

STATUS_LABEL = {
    LineStatus.ORIGINAL: "Original",
    LineStatus.TRANSLATED: "Translated",
    LineStatus.LLM_TWEAKED: "LLM",
    LineStatus.USER_MODIFIED: "User",
    LineStatus.BROKEN: "Broken",
}


@dataclass
class TranslatableString:
    file_rel: str
    line_number: int
    line_text: str
    quote: str
    content: str
    original_content: str  # The English version from original file, or '' if unfound
    start: int
    end: int
    status: LineStatus = LineStatus.ORIGINAL
    llm_translation: str | None = None
    user_translation: str | None = None


@dataclass
class FileResult:
    file_rel: str
    strings: list[TranslatableString] = field(default_factory=list)


# --- Reusable patterns ---
STRING_PATTERN = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')')
LETTER_PATTERN = re.compile(r"[A-Za-z\u00C0-\u00FF]")
DM_ASSIGNMENT_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

DM_STRING_CONTEXT_MARKERS = (
    "to_chat(", "span_", "balloon_alert(", "visible_message(",
    "examine_list", "tgui_alert(", "alert(", "input(",
    "stripped_input(", "stripped_multiline_input(", "throw_alert(",
)
DM_SKIP_CONTEXT_MARKERS = (
    "message_admins(", "log_admin(", "log_game(", "log_world(",
    "log_shuttle(", "log_econ(", "log_combat(", "log_say(",
    "log_whisper(", "log_emote(", "log_attack(", "log_ooc(",
    "log_pda(", "log_chat(", "log_comment(", "log_",
    "investigate_log(",
)
DM_TRANSLATABLE_ASSIGNMENTS = {
    "desc", "description", "report_message", "full_name", "title",
    "display_name", "prompt_name", "extended_desc", "special_desc",
    "death_message", "you_are_text", "explanation_text",
    "catalog_description", "menu_description", "scan_desc",
    "steal_hint", "documentation", "medical_record_text",
    "default_raw_text", "flavour_text", "taste_description",
    "important_text", "spread_text", "occur_text", "unit_name",
    "machine_name", "singular_name", "crate_name", "rpg_title",
    "header",
}
DM_NON_TRANSLATABLE_ASSIGNMENTS = {
    "name", "id", "key", "config_tag", "savefile_key", "template_id",
    "shuttle_id", "shuttleid", "puzzle_id", "fish_id", "tgui_id",
    "map_name", "mappath", "filename", "filepath", "path", "icon",
    "icon_state", "base_icon_state", "inhand_icon_state",
    "button_icon_state", "worn_icon_state", "overlay_icon_state",
    "overlay_state", "background_icon_state", "post_init_icon_state",
    "new_icon_state", "trim_state", "program_icon",
    "program_open_overlay", "light_mask", "light_color", "main_color",
    "neon_color", "screen_loc", "agent", "role", "category", "group",
    "species", "assignment", "location", "suffix", "prefix",
    "real_name", "proper_name",
}

HARDCODED_EXCLUDE_SUFFIXES = (
    "rspack.config.ts", "rspack.config-dev.ts", "bun.lock",
    "bunfig.toml", "tsconfig.json", "package.json",
    ".prettierrc.yml", ".prettierignore", ".gitattributes",
    "global.d.ts", "happydom.ts",
)


def is_excluded(path: Path, config: Config) -> bool:
    lowered = path.as_posix().lower()
    if any(lowered.endswith("/" + suffix) for suffix in HARDCODED_EXCLUDE_SUFFIXES):
        return True
    return any(
        f"/{excluded}/" in f"/{lowered}/" or lowered.endswith("/" + excluded)
        for excluded in config.exclude_dirs
    )


def collect_files(config: Config) -> list[Path]:
    files: list[Path] = []
    for path in config.target_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in config.include_exts:
            continue
        if is_excluded(path, config):
            continue
        files.append(path)
    return sorted(files)


def extract_dm_strings(line: str) -> list[tuple[int, int, str]]:
    strings: list[tuple[int, int, str]] = []
    i = 0
    while i < len(line):
        if line[i] not in ('"', "'"):
            i += 1
            continue
        quote = line[i]
        start = i
        i += 1
        escaped = False
        bracket_depth = 0
        while i < len(line):
            ch = line[i]
            if escaped:
                escaped = False
                i += 1
                continue
            if ch == "\\":
                escaped = True
                i += 1
                continue
            if ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif ch == quote and bracket_depth == 0:
                strings.append((start, i + 1, line[start : i + 1]))
                i += 1
                break
            i += 1
    return strings


def should_translate_text(text: str, line: str, ext: str) -> bool:
    stripped = text.strip()
    if not stripped or not LETTER_PATTERN.search(stripped):
        return False
    if len(stripped) <= 1:
        return False
    if line.lstrip().startswith("//"):
        return False
    if re.fullmatch(r"[A-Za-z0-9_.:/#-]+", stripped):
        return False
    if re.fullmatch(r"[a-z0-9_.-]+", stripped):
        return False

    low_line = line.lower()
    if any(marker in low_line for marker in DM_SKIP_CONTEXT_MARKERS):
        return False
    skip_context = ("#include", "import ", "require(", "icon =",
                    "icon_state =", "sound(", "resource(", "stylesheet", "url(")
    if any(marker in low_line for marker in skip_context):
        return False

    if ext == ".dm":
        assign_match = DM_ASSIGNMENT_PATTERN.match(line)
        if assign_match:
            field_name = assign_match.group(1).lower()
            if field_name in DM_NON_TRANSLATABLE_ASSIGNMENTS:
                return False
            if field_name.endswith("_id") or field_name.endswith("_icon_state") or field_name.endswith("_state"):
                return False
            if field_name.endswith("_desc") or field_name.endswith("_description") or field_name.endswith("_text") or field_name.endswith("_message"):
                return True
            return field_name in DM_TRANSLATABLE_ASSIGNMENTS

        if any(marker in low_line for marker in DM_STRING_CONTEXT_MARKERS):
            return True
        return False

    return True


DM_REQUIRED_MACRO_RE = re.compile(
    r'\\(?:[Tt]hem(?:selves)?|[Tt]heir|[Hh]im(?:self)?|[Hh]e(?:self)?|[Hh]is|[Ii]tself|[Oo]urselves|[Yy]ourselves)'
)
DM_ARTICLE_MACRO_RE = re.compile(r'\\(?:[Tt]he|[Aa]n|[Aa])(?![A-Za-z])')
# Match unknown backslash words, but ignore common single-character escapes like \n, \r, \t, \" and \\\.
DM_UNKNOWN_BACKSLASH_RE = re.compile(r'\\(?![nrt"\'\\])[A-Za-z]+')


def check_variables_safe(original_content: str, translated_content: str) -> tuple[bool, list[str]]:
    issues: list[str] = []

    orig_brackets = re.findall(r'\[.*?\]', original_content)
    trans_brackets = re.findall(r'\[.*?\]', translated_content)
    if orig_brackets != trans_brackets:
        issues.append(f"Brackets: {orig_brackets} vs {trans_brackets}")

    orig_macros = DM_REQUIRED_MACRO_RE.findall(original_content)
    trans_macros = DM_REQUIRED_MACRO_RE.findall(translated_content)
    if orig_macros != trans_macros:
        issues.append(f"Macros: {orig_macros} vs {trans_macros}")

    trans_article_macros = DM_ARTICLE_MACRO_RE.findall(translated_content)
    if trans_article_macros:
        issues.append(f"English article macros left in translation: {trans_article_macros}")

    allowed_backslash_words = set(DM_REQUIRED_MACRO_RE.findall(translated_content))
    allowed_backslash_words.update(DM_ARTICLE_MACRO_RE.findall(translated_content))
    allowed_backslash_words.update({"\\n", "\\r", "\\t"})
    unknown_backslash_words = [
        token for token in DM_UNKNOWN_BACKSLASH_RE.findall(translated_content)
        if token not in allowed_backslash_words
    ]
    if unknown_backslash_words:
        issues.append(f"Unknown backslash tokens: {unknown_backslash_words}")

    orig_escapes = re.findall(r'\\[nrt"\'\\]', original_content)
    trans_escapes = re.findall(r'\\[nrt"\'\\]', translated_content)
    if orig_escapes != trans_escapes:
        issues.append(f"Escapes: {orig_escapes} vs {trans_escapes}")

    orig_html = re.findall(r'<[^>]+>', original_content)
    trans_html = re.findall(r'<[^>]+>', translated_content)
    if orig_html != trans_html:
        issues.append(f"HTML: {orig_html} vs {trans_html}")

    orig_printf = re.findall(r'%(?:\d+\$)?[sdif]|%[sdif]', original_content)
    trans_printf = re.findall(r'%(?:\d+\$)?[sdif]|%[sdif]', translated_content)
    if orig_printf != trans_printf:
        issues.append(f"Printf: {orig_printf} vs {trans_printf}")

    return len(issues) == 0, issues


def read_file_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except Exception:
        return None


def extract_string_info(line: str, ext: str) -> list[dict]:
    if '"' not in line and "'" not in line:
        return []

    if ext == ".dm":
        raw_strings = extract_dm_strings(line)
    else:
        raw_strings = [(m.start(), m.end(), m.group(0)) for m in STRING_PATTERN.finditer(line)]

    results = []
    for start, end, quoted in raw_strings:
        quote = quoted[0]
        content = quoted[1:-1]
        if should_translate_text(content, line, ext):
            results.append({"start": start, "end": end, "quote": quote, "content": content})
    return results


TOKEN_PATTERN = re.compile(
    r"\[.*?\]"                          # DM bracket vars
    r"|<[^>]+>"                         # HTML tags
    r"|\\[nrt\"\'\\]"                   # escape sequences
    r"|%(?:\d+\$)?[sdif]"               # printf tokens
    r"|\\(?:[Tt]hem(?:selves)?|[Tt]heir|"
    r"[Hh]im(?:self)?|[Hh]e(?:self)?|[Hh]is|"
    r"[Ii]tself|[Oo]urselves|[Yy]ourselves)"  # non-article DM macros
)


def content_match_key(text: str) -> str:
    """Extract non-translatable tokens from content for structural matching."""
    return "|".join(TOKEN_PATTERN.findall(text))


def _join_dm_continuations(text: str) -> tuple[str, dict[int, int]]:
    """Join DM continuation lines. Returns (joined_text, {joined_lineno: original_lineno})."""
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    line_map: dict[int, int] = {}
    i = 0
    while i < len(lines):
        # Collect all continuation lines starting at i
        chunk: list[str] = [lines[i]]
        j = i
        while j < len(lines):
            stripped = lines[j].rstrip("\n\r")
            if stripped.endswith("\\") and j + 1 < len(lines):
                chunk.append(lines[j + 1])
                j += 2
            else:
                break
        # Join the chunk: remove trailing \, newlines, and leading whitespace of continuations
        if len(chunk) > 1:
            joined = chunk[0].rstrip("\n\r")[:-1]  # first line without its trailing \
            for k in range(1, len(chunk)):
                joined += chunk[k].lstrip()
            result.append(joined)
            line_map[len(result)] = i + 1  # first original line number
            i = j
        else:
            result.append(lines[i])
            line_map[len(result)] = i + 1
            i += 1
    return "".join(result), line_map


def extract_all_strings(text: str, ext: str) -> list[dict]:
    """Extract all translatable strings with line info and match keys."""
    line_map: dict[int, int] = {}
    if ext == ".dm":
        text, line_map = _join_dm_continuations(text)
    results = []
    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        actual_lineno = line_map.get(lineno, lineno)
        for info in extract_string_info(line, ext):
            info["line_number"] = actual_lineno
            info["prefix"] = line[: info["start"]]
            info["match_key"] = content_match_key(info["content"])
            results.append(info)
    return results


def scan_file(
    target_path: Path, original_root: Path, config: Config,
    llm_cache: dict[str, str] | None = None,
    user_cache: dict[str, str] | None = None,
) -> FileResult:
    rel = target_path.relative_to(config.target_root).as_posix()
    ext = target_path.suffix.lower()
    result = FileResult(file_rel=rel)

    target_text = read_file_safe(target_path)
    if target_text is None:
        return result

    original_path = original_root / rel
    original_text = read_file_safe(original_path)
    if original_text is None:
        return result

    # Extract all strings from both files
    orig_strings = extract_all_strings(original_text, ext)
    tgt_strings = extract_all_strings(target_text, ext)

    # Build maps
    # Exact content match: English content -> list of originals
    orig_by_content: dict[str, list[dict]] = {}
    for s in orig_strings:
        c = s["content"]
        if c not in orig_by_content:
            orig_by_content[c] = []
        orig_by_content[c].append(s)

    # Context-keyed map: (prefix, match_key) -> list of original strings
    orig_by_context: dict[tuple[str, str], list[dict]] = {}
    # Match-key ordered appearance: which originals have each match_key, preserving order
    orig_by_key_ordered: dict[str, list[dict]] = {}
    for s in orig_strings:
        key = (s["prefix"], s["match_key"])
        if key not in orig_by_context:
            orig_by_context[key] = []
        orig_by_context[key].append(s)

        mk = s["match_key"]
        if mk not in orig_by_key_ordered:
            orig_by_key_ordered[mk] = []
        orig_by_key_ordered[mk].append(s)

    # Track used originals to avoid reuse
    used_originals: set[int] = set()

    def find_original(tgt_prefix: str, tgt_match_key: str, tgt_content: str) -> str:
        # 1. Exact content match
        if tgt_content in orig_by_content:
            for candidate in orig_by_content[tgt_content]:
                cid = id(candidate)
                if cid not in used_originals:
                    used_originals.add(cid)
                    return candidate["content"]

        # 2. Context match: same prefix + same tokens
        context_key = (tgt_prefix, tgt_match_key)
        if context_key in orig_by_context:
            for candidate in orig_by_context[context_key]:
                cid = id(candidate)
                if cid not in used_originals:
                    used_originals.add(cid)
                    return candidate["content"]

        # 3. Ordered key match: first unused original in appearance order with same match_key
        # Handles structural differences (continuation joins, line restructuring)
        if tgt_match_key in orig_by_key_ordered:
            for candidate in orig_by_key_ordered[tgt_match_key]:
                cid = id(candidate)
                if cid not in used_originals:
                    used_originals.add(cid)
                    return candidate["content"]

        # 4. Fallback: match by prefix alone (for simple strings with no tokens)
        if tgt_match_key == "":
            prefix_only_key = (tgt_prefix, "")
            if prefix_only_key in orig_by_context:
                for candidate in orig_by_context[prefix_only_key]:
                    cid = id(candidate)
                    if cid not in used_originals:
                        used_originals.add(cid)
                        return candidate["content"]

        return ""

    # Process target strings
    for s in tgt_strings:
        content = s["content"]
        match_key = s["match_key"]
        prefix = s["prefix"]
        line_no = s["line_number"]
        start = s["start"]
        end = s["end"]
        quote = s["quote"]
        line_text = target_text.splitlines(keepends=True)[line_no - 1].rstrip("\n\r")

        orig_content = find_original(prefix, match_key, content)
        content_unchanged = (orig_content == content)

        if content_unchanged:
            status = LineStatus.ORIGINAL
        else:
            status = LineStatus.TRANSLATED
            if orig_content:
                safe, _ = check_variables_safe(orig_content, content)
                if not safe:
                    status = LineStatus.BROKEN

        cache_key = f"{rel}:{line_no}:{orig_content or content}"
        if user_cache and cache_key in user_cache:
            status = LineStatus.USER_MODIFIED
        elif llm_cache and cache_key in llm_cache:
            status = LineStatus.LLM_TWEAKED

        ts = TranslatableString(
            file_rel=rel,
            line_number=line_no,
            line_text=line_text,
            quote=quote,
            content=content,
            original_content=orig_content,
            start=start,
            end=end,
            status=status,
        )

        if cache_key in (user_cache or {}):
            ts.user_translation = user_cache[cache_key]
        elif cache_key in (llm_cache or {}):
            ts.llm_translation = llm_cache[cache_key]

        result.strings.append(ts)

    return result
