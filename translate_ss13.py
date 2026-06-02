from __future__ import annotations

import logging
import os
import re
import threading
import json
import hashlib
import subprocess
import time
import queue
import codecs
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm


@dataclass
class Config:
    project_root: Path
    source_argos_code: str
    target_argos_code: str
    argos_model_path: Path | None
    auto_install_model: bool
    argos_data_dir: Path
    log_level: str
    show_progress_bar: bool
    dry_run: bool
    write_report: bool
    report_path: Path
    include_exts: set[str]
    exclude_dirs: tuple[str, ...]
    max_workers: int
    skip_portuguese: bool
    file_cache_path: Path
    reset_file_cache_on_start: bool
    file_cache_save_every: int
    max_file_bytes: int
    translator_pool_size: int
    max_line_chars: int
    skip_dm_strings_with_brackets: bool
    review_log_path: Path
    persistent_translation_cache_path: Path
    persistent_translation_cache_save_every: int
    biome_fix_cmd: str | None


def as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def resolve_config() -> Config:
    load_dotenv()
    script_dir = Path(__file__).resolve().parent
    default_root = script_dir.parent.parent
    project_root = Path(os.getenv('PROJECT_ROOT', str(default_root))).resolve()

    model_env = os.getenv('ARGOS_MODEL_PATH')
    argos_model_path = Path(model_env).resolve() if model_env else None

    include_exts = {
        ext.strip().lower()
        for ext in os.getenv('INCLUDE_EXTENSIONS', '.dm,.js,.jsx,.ts,.tsx').split(',')
        if ext.strip()
    }
    if not include_exts:
        include_exts = {'.dm'}

    exclude_dirs = tuple(
        d.strip().lower().replace('\\', '/')
        for d in os.getenv(
            'EXCLUDE_DIRS',
            '.git,node_modules,code/__DEFINES,tgui/packages/tgui-panel/dist,tgui/packages/tgui/dist,tools/ss13 translator/.argos,tools/ss13 translator/build,tools/ss13 translator/dist',
        ).split(',')
        if d.strip()
    )

    return Config(
        project_root=project_root,
        source_argos_code=os.getenv('SOURCE_ARGOS_CODE', 'en').lower(),
        target_argos_code=os.getenv('TARGET_ARGOS_CODE', 'pb').lower(),
        argos_model_path=argos_model_path,
        auto_install_model=as_bool(os.getenv('AUTO_INSTALL_MODEL'), True),
        argos_data_dir=Path(os.getenv('ARGOS_DATA_DIR', str(script_dir / '.argos'))).resolve(),
        log_level=os.getenv('LOG_LEVEL', 'INFO').upper(),
        show_progress_bar=as_bool(os.getenv('SHOW_PROGRESS_BAR'), True),
        dry_run=as_bool(os.getenv('DRY_RUN'), False),
        write_report=as_bool(os.getenv('WRITE_REPORT'), True),
        report_path=Path(os.getenv('REPORT_PATH', str(script_dir / 'translation_report.txt'))).resolve(),
        include_exts=include_exts,
        exclude_dirs=exclude_dirs,
        max_workers=max(1, int(os.getenv('MAX_WORKERS', str(os.cpu_count() or 4)))),
        skip_portuguese=as_bool(os.getenv('SKIP_PORTUGUESE'), True),
        file_cache_path=Path(
            os.getenv(
                'FILE_CACHE_PATH',
                str(
                    script_dir
                    / '.cache'
                    / f"files_{hashlib.sha1(str(project_root).encode('utf-8')).hexdigest()[:12]}.json"
                ),
            )
        ).resolve(),
        reset_file_cache_on_start=as_bool(os.getenv('RESET_FILE_CACHE_ON_START'), False),
        file_cache_save_every=max(0, int(os.getenv('FILE_CACHE_SAVE_EVERY', '0'))),
        max_file_bytes=max(0, int(os.getenv('MAX_FILE_BYTES', '1048576'))),
        translator_pool_size=max(1, int(os.getenv('TRANSLATOR_POOL_SIZE', str(max(1, min(os.cpu_count() or 4, 8)))))),
        max_line_chars=max(0, int(os.getenv('MAX_LINE_CHARS', '6000'))),
        skip_dm_strings_with_brackets=as_bool(os.getenv('SKIP_DM_STRINGS_WITH_BRACKETS'), True),
        review_log_path=Path(
            os.getenv('REVIEW_LOG_PATH', str(script_dir / '.cache' / 'review.jsonl'))
        ).resolve(),
        persistent_translation_cache_path=Path(
            os.getenv(
                'PERSISTENT_TRANSLATION_CACHE_PATH',
                str(script_dir / '.cache' / 'translation_cache.json')
            )
        ).resolve(),
        persistent_translation_cache_save_every=max(0, int(os.getenv('PERSISTENT_TRANSLATION_CACHE_SAVE_EVERY', '0'))),
        biome_fix_cmd=os.getenv('BIOME_FIX_CMD') or None,
    )


