# Guideline for Finding Translatable Strings

## Purpose
This document explains how to locate and translate user‑visible strings in the Bubberstation codebase.

## 1. Search for Relevant Keywords
A brute‑force search is the fastest way to gather candidates.

```powershell
# Find all calls to to_chat, span_notice, and span_warning
grep -R -nE "to_chat|span_notice|span_warning" code/ | head -n 200

# Find definitions that contain names or descriptions
grep -R -nE "name =" code/ | head -n 200
grep -R -nE "desc =" code/ | head -n 200
grep -R -nE "description =" code/ | head -n 200
grep -R -nE "report_message =" code/ | head -n 200
```

### What the flags mean
- `-R` – recursive search
- `-n` – show line numbers
- `-E` – extended regex (allows `|` alternation)
- `head -n 200` – limit the output to the first 200 lines for readability.

## 2. Inspect the Results
Open the files at the reported line numbers. Typical patterns:
- `to_chat(user, span_warning("..."))`
- `description = "Some message"`
- `desc = "Some message"`
- `report_message = "Some message"

## 3. Translate the Strings
Translate the literal text *inside* the quotes.  Keep the surrounding syntax unchanged.
- **Interpolate tokens** (`[name]`, `[zone_readable]`) stay untouched.
- **Escaped characters** (`\"`, `\n`) must be preserved exactly.
- **Case** – balloon alerts use `.span_warning`, `.span_notice`; keep the casing.

Example before/after:
```
# Before
to_chat(user, span_warning("You don't want to harm other living beings!"))
# After
to_chat(user, span_warning("Você não quer machucar outros seres vivos!"))
```

## 3.1 SS13-Specific Safety Rules

- Translate these DM assignment fields:
  - `description = "..."`
  - `desc = "..."`
  - `report_message = "..."`
  - `full_name = "..."`
  - and similar user-facing fields like `*_desc`, `*_description`, `*_text`, `*_message`
- Do **not** translate identifier fields:
  - `name = "drop_item"` (or other ids/keys)
  - `id`, `key`, `*_id`, `icon_state`, `*_icon_state`, `*_state`, paths/filenames
- Translate user-facing chat/examine contexts, such as:
  - `to_chat(..., span_notice("..."))`
  - `to_chat(..., span_warning("..."))`
  - `examine_list += span_warning("...")`
  - `balloon_alert(...)`, `visible_message(...)`, `tgui_alert(...)`, `alert(...)`, `input(...)`

## 4. Batch Edit Repetitive Lines
If a string appears many times, use the `edit` tool with `replaceAll:true` for a quick bulk change.

```powershell
edit(
  filePath: "path/to/file.dm",
  oldString: "You don't want to harm other living beings!",
  newString: "Você não quer machucar outros seres vivos!",
  replaceAll: true,
)
```

## 5. Verify Completeness
After translating a file or folder, run a second grep to confirm no English remains:

```powershell
grep -R -n "You" code/ | wc -l   # Should ideally be 0 for untranslatable excerpts
```

If the count is above 0, double‑check those lines.

## 6. Commit Your Changes
Stage only the files you modified:

```powershell
git add <files>
git commit -m "Translate UI strings to Portuguese"
```

## 7. Repeat
Iterate through each set of files (shuttles, traits, modules, etc.) until all visible text is translated.

---

*This guide is meant to be used by developers editing the Bubberstation repository.*
