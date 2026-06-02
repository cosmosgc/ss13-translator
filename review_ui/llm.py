from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from review_ui.cache import _restore_original_tokens
from review_ui.config import Config


@dataclass
class LLMConfig:
    api_base: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    timeout: int


def make_llm_config(config: Config) -> LLMConfig:
    return LLMConfig(
        api_base=config.llm_api_base,
        api_key=config.llm_api_key,
        model=config.llm_model,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
        timeout=config.llm_timeout,
    )


TRANSLATION_SYSTEM_PROMPT = """You are a translator for a game codebase. Translate from {source_lang} to {target_lang}.

Rules:
- Keep ALL [brackets], HTML <tags>, \\escapes, %tokens, and non-article macros like \\him exactly as-is.
- Treat English article macros like \\The, \\the, \\a, and \\an as plain words without the backslash.
- Escaped quotes are shown as normal quotes for readability; keep the quoted phrase intact.
- Translate only the English words between those special tokens.

CRITICAL: Output ONLY the translated text. No thinking, no analysis, no explanation, no notes, no step-by-step. Begin your response directly with the translation."""

STRICT_SYSTEM_PROMPT = """You are a translator for a game codebase. Translate from {source_lang} to {target_lang}.

CRITICAL RULES — FAILURE MEANS THE GAME CODE BREAKS:
- KEEP ALL [brackets] EXACTLY as they appear. DO NOT change, translate, remove, or add brackets.
- KEEP ALL \\escapes and non-article macros like \\him and \\his EXACTLY as they appear.
- Article macros like \\The, \\the, \\a, and \\an are English-only. Translate them as normal words with NO backslash.
- Escaped quotes are shown as normal quotes for readability; keep the quoted phrase intact.
- KEEP ALL HTML <tags>, %tokens, and format specifiers exactly as-is.
- Translate ONLY the English words between those special tokens.
- Your PREVIOUS attempt CHANGED or REMOVED some of these tokens. This attempt MUST fix that.
- Output ONLY the translated text. No thinking, no explanation, no notes.

Failed safety checks from the previous attempt:
{safety_issues}

Before answering, silently verify that the output contains the exact same protected tokens as the input."""

# Letters used to detect if text is actually translatable
_LETTER_RE = re.compile(r'[A-Za-zÀ-ÿ]')
_DM_ARTICLE_MACRO_TEXT = {
    "\\The": "The",
    "\\the": "the",
    "\\A": "A",
    "\\a": "a",
    "\\An": "An",
    "\\an": "an",
}
_DM_ARTICLE_MACRO_RE = re.compile(r'\\(?:[Tt]he|[Aa]n|[Aa])(?![A-Za-z])')
_DM_REQUIRED_MACRO_RE = re.compile(
    r'\\(?:[Tt]hem(?:selves)?|[Tt]heir|[Hh]im(?:self)?|[Hh]e(?:self)?|[Hh]is|[Ii]tself|[Oo]urselves|[Yy]ourselves)'
)
_BACKSLASH_WORD_RE = re.compile(r'\\[A-Za-zÀ-ÿ]+')


def _should_translate(text: str) -> bool:
    """Check if a string actually needs translation — skips empty/code-like/etc."""
    stripped = text.strip()
    if not stripped or not _LETTER_RE.search(stripped):
        return False
    if len(stripped) <= 1:
        return False
    # Code identifiers with structural symbols (underscore, dot, slash, etc.)
    if re.fullmatch(r'[A-Za-z0-9_.:/#\'-]+', stripped):
        # If it contains structural symbols, it's a code identifier
        if re.search(r'[_.:/#]', stripped):
            return False
    return True


def _extract_from_message(msg: dict) -> str | None:
    """Extract translation from a chat completion message.

    Tries (in order):
    1. OpenAI/Qwen tool calls
    2. Direct content
    3. reasoning_content with marker-based extraction
    4. reasoning_content last-quoted-string fallback
    """
    tool_result = _extract_from_tool_calls(msg)
    if tool_result:
        return _clean_quotes(tool_result)

    # 2. Direct content
    content = (msg.get("content") or "").strip()
    if content:
        return _clean_quotes(content)

    # 3. reasoning_content
    reasoning = (msg.get("reasoning_content") or "").strip()
    if not reasoning:
        return None

    return _extract_from_reasoning(reasoning)


def _extract_from_tool_calls(msg: dict) -> str | None:
    for tool_call in msg.get("tool_calls") or []:
        found = _extract_from_tool_call_payload(tool_call)
        if found:
            return found

    content = msg.get("content") or ""
    for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.DOTALL):
        found = _extract_from_tool_call_payload(match.group(1))
        if found:
            return found

    return None