def setup_logger(level_name: str) -> logging.Logger:
    logger = logging.getLogger('ss13_translator')
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logger.addHandler(handler)
    return logger


def ensure_argos_translation(from_code: str, to_code: str, model_path: Path | None, auto_install: bool, argos_data_dir: Path):
    argos_data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('XDG_CONFIG_HOME', str(argos_data_dir))
    os.environ.setdefault('XDG_CACHE_HOME', str(argos_data_dir / 'cache'))
    os.environ.setdefault('ARGOS_PACKAGES_DIR', str(argos_data_dir / 'packages'))
    os.environ.setdefault('ARGOS_TRANSLATE_DATA_DIR', str(argos_data_dir / 'data'))

    import argostranslate.package
    import argostranslate.translate

    def find_translation():
        languages = argostranslate.translate.get_installed_languages()
        from_lang = next((lang for lang in languages if lang.code == from_code), None)
        to_lang = next((lang for lang in languages if lang.code == to_code), None)
        if not from_lang or not to_lang:
            return None
        return from_lang.get_translation(to_lang)

    translation = find_translation()
    if translation is not None:
        return translation

    if auto_install and model_path and model_path.exists():
        argostranslate.package.install_from_path(str(model_path))
        translation = find_translation()
        if translation is not None:
            return translation

    raise RuntimeError(f'No Argos translation found for {from_code}->{to_code}. Set ARGOS_MODEL_PATH or install model manually.')


class TranslatorPool:
    def __init__(self, translators: list[object]):
        self._pool: queue.SimpleQueue[object] = queue.SimpleQueue()
        for t in translators:
            self._pool.put(t)

    def translate(self, text: str) -> str:
        translator = self._pool.get()
        try:
            return translator.translate(text)
        finally:
            self._pool.put(translator)


