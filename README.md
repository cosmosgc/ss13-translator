# SS13 Translator (Argos)

Translator focused on SS13 hardcoded strings (DM + TGUI files).

## What it does

- Scans source files (`.dm`, `.js`, `.jsx`, `.ts`, `.tsx`).
- Finds quoted string literals.
- Translates likely user-visible text using Argos.
- Preserves common placeholders/tokens:
  - BYOND interpolation like `[name]`
  - Braced tokens like `{value}`
  - printf placeholders like `%s`, `%d`
  - Escapes like `\n`
- Writes a per-file report with number of translated strings.

## Quick start

1. Configure `.env` (you can copy from `.env.example`).
2. Install dependencies:

```powershell
install.bat
```

3. Run translator:

```powershell
start.bat
```

## Important settings (`.env`)

- `PROJECT_ROOT`: SS13 repo root.
- `SOURCE_ARGOS_CODE` / `TARGET_ARGOS_CODE`: Argos language codes (`en` -> `pb` for pt-BR).
- `ARGOS_MODEL_PATH`: path to local `.argosmodel`.
- `DRY_RUN`: `true` means no files are modified.
- `INCLUDE_EXTENSIONS`: which file extensions to scan.
- `EXCLUDE_DIRS`: folders ignored during scan.
- `REPORT_PATH`: where translation report is written.

## Suggested workflow

1. Run with `DRY_RUN=true`.
2. Inspect `translation_report.txt` and `git diff`.
3. Set `DRY_RUN=false` to apply changes.
4. Review and adjust translations manually where needed.

## Notes

- This is intentionally conservative but still heuristic-based.
- Human review is required after automatic translation.