def _extract_from_tool_call_payload(payload) -> str | None:
    if isinstance(payload, str):
        payload = payload.strip()
        if not payload:
            return None
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return _clean_quotes(payload)

    if not isinstance(payload, dict):
        return None

    function_payload = payload.get("function")
    if isinstance(function_payload, dict):
        arguments = function_payload.get("arguments")
        found = _extract_from_tool_call_payload(arguments)
        if found:
            return found

    arguments = payload.get("arguments")
    if arguments is not None:
        found = _extract_from_tool_call_payload(arguments)
        if found:
            return found

    for key in ("translation", "translated_text", "result", "output", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


_REASONING_MARKERS = [
    "Final Output:",
    "Final Translation:",
    "Final Polish:",
    "Final:\n",
    "\nTranslation:",
    "Draft Translation:",
    "**Final Translation:**",
    "**Translation:**",
]


def _extract_from_reasoning(reasoning: str) -> str | None:
    import re

    # Strategy A: Find text after known marker labels
    for marker in _REASONING_MARKERS:
        if marker not in reasoning:
            continue
        after = reasoning.split(marker, 1)[1].strip()
        line = after.split("\n")[0].strip()
        if not line:
            continue
        cleaned = _clean_quotes(line)
        if cleaned and len(cleaned) > 3:
            return cleaned

    # Strategy B: Scan backwards for plausible lines
    lines = [l.strip() for l in reasoning.split("\n") if l.strip()]
    for line in reversed(lines):
        cleaned = _clean_quotes(line)
        if not cleaned or len(cleaned) < 6:
            continue
        # Skip markdown list items
        if cleaned.startswith("*") or cleaned.startswith("-"):
            continue
        # Skip lines that look like thinking/instructions
        if re.match(r"^(Wait|Let|One |Check|Ens|Okay|Sure|Here|I\s|This |The (g|m|t|p|s|a|b|c|d|e|f))", cleaned):
            continue
        # Skip lines that are all ASCII (looks like English thinking)
        if cleaned.isascii() and len(cleaned) > 60:
            continue
        return cleaned

    # Strategy C: Quoted strings
    quoted = re.findall(r'"([^"]*)"', reasoning)
    if not quoted:
        quoted = re.findall(r"'([^']*)'", reasoning)
    if not quoted:
        quoted = re.findall(r"`([^`]*)`", reasoning)
    if quoted:
        # For short reasoning (quick mode), first quoted string is the early draft
        if len(reasoning) < 6000:
            for q in quoted:
                qs = q.strip()
                if len(qs) > 5 and not re.match(r"^(Wait|Let|One |Check|Ens|Okay|Sure|Here)", qs):
                    return qs
        # For long reasoning, last quoted string is the final draft
        for q in reversed(quoted):
            qs = q.strip()
            if len(qs) > 5 and not re.match(r"^(Wait|Let|One |Check|Ens|Okay|Sure|Here)", qs):
                return qs

    # Strategy D: Last non-empty line
    if lines:
        candidate = _clean_quotes(lines[-1])
        if len(candidate) > 3:
            return candidate

    return None


def _clean_quotes(text: str) -> str:
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        text = text[1:-1]
    if text.startswith("`") and text.endswith("`"):
        text = text[1:-1]
    return text.strip()


def _format_safety_issues(issues: list[str] | None) -> str:
    if not issues:
        return "- Previous translation failed protected-token validation."
    return "\n".join(f"- {issue}" for issue in issues)


def _strip_article_macro_slashes(text: str) -> str:
    return _DM_ARTICLE_MACRO_RE.sub(lambda m: _DM_ARTICLE_MACRO_TEXT[m.group(0)], text)


def _strip_unprotected_backslash_words(text: str) -> str:
    def replace(match: re.Match) -> str:
        token = match.group(0)
        if token in {"\\n", "\\r", "\\t"} or _DM_REQUIRED_MACRO_RE.fullmatch(token):
            return token
        return token[1:]

    return _BACKSLASH_WORD_RE.sub(replace, text)


def _show_escaped_quotes_to_llm(text: str) -> str:
    return text.replace('\\"', '"').replace("\\'", "'")


def _escape_literal_quotes_like_original(original: str, translation: str) -> str:
    if '\\"' in original:
        translation = re.sub(r'(?<!\\)"', r'\\"', translation)
    if "\\'" in original:
        translation = re.sub(r"(?<!\\)'", r"\\'", translation)
    return translation


async def translate_with_llm(
    text: str,
    llm_cfg: LLMConfig,
    source_lang: str = "en",
    target_lang: str = "pt-BR",
    reasoning_enabled: bool = True,
    strict: bool = False,
    safety_issues: list[str] | None = None,
) -> str | None:
    """Translate a single text string using the LLM API. Returns None on failure.

    When reasoning_enabled=False, uses lower max_tokens and extracts from
    reasoning_content immediately (faster but may be lower quality).
    When strict=True, uses a stricter system prompt emphasizing bracket preservation.
    Post-processes to restore original [bracket] variables, macros, and escapes.
    Returns None if the source looks empty or non-translatable.
    """
    if not _should_translate(text):
        return None
    safe_text = _strip_article_macro_slashes(text)
    safe_text = _show_escaped_quotes_to_llm(safe_text)

    if strict:
        prompt = STRICT_SYSTEM_PROMPT.format(
            source_lang=source_lang,
            target_lang=target_lang,
            safety_issues=_format_safety_issues(safety_issues),
        )
    else:
        prompt = TRANSLATION_SYSTEM_PROMPT.format(source_lang=source_lang, target_lang=target_lang)
    max_tokens = max(llm_cfg.max_tokens, 6144) if reasoning_enabled else 1536
    user_content = safe_text if strict else f"Translate this to {target_lang}: {safe_text}"

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
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": llm_cfg.temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            translation = _extract_from_message(msg)
            if translation:
                translation = _escape_literal_quotes_like_original(text, translation)
                translation = _strip_unprotected_backslash_words(translation)
                # Strip trailing garbage \" that the model sometimes hallucinates
                if not text.rstrip().endswith('\\"') and translation.rstrip().endswith('\\"'):
                    translation = translation.rstrip()[:-2].rstrip()
                translation = _restore_original_tokens(text, translation)
            return translation

    except httpx.TimeoutException:
        return None
    except httpx.HTTPStatusError as e:
        return None
    except Exception:
        return None


async def check_llm_connection(llm_cfg: LLMConfig) -> bool:
    """Check if the LLM server is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{llm_cfg.api_base}/models",
                headers={"Authorization": f"Bearer {llm_cfg.api_key}"},
            )
            return resp.status_code == 200
    except Exception:
        return False