STRING_PATTERN = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')')
LETTER_PATTERN = re.compile(r'[A-Za-zÀ-ÿ]')
WORD_PATTERN = re.compile(r"[A-Za-zÀ-ÿ']+")
ESCAPE_PATTERN = re.compile(r'\\[nrt"\'\\]')
DM_TEXT_MACRO_PATTERN = re.compile(r'\\(?:[Tt]hem(?:selves)?|[Tt]heir|[Tt]he|[Hh]im(?:self)?|[Hh]e(?:self)?|[Hh]is|[Aa]n|[Aa]|[Ii]tself|[Oo]urselves|[Yy]ourselves)')
PRINTF_PATTERN = re.compile(r'%(?:\d+\$)?[sdif]|%[sdif]')
DM_ASSIGNMENT_PATTERN = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
DM_STRING_CONTEXT_MARKERS = (
    'to_chat(',
    'span_',
    'balloon_alert(',
    'visible_message(',
    'examine_list',
    'tgui_alert(',
    'alert(',
    'input(',
    'stripped_input(',
    'stripped_multiline_input(',
    'throw_alert(',
)
DM_SKIP_CONTEXT_MARKERS = (
    'message_admins(',
    'log_admin(',
    'log_game(',
    'log_world(',
    'log_shuttle(',
    'log_econ(',
    'log_combat(',
    'log_say(',
    'log_whisper(',
    'log_emote(',
    'log_attack(',
    'log_ooc(',
    'log_pda(',
    'log_chat(',
    'log_comment(',
    'log_',
    'investigate_log(',
)
DM_FIRST_STRING_ONLY_CALLS = (
    'tgui_alert(',
    'tgui_input_text(',
    'tgui_input_number(',
    'tgui_input_list(',
    'tgui_input_color(',
    'tgui_input_filename(',
    'tgui_input_file(',
)
DM_TRANSLATABLE_ASSIGNMENTS = {
    'desc',
    'description',
    'report_message',
    'full_name',
    'title',
    'display_name',
    'prompt_name',
    'extended_desc',
    'special_desc',
    'death_message',
    'you_are_text',
    'explanation_text',
    'catalog_description',
    'menu_description',
    'scan_desc',
    'steal_hint',
    'documentation',
    'medical_record_text',
    'default_raw_text',
    'flavour_text',
    'taste_description',
    'important_text',
    'spread_text',
    'occur_text',
    'unit_name',
    'machine_name',
    'singular_name',
    'crate_name',
    'rpg_title',
    'header',
}
DM_NON_TRANSLATABLE_ASSIGNMENTS = {
    'name',
    'id',
    'key',
    'config_tag',
    'savefile_key',
    'template_id',
    'shuttle_id',
    'shuttleid',
    'puzzle_id',
    'fish_id',
    'tgui_id',
    'map_name',
    'mappath',
    'filename',
    'filepath',
    'path',
    'icon',
    'icon_state',
    'base_icon_state',
    'inhand_icon_state',
    'button_icon_state',
    'worn_icon_state',
    'overlay_icon_state',
    'overlay_state',
    'background_icon_state',
    'post_init_icon_state',
    'new_icon_state',
    'trim_state',
    'program_icon',
    'program_open_overlay',
    'light_mask',
    'light_color',
    'main_color',
    'neon_color',
    'screen_loc',
    'agent',
    'role',
    'category',
    'group',
    'species',
    'assignment',
    'location',
    'suffix',
    'prefix',
    'real_name',
    'proper_name',
}
DM_QUICK_SCAN_HINTS = (
    'to_chat(',
    'span_',
    'balloon_alert(',
    'visible_message(',
    'examine_list',
    'tgui_alert(',
    'alert(',
    'input(',
    'stripped_input(',
    'stripped_multiline_input(',
    'throw_alert(',
    ' desc =',
    ' description =',
    ' report_message =',
    ' full_name =',
    ' title =',
)
PT_HINT_CHARS = re.compile(r'[ãõáéíóúâêôàç]')
PT_COMMON_WORDS = {
    'você', 'voce', 'não', 'nao', 'para', 'com', 'uma', 'que', 'seu', 'sua', 'seus', 'suas',
    'está', 'esta', 'já', 'agora', 'como', 'isso', 'aqui', 'também', 'tambem', 'foi', 'ser',
    'tem', 'sem', 'espaço', 'espaco', 'interrompido', 'trancado', 'destrancado', 'fixado',
    'solto', 'consertando', 'escaneando', 'acesso', 'negado',
}


HARDCODED_EXCLUDE_SUFFIXES = (
    'rspack.config.ts',
    'rspack.config-dev.ts',
    'bun.lock',
    'bunfig.toml',
    'tsconfig.json',
    'package.json',
    '.prettierrc.yml',
    '.prettierignore',
    '.gitattributes',
    'global.d.ts',
    'happydom.ts',
)


def is_excluded(path: Path, config: Config) -> bool:
    lowered = path.as_posix().lower()
    if any(lowered.endswith('/' + suffix) for suffix in HARDCODED_EXCLUDE_SUFFIXES):
        return True
    return any(f'/{excluded}/' in f'/{lowered}/' or lowered.endswith('/' + excluded) for excluded in config.exclude_dirs)


