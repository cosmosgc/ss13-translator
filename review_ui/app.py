"""
SS13 Translation Review — Textual TUI
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, Static, Tree
from textual.worker import Worker, WorkerState

from review_ui.cache import ReviewCache
from review_ui.config import load_config
from review_ui.llm import check_llm_connection, make_llm_config, translate_with_llm
from review_ui.scanner import (
    STATUS_EMOJI,
    STATUS_LABEL,
    FileResult,
    LineStatus,
    TranslatableString,
    check_variables_safe,
    collect_files,
    scan_file,
)


def status_summary(strings: list[TranslatableString]) -> str:
    ks: dict[str, int] = {}
    for ts in strings:
        ks[ts.status.name] = ks.get(ts.status.name, 0) + 1
    parts = []
    for s in LineStatus:
        c = ks.get(s.name, 0)
        if c:
            parts.append(f"{STATUS_EMOJI.get(s, '?')}{c}")
    return " ".join(parts)


class TranslationReview(App):
    TITLE = "SS13 Translation Review"
    SUB_TITLE = "Original -> Target"
    CSS = """
    #file-panel { width: 40; min-width: 30; border: solid $primary; }
    #file-panel > Label { padding: 0 1; background: $surface; text-style: bold; }
    #file-tree { height: 1fr; }
    #line-panel { width: 1fr; min-width: 50; border: solid $secondary; }
    #line-panel > Label { padding: 0 1; background: $surface; text-style: bold; }
    #line-table { height: 1fr; }
    #detail-panel {
        height: 9; border: solid $accent;
        dock: bottom; padding: 0 1;
    }
    #status-bar {
        height: 3; padding: 0 1;
        background: $surface; color: $text-disabled;
    }
    .hidden { display: none; }
    DataTable { min-height: 5; }
    DataTable > .datatable--header { text-style: bold; background: $boost; }
    """

    BINDINGS = [
        Binding("f1", "show_help", "Help"),
        Binding("f2", "translate_line", "Xlate Line"),
        Binding("f3", "batch_translate", "Batch"),
        Binding("f4", "open_in_vscode", "VSCode"),
        Binding("f5", "refresh_scan", "Refresh"),
        Binding("ctrl+s", "save_cache", "Save"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.cache = ReviewCache(self.cfg.cache_dir)
        self.llm_cfg = make_llm_config(self.cfg)

        self.file_items: dict[str, FileItem] = {}
        self.all_lines: list[TranslatableString] = []
        self.selected_file_rel: str | None = None
        self.selected_line: TranslatableString | None = None
        self.selected_dir_rel: str | None = None
        self.llm_connected = False
        self.status_counts: dict[str, int] = {}
        self._row_to_line: dict[str, TranslatableString] = {}

        self._path_tree: dict = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="file-panel"):
                yield Label(" Files")
                yield Tree("Project", id="file-tree")
            with Vertical(id="line-panel"):
                yield Label(" Lines  [dim](select a file)[/]")
                yield DataTable(id="line-table")
        yield Static(id="detail-panel", classes="hidden")
        yield Static(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#line-table", DataTable).add_columns("Status", "#", "Original", "Current")
        self.query_one("#line-table", DataTable).cursor_type = "row"
        self.set_status("Loading...")
        self.check_llm()
        self.scan_project()

    # ----- Status -----

    def set_status(self, msg: str) -> None:
        self.query_one("#status-bar", Static).update(msg)

    def update_status_bar(self) -> None:
        total = len(self.all_lines)
        o = self.status_counts.get("ORIGINAL", 0)
        t = self.status_counts.get("TRANSLATED", 0)
        l = self.status_counts.get("LLM_TWEAKED", 0)
        u = self.status_counts.get("USER_MODIFIED", 0)
        b = self.status_counts.get("BROKEN", 0)
        lc = "\u2713" if self.llm_connected else "\u2717"
        self.set_status(
            f"Files: {len(self.file_items)}  Lines: {total}  "
            f"\u2705{o} \u2194\ufe0f{t} \U0001F916{l} \U0001F464{u} \u274c{b}  "
            f"LLM: {lc}  [F1]Help [F2]Xlate [F3]Batch [F4]VSCode"
        )

    # ----- LLM -----

    @work(thread=False)
    async def check_llm(self) -> None:
        self.llm_connected = await check_llm_connection(self.llm_cfg)
        self.update_status_bar()

    # ----- Scanning -----

    @work(thread=True, exit_on_error=False)
    def scan_project(self) -> None:
        self.call_from_thread(self.set_status, "Scanning target project...")
        files = collect_files(self.cfg)
        self.call_from_thread(self.set_status, f"Diffing {len(files)} files...")

        local_items: dict[str, FileItem] = {}
        local_all: list[TranslatableString] = []
        local_counts: dict[str, int] = {}
        path_tree: dict = {}

        for idx, fpath in enumerate(files):
            if idx % 100 == 0 and idx > 0:
                self.call_from_thread(self.set_status, f"Diffing... {idx}/{len(files)}")

            try:
                result = scan_file(
                    fpath, self.cfg.original_root, self.cfg,
                    self.cache.llm_cache, self.cache.user_cache,
                )
            except Exception:
                continue

            if not result.strings:
                continue

            fi = FileItem(fpath, result)
            local_items[result.file_rel] = fi
            local_all.extend(result.strings)

            for ts in result.strings:
                local_counts[ts.status.name] = local_counts.get(ts.status.name, 0) + 1

            rel = result.file_rel.replace("\\", "/")
            parts = rel.split("/")
            node = path_tree
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = None  # None marks a file

        self.call_from_thread(
            self._on_scan_complete, local_items, local_all, local_counts, path_tree,
        )

    def _on_scan_complete(
        self, items: dict[str, FileItem],
        lines: list[TranslatableString],
        counts: dict[str, int],
        path_tree: dict,
    ) -> None:
        self.file_items = items
        self.all_lines = lines
        self.status_counts = counts
        self._path_tree = path_tree

        self.update_status_bar()
        self.set_status(f"Building file tree ({len(items)} files)...")
        self.call_later(self._build_tree)

    async def _build_tree(self) -> None:
        tree = self.query_one("#file-tree", Tree)
        tree.clear()

        names = sorted(self._path_tree)
        for name in names:
            sub = self._path_tree[name]
            if sub is None:
                fi = self.file_items.get(name)
                if fi:
                    tree.root.add(
                        f"{status_summary(fi.translatable)}  {name}",
                        data={"type": "file", "rel": name},
                    )
            else:
                label = name
                dir_node = tree.root.add(
                    label, data={"type": "dir", "rel": name, "loaded": False},
                )

        tree.root.expand()
        self.set_status(
            f"Done. {len(self.file_items)} files, {len(self.all_lines)} lines. "
            "Select a file to review."
        )

    # ----- Lazy directory loading -----

    def _load_dir_children(self, tree: Tree, node: Tree.Node, dir_rel: str) -> None:
        """Add child nodes for a directory (one level deep)."""
        sub = self._path_tree
        if dir_rel:
            parts = dir_rel.split("/")
            for p in parts:
                sub = sub.get(p)
                if sub is None:
                    return

        names = sorted(sub)
        for name in names:
            child = sub[name]
            child_rel = f"{dir_rel}/{name}" if dir_rel else name
            if child is None:
                fi = self.file_items.get(child_rel)
                if fi:
                    node.add(
                        f"{status_summary(fi.translatable)}  {name}",
                        data={"type": "file", "rel": child_rel},
                    )
            else:
                node.add(
                    name,
                    data={"type": "dir", "rel": child_rel, "loaded": False},
                )

    # ----- Tree Events -----

    @on(Tree.NodeSelected)
    def on_tree_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return

        t = data.get("type")
        if t == "file":
            rel = data.get("rel", "")
            self.selected_file_rel = rel
            self.selected_dir_rel = None
            self.show_lines_for_file(rel)
        elif t == "dir":
            rel = data.get("rel", "")
            self.selected_file_rel = None
            self.selected_dir_rel = rel
            self.set_status(f"Directory selected: {rel}  (F3 = batch translate all files in dir)")

    @on(Tree.NodeExpanded)
    def on_tree_expanded(self, event: Tree.NodeExpanded) -> None:
        data = event.node.data
        if not data or data.get("type") != "dir":
            return
        if data.get("loaded"):
            return

        rel = data.get("rel", "")
        tree = self.query_one("#file-tree", Tree)
        self._load_dir_children(tree, event.node, rel)
        event.node.data["loaded"] = True

    def show_lines_for_file(self, rel: str) -> None:
        fi = self.file_items.get(rel)
        if not fi:
            return

        table = self.query_one("#line-table", DataTable)
        table.clear()
        self._row_to_line.clear()

        for ts in fi.translatable:
            row_key = str(id(ts))
            self._row_to_line[row_key] = ts

            emoji = STATUS_EMOJI.get(ts.status, "?")
            label = STATUS_LABEL.get(ts.status, "?")

            orig = ts.original_content
            curr = ts.content
            if len(orig) > 120:
                orig = orig[:120] + "..."
            if len(curr) > 120:
                curr = curr[:120] + "..."

            table.add_row(
                f"{emoji} {label}", str(ts.line_number), orig, curr, key=row_key,
            )

        if fi.translatable:
            table.move_cursor(row=0)

    # ----- Line Selection -----

    @on(DataTable.RowHighlighted)
    def on_line_selected(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key:
            return
        ts = self._row_to_line.get(str(event.row_key))
        if not ts:
            return
        self.selected_line = ts
        self.show_detail_panel(ts)

    def show_detail_panel(self, ts: TranslatableString) -> None:
        panel = self.query_one("#detail-panel", Static)
        panel.remove_class("hidden")

        emoji = STATUS_EMOJI.get(ts.status, "?")
        label = STATUS_LABEL.get(ts.status, "?")
        safe, issues = check_variables_safe(ts.original_content, ts.content)
        safe_text = "\u2705 Safe" if safe else f"\u274c Issues: {', '.join(issues)}"

        text = (
            f"[bold]{ts.file_rel}:{ts.line_number}[/bold]  {emoji} {label}\n"
        )
        if ts.original_content != ts.content:
            text += f"[bold]Original:[/bold] {ts.original_content}\n"
            text += f"[bold]Current:[/bold]  {ts.content}\n"
        else:
            text += f"[bold]Text:[/bold] {ts.content}  [dim](unchanged)[/]\n"
        if ts.llm_translation:
            text += f"[bold]LLM:[/bold] {ts.llm_translation}\n"
        elif ts.user_translation:
            text += f"[bold]User:[/bold] {ts.user_translation}\n"
        text += f"[italic]{safe_text}[/italic]"

        panel.update(text)

    # ----- Translation (single line) -----

    @work(thread=False, exit_on_error=False)
    async def action_translate_line(self) -> None:
        if not self.selected_line:
            self.set_status("No line selected")
            return
        if not self.llm_connected:
            self.set_status("LLM not connected!")
            return

        ts = self.selected_line
        source = ts.original_content
        self.set_status(f"Translating line {ts.line_number}...")

        result = await translate_with_llm(
            source, self.llm_cfg,
            self.cfg.source_lang, self.cfg.target_lang,
        )

        if result is None:
            self.set_status("Translation failed!")
            return

        cache_key = f"{ts.file_rel}:{ts.line_number}:{source}"
        self.cache.set_llm_translation(cache_key, result)
        ts.llm_translation = result

        safe, _ = check_variables_safe(source, result)
        ts.status = LineStatus.LLM_TWEAKED if safe else LineStatus.BROKEN

        self.show_lines_for_file(self.selected_file_rel or ts.file_rel)
        self.show_detail_panel(ts)
        self.cache.save()
        self.set_status(f"LLM: {result[:80]}...")

    # ----- Translation (batch) -----

    @work(thread=False, exit_on_error=False)
    async def action_batch_translate(self) -> None:
        if not self.llm_connected:
            self.set_status("LLM not connected!")
            return

        todo: list[TranslatableString] = []

        if self.selected_file_rel:
            fi = self.file_items.get(self.selected_file_rel)
            if fi:
                todo = [
                    ts for ts in fi.translatable
                    if ts.status in (LineStatus.ORIGINAL, LineStatus.TRANSLATED)
                ]
            scope = self.selected_file_rel
        elif self.selected_dir_rel:
            todo = self._collect_dir_strings(self.selected_dir_rel)
            scope = self.selected_dir_rel + "/"
        else:
            self.set_status("Select a file or directory first (F3 = batch)")
            return

        if not todo:
            self.set_status("No lines to translate in selection")
            return

        self.set_status(f"Translating {len(todo)} lines in {scope}...")
        self.query_one("#line-table", DataTable).loading = True

        count = 0
        for ts in todo:
            source = ts.original_content
            cache_key = f"{ts.file_rel}:{ts.line_number}:{source}"
            cached = self.cache.get_llm_translation(cache_key)
            if cached:
                ts.llm_translation = cached
                ts.status = LineStatus.LLM_TWEAKED
                count += 1
                continue

            result = await translate_with_llm(
                source, self.llm_cfg,
                self.cfg.source_lang, self.cfg.target_lang,
            )

            if result is None:
                self.set_status(f"Failed at line {ts.line_number} in {ts.file_rel}")
                break

            self.cache.set_llm_translation(cache_key, result)
            ts.llm_translation = result

            safe, _ = check_variables_safe(source, result)
            ts.status = LineStatus.LLM_TWEAKED if safe else LineStatus.BROKEN
            count += 1

            if count % 5 == 0:
                self.set_status(f"Translated {count}/{len(todo)} lines...")
                await asyncio.sleep(0)

        self.query_one("#line-table", DataTable).loading = False
        self.cache.save()
        self.set_status(f"Batch done: {count}/{len(todo)} lines in {scope}")
        self.update_status_bar()
        if self.selected_file_rel:
            self.show_lines_for_file(self.selected_file_rel)

    def _collect_dir_strings(self, dir_rel: str) -> list[TranslatableString]:
        prefix = dir_rel.replace("\\", "/") + "/"
        result: list[TranslatableString] = []
        for rel, fi in self.file_items.items():
            if rel.replace("\\", "/").startswith(prefix):
                for ts in fi.translatable:
                    if ts.status in (LineStatus.ORIGINAL, LineStatus.TRANSLATED):
                        result.append(ts)
        return result

    # ----- Misc Actions -----

    def action_open_in_vscode(self) -> None:
        if not self.selected_line:
            self.set_status("No line selected")
            return
        ts = self.selected_line
        abs_path = self.cfg.target_root / ts.file_rel
        try:
            subprocess.Popen(["code", "--goto", f"{abs_path}:{ts.line_number}"], shell=True)
            self.set_status(f"Opened: {abs_path}:{ts.line_number}")
        except Exception as e:
            self.set_status(f"VSCode failed: {e}")

    def action_refresh_scan(self) -> None:
        self.query_one("#line-table", DataTable).clear()
        self.set_status("Re-scanning...")
        self.cache.save()
        self.scan_project()

    def action_save_cache(self) -> None:
        self.cache.save()
        self.set_status("Cache saved")

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())


class FileItem:
    def __init__(self, path: Path, result: FileResult):
        self.path = path
        self.result = result
        self.translatable: list[TranslatableString] = result.strings

    @property
    def rel(self) -> str:
        return self.result.file_rel


class HelpScreen(Screen):
    TITLE = "Help"
    CSS = """
    Screen { align: center middle; }
    #help-box {
        width: 56; height: auto; border: solid $primary;
        padding: 1 2; background: $surface;
    }
    """
    BINDINGS = [Binding("escape,q,space,enter", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label("[bold]SS13 Translation Review[/bold]")
            yield Label("")
            yield Label("[bold]Navigation[/bold]")
            yield Static("  Click a directory to expand/collapse it")
            yield Static("  Click a file to see its translatable lines")
            yield Label("")
            yield Label("[bold]Keybindings[/bold]")
            yield Static("  F1        Show help")
            yield Static("  F2        Translate selected line with LLM")
            yield Static("  F3        Batch translate (file or entire dir)")
            yield Static("  F4        Open selected line in VS Code")
            yield Static("  F5        Re-scan project")
            yield Static("  Ctrl+S    Save cache")
            yield Static("  Q         Quit")
            yield Label("")
            yield Label("[bold]Status Emojis[/bold]")
            yield Static("  \u2705 Original   = same in original & target")
            yield Static("  \u2194\ufe0f Translated = changed (unknown origin)")
            yield Static("  \U0001F916 LLM       = changed via LLM API")
            yield Static("  \U0001F464 User      = edited by user")
            yield Static("  \u274c Broken    = variables/brackets damaged")
            yield Label("")
            yield Label("[dim]Press Esc to close[/]")


def main():
    TranslationReview().run()


if __name__ == "__main__":
    main()
