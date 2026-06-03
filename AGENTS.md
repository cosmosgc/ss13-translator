# AGENTS.md

## What this repo is

Two separate Python tools for translating SS13 hardcoded strings (`.dm`, `.js`, `.ts`, `.tsx`, `.jsx`) from English to Portuguese:

1. **Batch translator** — `translate_ss13.py` — uses Argos Translate (offline ML) to auto-translate all files in one pass.
2. **Review UI** — `review_ui/` package — compares original vs translated projects side-by-side with LLM-assisted review (Flask web UI or Textual TUI).

## Quick start

```powershell
install.bat                  # pip install -r requirements.txt
# Edit .env (copy from .env.example), set PROJECT_ROOT and DRY_RUN=true
start.bat                    # python translate_ss13.py
```

## `.env` is the single source of config

Critical vars in root `.env`:
- `PROJECT_ROOT` — path to SS13 repo
- `DRY_RUN=true` — always run dry first, inspect `translation_report.txt` + `git diff`, then set `false`
- `ARGOS_MODEL_PATH` — path to `.argosmodel` file (Argos offline model)
- `INCLUDE_EXTENSIONS`, `EXCLUDE_DIRS` — scan scope
- `BIOME_FIX_CMD` — optional auto-formatting of changed TGUI files (e.g. `bunx biome`); uses safe flags: `--formatter-enabled=true --linter-enabled=false --assist-enabled=false`

The review UI has its own `.env` vars (`ORIGINAL_ROOT`, `TARGET_ROOT`, `LLM_API_BASE`). Both tools share `INCLUDE_EXTENSIONS` and `EXCLUDE_DIRS`.

## Key workflows

- **Batch translate**: `start.bat` (or `python translate_ss13.py`)
- **Dry run with review**: `DRY_RUN=true`, run translator, inspect `translation_report.txt`, check `git diff` in SS13 repo
- **Review UI headless scan**: `review_ui\scan.bat` — compares original vs target, prints per-file string status
- **Review UI web**: `review_ui\start_web.bat` — opens Flask app on `http://127.0.0.1:5001`
- **Review UI TUI**: `python -m review_ui.app` (Textual terminal UI)
- **Build standalone EXE**: `build.bat` (PyInstaller, output in `dist/`)

## Translation heuristics (important for agents)

The scanner uses context-based filtering — an agent working on the scanner logic needs to know:

- **DM translatable assignments** (translate these): `desc`, `description`, `report_message`, `full_name`, `title`, `display_name`, `extended_desc`, `special_desc`, `death_message`, and any field ending in `_desc`, `_description`, `_text`, `_message`
- **DM context markers** (translate strings inside): `to_chat(`, `span_`, `balloon_alert(`, `visible_message(`, `examine_list`, `tgui_alert(`, `alert(`, `input(`, `stripped_input(`
- **DM skip markers** (do NOT translate): `message_admins(`, `log_admin(`, `log_game(`, `log_`, `investigate_log(`
- **TGUI/JSX**: translates any user-facing string in `.tsx`/`.jsx` files under `tgui/packages/tgui/`
- **Brackets preserved**: `[name]`, `{value}` — never translated
- **Skip Portuguese**: when `SKIP_PORTUGUESE=true`, lines already containing Portuguese-like words are not re-translated

## Caching

All under `.cache/`:
- `files_cache.json` — per-file hash to avoid re-scanning unchanged files
- `translation_cache.json` — persistent translation key-value store (avoids re-translating same source text across runs)
- `review.jsonl` — JSONL log of all translated strings (original → translated, file:line)
- `review_ui/` — LLM cache, user-edit cache, scan cache

## Utility scripts

- `revert_corrupted_dm_files.bat [repo_path] [dryrun|run]` — finds `.dm` files with certain corruption patterns and `git checkout` them from the SS13 repo. Use when the translator damages `.dm` files.

## Architecture notes

- No test framework or formal test suite. `temp_dm_test.py` is an ad-hoc parser test.
- Python 3.11+ required. Dependencies: `argostranslate`, `python-dotenv`, `tqdm`. Review UI additionally: `textual`, `httpx`, `flask`.
- No `pyproject.toml` — plain `requirements.txt` setup.
- The translator preserves BYOND macro escapes (`\The`, `\him`, `\his`, etc.), DM interpolations (`[name]`), and printf-style (`%s`, `%d`) tokens.
- **DM `\` continuations are preserved**. Multi-line `#define` macros (with `\` at end of line) are processed as groups — the translator joins them to find complete strings, translates, then reconstructs the original multi-line structure with `\`+newline restored. Strings that span continuation boundaries are handled via position shift tracking.