def collect_files(config: Config) -> list[Path]:
    files: list[Path] = []
    for path in config.project_root.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix.lower() not in config.include_exts:
            continue
        if is_excluded(path, config):
            continue
        files.append(path)
    return sorted(files)


def load_file_cache(config: Config, logger: logging.Logger) -> dict[str, str]:
    if config.reset_file_cache_on_start and config.file_cache_path.exists():
        config.file_cache_path.unlink(missing_ok=True)
        logger.info(f'File cache reset: {config.file_cache_path}')

    if not config.file_cache_path.exists():
        config.file_cache_path.parent.mkdir(parents=True, exist_ok=True)
        config.file_cache_path.write_text("{}", encoding='utf-8')
        return {}

    try:
        data = json.loads(config.file_cache_path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_file_cache(config: Config, file_cache: dict[str, str]) -> None:
    config.file_cache_path.parent.mkdir(parents=True, exist_ok=True)
    config.file_cache_path.write_text(
        json.dumps(file_cache, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def file_signature(path: Path) -> str:
    stat = path.stat()
    return f'{stat.st_mtime_ns}:{stat.st_size}'


def read_text_preserve_encoding(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode('utf-8-sig'), 'utf-8-sig'
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1', errors='ignore'), 'latin-1'


def extract_square_bracket_tokens(text: str) -> list[str] | None:
    tokens: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != '[':
            i += 1
            continue
        depth = 0
        start = i
        while i < len(text):
            ch = text[i]
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    tokens.append(text[start : i + 1])
                    break
            i += 1
        if depth != 0:
            return None
        i += 1
    return tokens


def brackets_translation_is_safe(original: str, translated: str) -> bool:
    if ('[' not in original and ']' not in original):
        return True
    before = extract_square_bracket_tokens(original)
    after = extract_square_bracket_tokens(translated)
    if before is None or after is None:
        return False
    return before == after


def escape_dm_quotes(text: str, quote_char: str) -> str:
    result: list[str] = []
    i = 0
    bracket_depth = 0
    while i < len(text):
        if text[i] == '[':
            bracket_depth += 1
            result.append(text[i])
            i += 1
        elif text[i] == ']':
            bracket_depth = max(0, bracket_depth - 1)
            result.append(text[i])
            i += 1
        elif bracket_depth > 0:
            result.append(text[i])
            i += 1
        elif text[i] == '\\' and i + 1 < len(text):
            result.append(text[i])
            result.append(text[i + 1])
            i += 2
        elif text[i] == quote_char:
            result.append('\\' + quote_char)
            i += 1
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def load_persistent_translation_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_persistent_translation_cache(path: Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')


def append_review_log(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


def should_translate_text(text: str, line: str, ext: str, config: Config) -> bool:
    stripped = text.strip()
    if not stripped or not LETTER_PATTERN.search(stripped):
        return False
    if len(stripped) <= 1:
        return False
    if line.lstrip().startswith('//'):
        return False
    if re.fullmatch(r'[A-Za-z0-9_.:/#-]+', stripped):
        return False
    if re.fullmatch(r'[a-z0-9_.-]+', stripped):
        return False

    low_line = line.lower()
    if any(marker in low_line for marker in DM_SKIP_CONTEXT_MARKERS):
        return False
    skip_context = ('#include', 'import ', 'require(', 'icon =', 'icon_state =', 'sound(', 'resource(', 'stylesheet', 'url(')
    if any(marker in low_line for marker in skip_context):
        return False

    if ext == '.dm':
        if config.skip_dm_strings_with_brackets and ('[' in text or ']' in text):
            return False
        assign_match = DM_ASSIGNMENT_PATTERN.match(line)
        if assign_match:
            field_name = assign_match.group(1).lower()
            if field_name in DM_NON_TRANSLATABLE_ASSIGNMENTS:
                return False
            if field_name.endswith('_id') or field_name.endswith('_icon_state') or field_name.endswith('_state'):
                return False
            if (
                field_name.endswith('_desc')
                or field_name.endswith('_description')
                or field_name.endswith('_text')
                or field_name.endswith('_message')
            ):
                return True
            return field_name in DM_TRANSLATABLE_ASSIGNMENTS

        if any(marker in low_line for marker in DM_STRING_CONTEXT_MARKERS):
            return True

        return False

    return True


def looks_portuguese(text: str) -> bool:
    lower = text.lower()
    if PT_HINT_CHARS.search(lower):
        return True
    words = [w for w in WORD_PATTERN.findall(lower) if len(w) > 1]
    if not words:
        return False
    hits = sum(1 for w in words if w in PT_COMMON_WORDS)
    return hits >= 2


def translate_span_calls_inside_brackets(text: str, translator_pool: TranslatorPool, cache: dict[str, str], skip_portuguese: bool, cache_lock: threading.Lock, ext: str | None = None) -> str:
    def translate_span_expr(match: re.Match[str]) -> str:
        inner = match.group(1)
        rebuilt: list[str] = []
        pos = 0
        for sm in STRING_PATTERN.finditer(inner):
            rebuilt.append(inner[pos:sm.start()])
            quoted = sm.group(0)
            quote = quoted[0]
            content = quoted[1:-1]
            if LETTER_PATTERN.search(content):
                translated = translate_preserving_tokens(content, translator_pool, cache, skip_portuguese, cache_lock, ext, quote)
            else:
                translated = content
            rebuilt.append(quote + translated + quote)
            pos = sm.end()
        rebuilt.append(inner[pos:])
        return '[' + ''.join(rebuilt) + ']'

    return re.sub(r'\[(span_[A-Za-z0-9_]+\([^\]]*\))\]', translate_span_expr, text)


def translate_preserving_tokens(
    text: str,
    translator_pool: TranslatorPool,
    cache: dict[str, str],
    skip_portuguese: bool,
    cache_lock: threading.Lock,
    ext: str | None = None,
    quote_char: str | None = None,
) -> str:
    with cache_lock:
        cached = cache.get(text)
    if cached is not None:
        return cached

    def extract_balanced(src: str, start: int, opening: str, closing: str) -> tuple[str, int] | None:
        depth = 0
        i = start
        while i < len(src):
            ch = src[i]
            if ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    return src[start : i + 1], i + 1
            i += 1
        return None

    def extract_html_tag(src: str, start: int) -> tuple[str, int] | None:
        if src[start] != '<':
            return None
        i = start + 1
        quote: str | None = None
        while i < len(src):
            ch = src[i]
            if quote:
                if ch == quote:
                    quote = None
            else:
                if ch in ('"', "'"):
                    quote = ch
                elif ch == '>':
                    return src[start : i + 1], i + 1
            i += 1
        return None

    segments: list[tuple[bool, str]] = []
    plain_buf: list[str] = []

    def flush_plain() -> None:
        if plain_buf:
            segments.append((False, ''.join(plain_buf)))
            plain_buf.clear()

    def push_token(raw: str) -> None:
        flush_plain()
        segments.append((True, raw))

    i = 0
    while i < len(text):
        ch = text[i]

        if ch == '[':
            extracted = extract_balanced(text, i, '[', ']')
            if extracted:
                raw, new_i = extracted
                if raw.startswith('[span_'):
                    raw = translate_span_calls_inside_brackets(raw, translator_pool, cache, skip_portuguese, cache_lock, ext)
                push_token(raw)
                i = new_i
                continue

        if ch == '<':
            extracted = extract_html_tag(text, i)
            if extracted:
                raw, new_i = extracted
                push_token(raw)
                i = new_i
                continue

        if ch == '{':
            extracted = extract_balanced(text, i, '{', '}')
            if extracted:
                raw, new_i = extracted
                push_token(raw)
                i = new_i
                continue

        if ext == '.dm':
            dm_tm = DM_TEXT_MACRO_PATTERN.match(text, i)
            if dm_tm:
                push_token(dm_tm.group(0))
                i = dm_tm.end()
                continue

        esc_match = ESCAPE_PATTERN.match(text, i)
        if esc_match:
            raw = esc_match.group(0)
            push_token(raw)
            i = esc_match.end()
            continue

        printf_match = PRINTF_PATTERN.match(text, i)
        if printf_match:
            raw = printf_match.group(0)
            push_token(raw)
            i = printf_match.end()
            continue

        plain_buf.append(ch)
        i += 1
    flush_plain()

    translated_parts: list[str] = []
    for is_token, value in segments:
        if is_token:
            translated_parts.append(value)
            continue
        if not LETTER_PATTERN.search(value):
            translated_parts.append(value)
            continue
        if skip_portuguese and looks_portuguese(value):
            translated_parts.append(value)
            continue
        translated_value = translator_pool.translate(value)
        translated_value = translated_value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if ext == '.dm' and quote_char is not None and LETTER_PATTERN.search(translated_value):
            translated_value = escape_dm_quotes(translated_value, quote_char)
        stripped = translated_value.strip()
        if (stripped.startswith('"') and stripped.endswith('"')) or (stripped.startswith("'") and stripped.endswith("'")):
            inner = stripped[1:-1]
            if inner:
                translated_value = translated_value.replace(stripped, inner, 1)
        translated_parts.append(translated_value)

    translated = ''.join(translated_parts)
    with cache_lock:
        cache[text] = translated
    return translated


def extract_dm_strings(line: str) -> list[tuple[int, int, str]]:
    """Extract string boundaries in DM code, respecting [bracket] interpolation."""
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
            if ch == '\\':
                escaped = True
                i += 1
                continue
            if ch == '[':
                bracket_depth += 1
            elif ch == ']':
                bracket_depth = max(0, bracket_depth - 1)
            elif ch == quote and bracket_depth == 0:
                strings.append((start, i + 1, line[start:i + 1]))
                i += 1
                break
            i += 1
    return strings


def translate_line(line: str, translator_pool: TranslatorPool, ext: str, cache: dict[str, str], skip_portuguese: bool, cache_lock: threading.Lock, config: Config) -> tuple[str, int]:
    allowed_literal_spans: set[tuple[int, int]] | None = None
    if ext == '.dm':
        lower_line = line.lower()
        for marker in DM_FIRST_STRING_ONLY_CALLS:
            if marker in lower_line:
                allowed_literal_spans = set()
                search_from = 0
                while True:
                    marker_idx = lower_line.find(marker, search_from)
                    if marker_idx == -1:
                        break
                    q_start = None
                    i = marker_idx + len(marker)
                    while i < len(line):
                        if line[i] in ('"', "'"):
                            q_start = i
                            break
                        i += 1
                    if q_start is None:
                        search_from = marker_idx + len(marker)
                        continue
                    quote = line[q_start]
                    j = q_start + 1
                    escaped = False
                    while j < len(line):
                        ch = line[j]
                        if escaped:
                            escaped = False
                        elif ch == '\\':
                            escaped = True
                        elif ch == quote:
                            allowed_literal_spans.add((q_start, j + 1))
                            break
                        j += 1
                    search_from = marker_idx + len(marker)

    out = line
    offset = 0
    count = 0
    if '"' not in line and "'" not in line:
        return line, 0

    # Use custom DM string extractor for DM files to handle [bracket] interpolation
    if ext == '.dm':
        strings = extract_dm_strings(line)
    else:
        strings = [(m.start(), m.end(), m.group(0)) for m in STRING_PATTERN.finditer(line)]

    for start, end, quoted in strings:
        if allowed_literal_spans is not None and (start, end) not in allowed_literal_spans:
            continue

        quote = quoted[0]
        text = quoted[1:-1]
        if not should_translate_text(text, line, ext, config):
            continue

        translated = translate_preserving_tokens(text, translator_pool, cache, skip_portuguese, cache_lock, ext, quote)
        if ext == '.dm' and not brackets_translation_is_safe(text, translated):
            continue
        if translated == text:
            continue

        replacement = quote + translated + quote
        actual_start = start + offset
        actual_end = end + offset
        out = out[:actual_start] + replacement + out[actual_end:]
        offset += len(replacement) - (end - start)
        count += 1

    return out, count


def process_file(
    path: Path,
    project_root: Path,
    translator_pool: TranslatorPool,
    skip_portuguese: bool,
    dry_run: bool,
    shared_cache: dict[str, str],
    shared_cache_lock: threading.Lock,
    file_cache: dict[str, str],
    file_cache_lock: threading.Lock,
    max_file_bytes: int,
    max_line_chars: int,
    config: Config,
) -> tuple[Path, int, list[dict]]:
    rel_key = path.relative_to(project_root).as_posix()
    sig_before = file_signature(path)

    def mark_cached(sig: str) -> None:
        with file_cache_lock:
            file_cache[rel_key] = sig

    with file_cache_lock:
        cached_sig = file_cache.get(rel_key)
    if cached_sig == sig_before:
        return path, 0, []

    try:
        if max_file_bytes > 0 and path.stat().st_size > max_file_bytes:
            mark_cached(sig_before)
            return path, 0, []
        original, source_encoding = read_text_preserve_encoding(path)
    except Exception:
        mark_cached(sig_before)
        return path, 0, []

    lines = original.splitlines(keepends=True)
    new_lines: list[str] = []
    file_changes = 0
    ext = path.suffix.lower()
    review_records: list[dict] = []

    if ext == '.dm':
        lowered = original.lower()
        if not any(hint in lowered for hint in DM_QUICK_SCAN_HINTS):
            mark_cached(sig_before)
            return path, 0, []

        # Join backslash-newline continuations so multi-line strings are on one logical line
        original = re.sub(r'\\\r?\n', '', original)
        lines = original.splitlines(keepends=True)

    for lineno, line in enumerate(lines, start=1):
        if max_line_chars > 0 and len(line) > max_line_chars:
            new_lines.append(line)
            continue
        new_line, line_changes = translate_line(
            line,
            translator_pool,
            ext,
            shared_cache,
            skip_portuguese,
            shared_cache_lock,
            config,
        )
        new_lines.append(new_line)
        if line_changes > 0:
            file_changes += line_changes
            review_records.append({
                'file': rel_key,
                'line': lineno,
                'original': line,
                'translated': new_line,
                'ts': time.time(),
            })

    if file_changes > 0 and not dry_run:
        path.write_text(''.join(new_lines), encoding=source_encoding)
        sig_after = file_signature(path)
    else:
        sig_after = sig_before
    mark_cached(sig_after)
    return path, file_changes, review_records


def main() -> int:
    config = resolve_config()
    logger = setup_logger(config.log_level)

    logger.info(f'SS13 translation started for {config.project_root}')
    logger.info('Mode: DRY_RUN (no files will be written)' if config.dry_run else 'Mode: WRITE')
    logger.info(f'Workers: {config.max_workers} | Skip PT: {config.skip_portuguese}')
    files = collect_files(config)
    logger.info(f'Candidate files: {len(files)}')
    file_cache = load_file_cache(config, logger)
    logger.info(f'File cache entries: {len(file_cache)}')

    pending_files: list[Path] = []
    cached_skip_count = 0
    for path in files:
        rel_key = path.relative_to(config.project_root).as_posix()
        try:
            sig = file_signature(path)
        except OSError:
            continue
        if file_cache.get(rel_key) == sig:
            cached_skip_count += 1
            continue
        pending_files.append(path)
    pending_files.sort(key=lambda p: p.stat().st_size)
    logger.info(f'Already cached/unchanged: {cached_skip_count}')
    logger.info(f'Files queued this run: {len(pending_files)}')

    try:
        first_translator = ensure_argos_translation(
            config.source_argos_code,
            config.target_argos_code,
            config.argos_model_path,
            config.auto_install_model,
            config.argos_data_dir,
        )
        translators = [first_translator]
        for _ in range(1, config.translator_pool_size):
            translators.append(
                ensure_argos_translation(
                    config.source_argos_code,
                    config.target_argos_code,
                    config.argos_model_path,
                    False,
                    config.argos_data_dir,
                )
            )
        translator_pool = TranslatorPool(translators)
    except Exception as exc:
        logger.error(str(exc))
        return 1

    touched = 0
    string_changes = 0
    changed_files: list[Path] = []
    report_lines: list[str] = []
    shared_cache: dict[str, str] = {}
    shared_cache_lock = threading.Lock()
    file_cache_lock = threading.Lock()

    persistent_cache = load_persistent_translation_cache(config.persistent_translation_cache_path)
    if persistent_cache:
        logger.info(f'Persistent translation cache loaded: {len(persistent_cache)} entries')
        shared_cache.update(persistent_cache)
    save_persistent_cache_counter = 0

    all_review_records: list[dict] = []

    progress = tqdm(total=len(pending_files), disable=not config.show_progress_bar, desc='Translating SS13 files', unit='file')
    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = [
            executor.submit(
                process_file,
                path,
                config.project_root,
                translator_pool,
                config.skip_portuguese,
                config.dry_run,
                shared_cache,
                shared_cache_lock,
                file_cache,
                file_cache_lock,
                config.max_file_bytes,
                config.max_line_chars,
                config,
            )
            for path in pending_files
        ]
        for future in as_completed(futures):
            path, file_changes, review_records = future.result()
            progress.update(1)
            if config.file_cache_save_every > 0 and progress.n % config.file_cache_save_every == 0:
                save_file_cache(config, file_cache)
            save_persistent_cache_counter += 1
            if config.persistent_translation_cache_save_every > 0 and save_persistent_cache_counter % config.persistent_translation_cache_save_every == 0:
                save_persistent_translation_cache(config.persistent_translation_cache_path, shared_cache)
            if file_changes == 0:
                continue
            touched += 1
            string_changes += file_changes
            changed_files.append(path)
            rel = path.relative_to(config.project_root)
            report_lines.append(f'{rel}: {file_changes}')
            all_review_records.extend(review_records)
    progress.close()

    logger.info(f'Files changed: {touched}')
    logger.info(f'Strings translated: {string_changes}')
    logger.info('Mode: DRY_RUN (no files written)' if config.dry_run else 'Mode: WRITE')

    if config.biome_fix_cmd and changed_files and not config.dry_run:
        biome_files = [str(p) for p in changed_files]
        logger.info(f'Running biome fix on {len(biome_files)} changed files...')
        cmd = config.biome_fix_cmd.split() + [
            'check',
            '--formatter-enabled=true',
            '--linter-enabled=false',
            '--assist-enabled=false',
            '--write',
        ] + biome_files
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.warning(f'biome exited with code {result.returncode}: {result.stderr.strip()}')
            else:
                logger.info('biome fix completed successfully')
        except FileNotFoundError:
            logger.warning(f'biome command not found: {config.biome_fix_cmd}')
        except subprocess.TimeoutExpired:
            logger.warning('biome fix timed out after 120s')
    save_file_cache(config, file_cache)
    logger.info(f'File cache saved: {config.file_cache_path}')

    save_persistent_translation_cache(config.persistent_translation_cache_path, shared_cache)
    logger.info(f'Persistent translation cache saved: {config.persistent_translation_cache_path}')

    if all_review_records:
        append_review_log(config.review_log_path, all_review_records)
        logger.info(f'Review log appended: {config.review_log_path} ({len(all_review_records)} entries)')

    if config.write_report:
        config.report_path.parent.mkdir(parents=True, exist_ok=True)
        config.report_path.write_text('\n'.join(report_lines) + ('\n' if report_lines else ''), encoding='utf-8')
        logger.info(f'Report written: {config.report_path}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
