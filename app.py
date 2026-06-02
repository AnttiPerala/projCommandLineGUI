from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Literal


ShellKind = Literal["powershell", "cmd"]
PickerKind = Literal["file", "folder"]


PATH_PLACEHOLDER_RE = re.compile(
    r"(?P<token>"
    r"<(?P<angle>(?:input|output|source|target|src|dst|dest|file|folder|dir|directory|path)[^>]*)>"
    r"|"
    r"\{(?P<brace>(?:input|output|source|target|src|dst|dest|file|folder|dir|directory|path)[^}]*)\}"
    r"|"
    r"\$\{(?P<psbrace>(?:input|output|source|target|src|dst|dest|file|folder|dir|directory|path)[^}]*)\}"
    r")",
    re.IGNORECASE,
)

WINDOWS_PATH_RE = re.compile(
    r"(?P<quote>['\"])?"
    r"(?P<path>"
    r"(?:[A-Za-z]:\\|\\\\)[^'\"\r\n|&;<>]*"
    r")"
    r"(?P=quote)?"
)

PATH_OPTION_NAMES = {
    "path",
    "literalpath",
    "filepath",
    "fullpath",
    "destination",
    "dest",
    "target",
    "source",
    "src",
    "out",
    "outfile",
    "output",
    "input",
    "filter",
    "workingdirectory",
}

CMD_PATH_OPTIONS = {
    "/d",
    "/s",
    "/t",
    "/y",
}


@dataclass
class PathSlot:
    id: int
    start: int
    end: int
    original: str
    label: str
    picker_kind: PickerKind
    value: str = ""
    include_subfolders: bool = False


@dataclass
class Bookmark:
    id: str
    name: str
    path: str
    picker_kind: PickerKind
    group: str = ""
    tags: str = ""
    include_subfolders: bool = False


@dataclass
class ScriptTemplate:
    id: str
    name: str
    shell: ShellKind
    script: str
    group: str = ""
    tags: str = ""


def library_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / "CommandLineGUI" / "library.json"


class LibraryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bookmarks: list[Bookmark] = []
        self.templates: list[ScriptTemplate] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.bookmarks = [
            Bookmark(
                id=str(item.get("id") or uuid.uuid4()),
                name=str(item.get("name") or ""),
                path=str(item.get("path") or ""),
                picker_kind="folder" if item.get("picker_kind") == "folder" else "file",
                group=str(item.get("group") or ""),
                tags=str(item.get("tags") or ""),
                include_subfolders=bool(item.get("include_subfolders")),
            )
            for item in data.get("bookmarks", [])
            if item.get("path")
        ]
        self.templates = [
            ScriptTemplate(
                id=str(item.get("id") or uuid.uuid4()),
                name=str(item.get("name") or ""),
                shell="cmd" if item.get("shell") == "cmd" else "powershell",
                script=str(item.get("script") or ""),
                group=str(item.get("group") or ""),
                tags=str(item.get("tags") or ""),
            )
            for item in data.get("templates", [])
            if item.get("script")
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "bookmarks": [bookmark.__dict__ for bookmark in self.bookmarks],
            "templates": [template.__dict__ for template in self.templates],
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add_bookmark(self, bookmark: Bookmark) -> None:
        self.bookmarks.append(bookmark)
        self.save()

    def remove_bookmark(self, bookmark_id: str) -> None:
        self.bookmarks = [bookmark for bookmark in self.bookmarks if bookmark.id != bookmark_id]
        self.save()

    def add_template(self, template: ScriptTemplate) -> None:
        self.templates.append(template)
        self.save()

    def remove_template(self, template_id: str) -> None:
        self.templates = [template for template in self.templates if template.id != template_id]
        self.save()


def guess_picker_kind(text: str) -> PickerKind:
    lowered = text.lower()
    folder_words = ("folder", "dir", "directory", "workingdirectory", "literalpath")
    file_words = ("file", "outfile", "input", "output")
    if any(word in lowered for word in folder_words):
        return "folder"
    if any(word in lowered for word in file_words):
        return "file"
    if lowered.endswith(("\\", "/")):
        return "folder"
    if re.search(r"\.[A-Za-z0-9]{1,8}(?:$|[ \t])", lowered):
        return "file"
    return "file"


def strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def friendly_label(raw: str, fallback: str) -> str:
    cleaned = strip_quotes(raw).strip("<>{}$ ")
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    return cleaned[:80] or fallback


def ranges_overlap(a_start: int, a_end: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(a_start < b_end and b_start < a_end for b_start, b_end in ranges)


def detect_path_slots(script: str, shell: ShellKind) -> list[PathSlot]:
    slots: list[PathSlot] = []
    occupied: list[tuple[int, int]] = []

    def add_slot(start: int, end: int, original: str, label: str, kind: PickerKind) -> None:
        if ranges_overlap(start, end, occupied):
            return
        occupied.append((start, end))
        slots.append(
            PathSlot(
                id=len(slots) + 1,
                start=start,
                end=end,
                original=original,
                label=label,
                picker_kind=kind,
                value=strip_quotes(original) if original else "",
            )
        )

    for match in PATH_PLACEHOLDER_RE.finditer(script):
        raw = match.group("token")
        label = friendly_label(raw, f"Path {len(slots) + 1}")
        add_slot(match.start(), match.end(), raw, label, guess_picker_kind(label))

    for match in WINDOWS_PATH_RE.finditer(script):
        raw = match.group(0).strip()
        path = match.group("path").strip()
        add_slot(match.start(), match.end(), raw, path, guess_picker_kind(path))

    for start, end, raw, label, kind in detect_option_values(script, shell):
        add_slot(start, end, raw, label, kind)

    ordered = sorted(slots, key=lambda slot: slot.start)
    for slot_id, slot in enumerate(ordered, start=1):
        slot.id = slot_id
    return ordered


def detect_option_values(script: str, shell: ShellKind) -> list[tuple[int, int, str, str, PickerKind]]:
    try:
        lexer = shlex.shlex(script, posix=False)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []

    findings: list[tuple[int, int, str, str, PickerKind]] = []
    cursor = 0
    for index, token in enumerate(tokens[:-1]):
        normalized = token.strip("'\"")
        next_token = tokens[index + 1]
        option_name = ""

        if shell == "powershell" and normalized.startswith("-"):
            option_name = normalized.lstrip("-").lower()
        elif shell == "cmd" and normalized.startswith("/"):
            option_name = normalized.lower()

        if not option_name:
            continue

        looks_like_path_option = (
            option_name in PATH_OPTION_NAMES
            or option_name in CMD_PATH_OPTIONS
            or any(word in option_name for word in ("path", "file", "folder", "dir", "source", "target", "dest", "out"))
        )
        if not looks_like_path_option:
            continue

        value_start = script.find(next_token, cursor)
        cursor = max(cursor, value_start + len(next_token)) if value_start >= 0 else cursor
        if value_start < 0 or next_token.startswith(("-", "/")):
            continue

        value_end = value_start + len(next_token)
        label = option_name.lstrip("/")
        findings.append((value_start, value_end, next_token, label, guess_picker_kind(label)))

    return findings


def quote_for_shell(path: str, shell: ShellKind) -> str:
    escaped = path.replace('"', '\\"' if shell == "powershell" else '""')
    return f'"{escaped}"'


def display_group(group: str) -> str:
    return group.strip() or "Ungrouped"


def group_sort_key(group: str) -> tuple[int, str]:
    name = display_group(group)
    return (1 if name == "Ungrouped" else 0, name.lower())


class BookmarkDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, initial_path: str = "", initial_kind: PickerKind = "folder") -> None:
        super().__init__(master)
        self.title("Bookmark")
        self.resizable(False, False)
        self.result: Bookmark | None = None

        self.name_var = tk.StringVar()
        self.path_var = tk.StringVar(value=initial_path)
        self.kind_var = tk.StringVar(value=initial_kind)
        self.group_var = tk.StringVar()
        self.tags_var = tk.StringVar()
        self.subfolders_var = tk.BooleanVar()

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.name_var, width=42).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(body, text="Path").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.path_var, width=42).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(body, text="Browse", command=self.browse).grid(row=1, column=2, padx=(8, 0), pady=4)
        ttk.Label(body, text="Type").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(body, textvariable=self.kind_var, values=("file", "folder"), state="readonly", width=12).grid(
            row=2, column=1, sticky="w", pady=4
        )
        ttk.Checkbutton(body, text="Subfolders", variable=self.subfolders_var).grid(row=2, column=2, sticky="w", pady=4)
        ttk.Label(body, text="Group").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.group_var).grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(body, text="Tags").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.tags_var).grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Save", command=self.save).pack(side="left")

        self.transient(master)
        self.grab_set()
        self.name_var.set(Path(initial_path).name if initial_path else "")
        self.wait_visibility()
        self.focus()

    def browse(self) -> None:
        if self.kind_var.get() == "folder":
            value = filedialog.askdirectory(title="Choose bookmark folder")
        else:
            value = filedialog.askopenfilename(title="Choose bookmark file")
        if value:
            self.path_var.set(value)
            if not self.name_var.get().strip():
                self.name_var.set(Path(value).name)

    def save(self) -> None:
        path = self.path_var.get().strip()
        if not path:
            messagebox.showinfo("Missing path", "Choose a file or folder first.", parent=self)
            return
        name = self.name_var.get().strip() or Path(path).name or path
        self.result = Bookmark(
            id=str(uuid.uuid4()),
            name=name,
            path=path,
            picker_kind="folder" if self.kind_var.get() == "folder" else "file",
            group=self.group_var.get().strip(),
            tags=self.tags_var.get().strip(),
            include_subfolders=bool(self.subfolders_var.get()),
        )
        self.destroy()


class TemplateDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, shell: ShellKind, script: str) -> None:
        super().__init__(master)
        self.title("Script Template")
        self.resizable(False, False)
        self.result: ScriptTemplate | None = None

        self.name_var = tk.StringVar()
        self.shell_var = tk.StringVar(value=shell)
        self.group_var = tk.StringVar()
        self.tags_var = tk.StringVar()
        self.script = script

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.name_var, width=44).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Shell").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(body, textvariable=self.shell_var, values=("powershell", "cmd"), state="readonly", width=14).grid(
            row=1, column=1, sticky="w", pady=4
        )
        ttk.Label(body, text="Group").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.group_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Tags").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.tags_var).grid(row=3, column=1, sticky="ew", pady=4)

        actions = ttk.Frame(body)
        actions.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Save", command=self.save).pack(side="left")

        self.transient(master)
        self.grab_set()
        self.wait_visibility()
        self.focus()

    def save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showinfo("Missing name", "Name the template first.", parent=self)
            return
        self.result = ScriptTemplate(
            id=str(uuid.uuid4()),
            name=name,
            shell="cmd" if self.shell_var.get() == "cmd" else "powershell",
            script=self.script,
            group=self.group_var.get().strip(),
            tags=self.tags_var.get().strip(),
        )
        self.destroy()


class SlotRow(ttk.Frame):
    def __init__(self, master: tk.Widget, slot: PathSlot, on_change: callable, *args, **kwargs) -> None:
        super().__init__(master, *args, **kwargs)
        self.slot = slot
        self.on_change = on_change
        self.kind_var = tk.StringVar(value=slot.picker_kind)
        self.path_var = tk.StringVar(value=slot.value)
        self.subfolders_var = tk.BooleanVar(value=slot.include_subfolders)

        ttk.Label(self, text=f"{slot.id}. {slot.label}", width=24).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Combobox(
            self,
            textvariable=self.kind_var,
            values=("file", "folder"),
            width=8,
            state="readonly",
        ).grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.path_entry = ttk.Entry(self, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        ttk.Button(self, text="Browse", command=self.pick).grid(row=0, column=3, sticky="ew", padx=(0, 8))
        self.subfolders = ttk.Checkbutton(self, text="Subfolders", variable=self.subfolders_var)
        self.subfolders.grid(row=0, column=4, sticky="w")

        self.columnconfigure(2, weight=1)
        self.kind_var.trace_add("write", self.sync)
        self.path_var.trace_add("write", self.sync)
        self.subfolders_var.trace_add("write", self.sync)
        self.sync()

    def contains_widget(self, widget: tk.Widget | None) -> bool:
        while widget is not None:
            if widget is self:
                return True
            widget = widget.master
        return False

    def set_bookmark(self, bookmark: Bookmark) -> None:
        self.path_var.set(bookmark.path)
        self.kind_var.set(bookmark.picker_kind)
        self.subfolders_var.set(bookmark.include_subfolders)

    def pick(self) -> None:
        kind = self.kind_var.get()
        if kind == "folder":
            value = filedialog.askdirectory(title=f"Choose {self.slot.label}")
        else:
            value = filedialog.askopenfilename(title=f"Choose {self.slot.label}")
        if value:
            self.path_var.set(value)

    def sync(self, *_args) -> None:
        self.slot.picker_kind = self.kind_var.get()  # type: ignore[assignment]
        self.slot.value = self.path_var.get()
        self.slot.include_subfolders = bool(self.subfolders_var.get())
        if self.slot.picker_kind == "folder":
            self.subfolders.state(["!disabled"])
        else:
            if self.subfolders_var.get():
                self.subfolders_var.set(False)
            self.subfolders.state(["disabled"])
        self.on_change()


class CommandLineGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Command Line GUI")
        self.geometry("1360x800")
        self.minsize(980, 620)

        self.store = LibraryStore(library_path())
        self.shell_var = tk.StringVar(value="powershell")
        self.bookmark_group_var = tk.StringVar()
        self.bookmark_tag_var = tk.StringVar()
        self.template_group_var = tk.StringVar()
        self.template_tag_var = tk.StringVar()
        self.slots: list[PathSlot] = []
        self.slot_rows: list[SlotRow] = []
        self.bookmark_ids: list[str] = []
        self.template_ids: list[str] = []
        self.drag_bookmark: Bookmark | None = None
        self.drag_started = False
        self.drag_start_xy: tuple[int, int] | None = None
        self.drag_label: tk.Toplevel | None = None
        self.drag_hover_row: SlotRow | None = None

        self.create_widgets()
        self.bind("<Control-r>", lambda _event: self.detect_slots())
        self.bind("<Control-Return>", lambda _event: self.run_script())

    def create_widgets(self) -> None:
        style = ttk.Style(self)
        style.configure("DropTarget.TFrame", background="#dbeafe")

        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=0)
        root.rowconfigure(0, weight=1)

        main = ttk.Frame(root)
        main.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)

        toolbar = ttk.Frame(main)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="Shell").pack(side="left")
        ttk.Radiobutton(toolbar, text="PowerShell", variable=self.shell_var, value="powershell", command=self.detect_slots).pack(
            side="left", padx=(8, 0)
        )
        ttk.Radiobutton(toolbar, text="CMD", variable=self.shell_var, value="cmd", command=self.detect_slots).pack(
            side="left", padx=(8, 16)
        )
        ttk.Button(toolbar, text="Detect paths", command=self.detect_slots).pack(side="left")
        ttk.Button(toolbar, text="Copy final script", command=self.copy_final_script).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Run", command=self.run_script).pack(side="left", padx=(8, 0))

        script_panel = ttk.PanedWindow(main, orient="horizontal")
        script_panel.grid(row=1, column=0, sticky="nsew")

        input_frame = ttk.Labelframe(script_panel, text="Paste script")
        output_frame = ttk.Labelframe(script_panel, text="Final script")
        script_panel.add(input_frame, weight=1)
        script_panel.add(output_frame, weight=1)

        self.input_text = tk.Text(input_frame, wrap="word", undo=True)
        self.input_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.input_text.insert(
            "1.0",
            'Get-ChildItem -Path <folder> -Recurse | Copy-Item -Destination "C:\\Temp\\Output"\n',
        )

        self.output_text = tk.Text(output_frame, wrap="word", height=8)
        self.output_text.pack(fill="both", expand=True, padx=8, pady=8)

        slots_frame = ttk.Labelframe(main, text="Detected path pickers")
        slots_frame.grid(row=2, column=0, sticky="ew", pady=8)
        slots_frame.columnconfigure(0, weight=1)

        self.slot_canvas = tk.Canvas(slots_frame, height=150, highlightthickness=0)
        self.slot_canvas.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(slots_frame, orient="vertical", command=self.slot_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.slot_canvas.configure(yscrollcommand=scrollbar.set)
        self.slot_inner = ttk.Frame(self.slot_canvas)
        self.slot_window = self.slot_canvas.create_window((0, 0), window=self.slot_inner, anchor="nw")
        self.slot_inner.bind("<Configure>", self.resize_slots)
        self.slot_canvas.bind("<Configure>", self.resize_slot_window)

        run_frame = ttk.Labelframe(main, text="Run output")
        run_frame.grid(row=3, column=0, sticky="nsew")
        self.run_output = tk.Text(run_frame, wrap="word", height=8)
        self.run_output.pack(fill="both", expand=True, padx=8, pady=8)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var).grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self.create_library_sidebar(root)

        self.detect_slots()

    def create_library_sidebar(self, root: ttk.Frame) -> None:
        sidebar = ttk.Frame(root, width=360)
        sidebar.grid(row=0, column=1, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(sidebar)
        notebook.grid(row=0, column=0, sticky="nsew")

        bookmarks_tab = ttk.Frame(notebook, padding=8)
        templates_tab = ttk.Frame(notebook, padding=8)
        notebook.add(bookmarks_tab, text="Bookmarks")
        notebook.add(templates_tab, text="Templates")

        self.create_bookmarks_tab(bookmarks_tab)
        self.create_templates_tab(templates_tab)

    def bookmark_matches_filters(self, bookmark: Bookmark) -> bool:
        group_filter = self.bookmark_group_var.get().strip().lower()
        tag_filter = self.bookmark_tag_var.get().strip().lower()
        if group_filter and group_filter not in bookmark.group.lower():
            return False
        if tag_filter and tag_filter not in bookmark.tags.lower():
            return False
        return True

    def template_matches_filters(self, template: ScriptTemplate) -> bool:
        group_filter = self.template_group_var.get().strip().lower()
        tag_filter = self.template_tag_var.get().strip().lower()
        if group_filter and group_filter not in template.group.lower():
            return False
        if tag_filter and tag_filter not in template.tags.lower():
            return False
        return True

    def refresh_bookmarks(self) -> None:
        if not hasattr(self, "bookmarks_tree"):
            return
        open_groups = {
            re.sub(r" \(\d+\)$", "", self.bookmarks_tree.item(item_id, "text"))
            for item_id in self.bookmarks_tree.get_children()
            if self.bookmarks_tree.item(item_id, "open")
        }
        had_groups = bool(self.bookmarks_tree.get_children())
        self.bookmarks_tree.delete(*self.bookmarks_tree.get_children())
        self.bookmark_ids = []
        grouped: dict[str, list[Bookmark]] = {}
        for bookmark in self.store.bookmarks:
            if not self.bookmark_matches_filters(bookmark):
                continue
            grouped.setdefault(display_group(bookmark.group), []).append(bookmark)

        for group_name in sorted(grouped, key=group_sort_key):
            bookmarks = sorted(grouped[group_name], key=lambda item: item.name.lower())
            group_id = f"bookmark-group:{group_name}"
            group_item = self.bookmarks_tree.insert(
                "",
                "end",
                iid=group_id,
                text=f"{group_name} ({len(bookmarks)})",
                values=("", "", ""),
                open=(not had_groups and group_name != "Ungrouped") or group_name in open_groups,
                tags=("group",),
            )
            for bookmark in bookmarks:
                item_id = self.bookmarks_tree.insert(
                    group_item,
                    "end",
                    iid=f"bookmark:{bookmark.id}",
                    text=bookmark.name,
                    values=(bookmark.tags, bookmark.picker_kind, bookmark.path),
                    tags=("bookmark", bookmark.id),
                )
                self.bookmark_ids.append(bookmark.id)
                self.bookmarks_tree.set(item_id, "path", bookmark.path)

    def refresh_templates(self) -> None:
        if not hasattr(self, "templates_tree"):
            return
        open_groups = {
            re.sub(r" \(\d+\)$", "", self.templates_tree.item(item_id, "text"))
            for item_id in self.templates_tree.get_children()
            if self.templates_tree.item(item_id, "open")
        }
        had_groups = bool(self.templates_tree.get_children())
        self.templates_tree.delete(*self.templates_tree.get_children())
        self.template_ids = []
        grouped: dict[str, list[ScriptTemplate]] = {}
        for template in self.store.templates:
            if not self.template_matches_filters(template):
                continue
            grouped.setdefault(display_group(template.group), []).append(template)

        for group_name in sorted(grouped, key=group_sort_key):
            templates = sorted(grouped[group_name], key=lambda item: item.name.lower())
            group_id = f"template-group:{group_name}"
            group_item = self.templates_tree.insert(
                "",
                "end",
                iid=group_id,
                text=f"{group_name} ({len(templates)})",
                values=("", ""),
                open=(not had_groups and group_name != "Ungrouped") or group_name in open_groups,
                tags=("group",),
            )
            for template in templates:
                item_id = self.templates_tree.insert(
                    group_item,
                    "end",
                    iid=f"template:{template.id}",
                    text=template.name,
                    values=(template.tags, template.shell),
                    tags=("template", template.id),
                )
                self.template_ids.append(template.id)

    def selected_bookmark(self) -> Bookmark | None:
        selection = self.bookmarks_tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        if not item_id.startswith("bookmark:"):
            return None
        bookmark_id = item_id.removeprefix("bookmark:")
        return next((bookmark for bookmark in self.store.bookmarks if bookmark.id == bookmark_id), None)

    def selected_template(self) -> ScriptTemplate | None:
        selection = self.templates_tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        if not item_id.startswith("template:"):
            return None
        template_id = item_id.removeprefix("template:")
        return next((template for template in self.store.templates if template.id == template_id), None)

    def handle_bookmark_double_click(self, event) -> None:
        item_id = self.bookmarks_tree.identify_row(event.y)
        if item_id.startswith("bookmark-group:"):
            self.bookmarks_tree.item(item_id, open=not self.bookmarks_tree.item(item_id, "open"))
            return
        self.apply_selected_bookmark()

    def handle_template_double_click(self, event) -> None:
        item_id = self.templates_tree.identify_row(event.y)
        if item_id.startswith("template-group:"):
            self.templates_tree.item(item_id, open=not self.templates_tree.item(item_id, "open"))
            return
        self.load_selected_template()

    def apply_selected_bookmark(self) -> None:
        bookmark = self.selected_bookmark()
        if not bookmark:
            messagebox.showinfo("No bookmark selected", "Select a bookmark first.")
            return
        target = self.first_matching_slot(bookmark)
        if target is None:
            messagebox.showinfo("No path picker", "Detect or add a path placeholder before applying a bookmark.")
            return
        target.value = bookmark.path
        target.picker_kind = bookmark.picker_kind
        target.include_subfolders = bookmark.include_subfolders
        self.render_slots()
        self.update_final_script()
        self.status_var.set(f"Applied bookmark '{bookmark.name}' to picker {target.id}.")

    def first_matching_slot(self, bookmark: Bookmark) -> PathSlot | None:
        empty_matching = [
            slot for slot in self.slots if not slot.value.strip() and slot.picker_kind == bookmark.picker_kind
        ]
        if empty_matching:
            return empty_matching[0]
        empty_any = [slot for slot in self.slots if not slot.value.strip()]
        if empty_any:
            return empty_any[0]
        matching = [slot for slot in self.slots if slot.picker_kind == bookmark.picker_kind]
        if matching:
            return matching[0]
        return self.slots[0] if self.slots else None

    def copy_selected_bookmark(self) -> None:
        bookmark = self.selected_bookmark()
        if not bookmark:
            messagebox.showinfo("No bookmark selected", "Select a bookmark first.")
            return
        self.clipboard_clear()
        self.clipboard_append(bookmark.path)
        self.status_var.set(f"Copied bookmark path '{bookmark.name}'.")

    def bookmark_from_item_id(self, item_id: str) -> Bookmark | None:
        if not item_id.startswith("bookmark:"):
            return None
        bookmark_id = item_id.removeprefix("bookmark:")
        return next((bookmark for bookmark in self.store.bookmarks if bookmark.id == bookmark_id), None)

    def start_bookmark_drag(self, event) -> None:
        item_id = self.bookmarks_tree.identify_row(event.y)
        bookmark = self.bookmark_from_item_id(item_id)
        self.drag_bookmark = bookmark
        self.drag_started = False
        self.drag_start_xy = (event.x_root, event.y_root) if bookmark else None

    def move_bookmark_drag(self, event) -> None:
        if not self.drag_bookmark or not self.drag_start_xy:
            return
        dx = abs(event.x_root - self.drag_start_xy[0])
        dy = abs(event.y_root - self.drag_start_xy[1])
        if not self.drag_started and dx + dy < 6:
            return
        if not self.drag_started:
            self.drag_started = True
            self.create_drag_label(self.drag_bookmark)
            self.status_var.set("Drop the bookmark onto a path picker.")
        self.move_drag_label(event.x_root, event.y_root)
        self.set_drag_hover_row(self.slot_row_at(event.x_root, event.y_root))

    def finish_bookmark_drag(self, event) -> None:
        bookmark = self.drag_bookmark
        target = self.slot_row_at(event.x_root, event.y_root) if self.drag_started else None
        self.clear_drag_hover()
        self.destroy_drag_label()
        self.drag_bookmark = None
        self.drag_started = False
        self.drag_start_xy = None
        if not bookmark or not target:
            if bookmark:
                self.status_var.set("Bookmark drag canceled.")
            return
        target.set_bookmark(bookmark)
        self.update_final_script()
        self.status_var.set(f"Dropped bookmark '{bookmark.name}' onto picker {target.slot.id}.")

    def create_drag_label(self, bookmark: Bookmark) -> None:
        self.destroy_drag_label()
        self.drag_label = tk.Toplevel(self)
        self.drag_label.overrideredirect(True)
        self.drag_label.attributes("-topmost", True)
        label = ttk.Label(self.drag_label, text=bookmark.path, padding=(8, 4), relief="solid")
        label.pack()

    def move_drag_label(self, x_root: int, y_root: int) -> None:
        if self.drag_label:
            self.drag_label.geometry(f"+{x_root + 12}+{y_root + 12}")

    def destroy_drag_label(self) -> None:
        if self.drag_label:
            self.drag_label.destroy()
            self.drag_label = None

    def slot_row_at(self, x_root: int, y_root: int) -> SlotRow | None:
        widget = self.winfo_containing(x_root, y_root)
        for row in self.slot_rows:
            if row.contains_widget(widget):
                return row
        return None

    def set_drag_hover_row(self, row: SlotRow | None) -> None:
        if row is self.drag_hover_row:
            return
        self.clear_drag_hover()
        self.drag_hover_row = row
        if row:
            row.configure(style="DropTarget.TFrame")

    def clear_drag_hover(self) -> None:
        if self.drag_hover_row:
            self.drag_hover_row.configure(style="TFrame")
            self.drag_hover_row = None

    def add_bookmark(self) -> None:
        initial_path = ""
        initial_kind: PickerKind = "folder"
        for slot in self.slots:
            if slot.value.strip():
                initial_path = slot.value
                initial_kind = slot.picker_kind
                break
        dialog = BookmarkDialog(self, initial_path, initial_kind)
        self.wait_window(dialog)
        if dialog.result:
            self.store.add_bookmark(dialog.result)
            self.refresh_bookmarks()
            self.status_var.set(f"Added bookmark '{dialog.result.name}'.")

    def remove_selected_bookmark(self) -> None:
        bookmark = self.selected_bookmark()
        if not bookmark:
            messagebox.showinfo("No bookmark selected", "Select a bookmark first.")
            return
        if not messagebox.askyesno("Remove bookmark", f"Remove '{bookmark.name}'?"):
            return
        self.store.remove_bookmark(bookmark.id)
        self.refresh_bookmarks()
        self.status_var.set(f"Removed bookmark '{bookmark.name}'.")

    def load_selected_template(self) -> None:
        template = self.selected_template()
        if not template:
            messagebox.showinfo("No template selected", "Select a template first.")
            return
        self.shell_var.set(template.shell)
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", template.script)
        self.detect_slots()
        self.status_var.set(f"Loaded template '{template.name}'.")

    def save_current_template(self) -> None:
        script = self.input_text.get("1.0", "end-1c")
        if not script.strip():
            messagebox.showinfo("Empty script", "Paste a script before saving a template.")
            return
        dialog = TemplateDialog(self, self.shell, script)
        self.wait_window(dialog)
        if dialog.result:
            self.store.add_template(dialog.result)
            self.refresh_templates()
            self.status_var.set(f"Saved template '{dialog.result.name}'.")

    def remove_selected_template(self) -> None:
        template = self.selected_template()
        if not template:
            messagebox.showinfo("No template selected", "Select a template first.")
            return
        if not messagebox.askyesno("Remove template", f"Remove '{template.name}'?"):
            return
        self.store.remove_template(template.id)
        self.refresh_templates()
        self.status_var.set(f"Removed template '{template.name}'.")

    def create_bookmarks_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        filters = ttk.Labelframe(parent, text="Filter")
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Group").grid(row=0, column=0, sticky="w", padx=8, pady=(6, 2))
        ttk.Entry(filters, textvariable=self.bookmark_group_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=(6, 2)
        )
        ttk.Label(filters, text="Tag").grid(row=1, column=0, sticky="w", padx=8, pady=(2, 8))
        ttk.Entry(filters, textvariable=self.bookmark_tag_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=(2, 8)
        )
        self.bookmark_group_var.trace_add("write", lambda *_args: self.refresh_bookmarks())
        self.bookmark_tag_var.trace_add("write", lambda *_args: self.refresh_bookmarks())

        ttk.Label(parent, text="Double-click a bookmark to fill a matching picker.").grid(
            row=1, column=0, sticky="w", pady=(8, 4)
        )
        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.bookmarks_tree = ttk.Treeview(
            tree_frame,
            columns=("tags", "kind", "path"),
            show="tree headings",
            selectmode="browse",
            height=12,
        )
        bookmarks_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.bookmarks_tree.yview)
        self.bookmarks_tree.configure(yscrollcommand=bookmarks_scrollbar.set)
        self.bookmarks_tree.heading("#0", text="Group / Name")
        self.bookmarks_tree.heading("tags", text="Tags")
        self.bookmarks_tree.heading("kind", text="Type")
        self.bookmarks_tree.heading("path", text="Path")
        self.bookmarks_tree.column("#0", width=150, stretch=True)
        self.bookmarks_tree.column("tags", width=90, stretch=True)
        self.bookmarks_tree.column("kind", width=54, stretch=False)
        self.bookmarks_tree.column("path", width=180, stretch=True)
        self.bookmarks_tree.grid(row=0, column=0, sticky="nsew")
        bookmarks_scrollbar.grid(row=0, column=1, sticky="ns")
        self.bookmarks_tree.bind("<Double-1>", self.handle_bookmark_double_click)
        self.bookmarks_tree.bind("<ButtonPress-1>", self.start_bookmark_drag)
        self.bookmarks_tree.bind("<B1-Motion>", self.move_bookmark_drag)
        self.bookmarks_tree.bind("<ButtonRelease-1>", self.finish_bookmark_drag)

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Use", command=self.apply_selected_bookmark).pack(side="left")
        ttk.Button(actions, text="Copy", command=self.copy_selected_bookmark).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Add", command=self.add_bookmark).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Remove", command=self.remove_selected_bookmark).pack(side="left", padx=(6, 0))
        self.refresh_bookmarks()

    def create_templates_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        filters = ttk.Labelframe(parent, text="Filter")
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Group").grid(row=0, column=0, sticky="w", padx=8, pady=(6, 2))
        ttk.Entry(filters, textvariable=self.template_group_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=(6, 2)
        )
        ttk.Label(filters, text="Tag").grid(row=1, column=0, sticky="w", padx=8, pady=(2, 8))
        ttk.Entry(filters, textvariable=self.template_tag_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=(2, 8)
        )
        self.template_group_var.trace_add("write", lambda *_args: self.refresh_templates())
        self.template_tag_var.trace_add("write", lambda *_args: self.refresh_templates())

        ttk.Label(parent, text="Double-click a template to load it into the editor.").grid(
            row=1, column=0, sticky="w", pady=(8, 4)
        )
        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.templates_tree = ttk.Treeview(
            tree_frame,
            columns=("tags", "shell"),
            show="tree headings",
            selectmode="browse",
            height=12,
        )
        templates_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.templates_tree.yview)
        self.templates_tree.configure(yscrollcommand=templates_scrollbar.set)
        self.templates_tree.heading("#0", text="Group / Name")
        self.templates_tree.heading("tags", text="Tags")
        self.templates_tree.heading("shell", text="Shell")
        self.templates_tree.column("#0", width=160, stretch=True)
        self.templates_tree.column("tags", width=100, stretch=True)
        self.templates_tree.column("shell", width=84, stretch=False)
        self.templates_tree.grid(row=0, column=0, sticky="nsew")
        templates_scrollbar.grid(row=0, column=1, sticky="ns")
        self.templates_tree.bind("<Double-1>", self.handle_template_double_click)

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Load", command=self.load_selected_template).pack(side="left")
        ttk.Button(actions, text="Save current", command=self.save_current_template).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Remove", command=self.remove_selected_template).pack(side="left", padx=(6, 0))
        self.refresh_templates()

    def resize_slots(self, _event=None) -> None:
        self.slot_canvas.configure(scrollregion=self.slot_canvas.bbox("all"))

    def resize_slot_window(self, event=None) -> None:
        if event is not None:
            self.slot_canvas.itemconfigure(self.slot_window, width=event.width)

    @property
    def shell(self) -> ShellKind:
        return self.shell_var.get()  # type: ignore[return-value]

    def detect_slots(self) -> None:
        script = self.input_text.get("1.0", "end-1c")
        previous = {(slot.start, slot.end, slot.original): slot for slot in self.slots}
        self.slots = detect_path_slots(script, self.shell)
        for slot in self.slots:
            old = previous.get((slot.start, slot.end, slot.original))
            if old:
                slot.value = old.value
                slot.picker_kind = old.picker_kind
                slot.include_subfolders = old.include_subfolders
        self.render_slots()
        self.update_final_script()
        self.status_var.set(f"Detected {len(self.slots)} path picker(s).")

    def render_slots(self) -> None:
        for child in self.slot_inner.winfo_children():
            child.destroy()
        self.slot_rows.clear()

        if not self.slots:
            ttk.Label(self.slot_inner, text="No path placeholders or path-like arguments found.").pack(
                fill="x", padx=8, pady=8
            )
            return

        for slot in self.slots:
            row = SlotRow(self.slot_inner, slot, self.update_final_script)
            row.pack(fill="x", padx=8, pady=4)
            self.slot_rows.append(row)

    def build_final_script(self) -> str:
        script = self.input_text.get("1.0", "end-1c")
        parts: list[str] = []
        position = 0
        for slot in self.slots:
            parts.append(script[position : slot.start])
            replacement = quote_for_shell(slot.value, self.shell) if slot.value else slot.original
            parts.append(replacement)
            position = slot.end
        parts.append(script[position:])

        final_script = "".join(parts)
        if self.shell == "powershell":
            recurse_vars = [
                f"$env:CLGUI_PICKER_{slot.id}_INCLUDE_SUBFOLDERS = '{str(slot.include_subfolders).lower()}'"
                for slot in self.slots
                if slot.picker_kind == "folder"
            ]
            if recurse_vars:
                final_script = "\n".join(recurse_vars) + "\n" + final_script
        else:
            recurse_vars = [
                f"set CLGUI_PICKER_{slot.id}_INCLUDE_SUBFOLDERS={str(slot.include_subfolders).lower()}"
                for slot in self.slots
                if slot.picker_kind == "folder"
            ]
            if recurse_vars:
                final_script = "\n".join(recurse_vars) + "\n" + final_script
        return final_script

    def update_final_script(self) -> None:
        final_script = self.build_final_script()
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", final_script)
        self.output_text.configure(state="normal")

    def copy_final_script(self) -> None:
        script = self.build_final_script()
        self.clipboard_clear()
        self.clipboard_append(script)
        self.status_var.set("Final script copied to clipboard.")

    def run_script(self) -> None:
        script = self.build_final_script()
        if not script.strip():
            messagebox.showinfo("Nothing to run", "Paste a script first.")
            return

        command = (
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
            if self.shell == "powershell"
            else ["cmd.exe", "/d", "/s", "/c", script]
        )

        self.run_output.delete("1.0", "end")
        self.run_output.insert("1.0", "Running...\n")
        self.status_var.set("Running script...")
        self.update_idletasks()

        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=None)
        except FileNotFoundError as exc:
            messagebox.showerror("Shell not found", str(exc))
            self.status_var.set("Run failed.")
            return
        except Exception as exc:  # pragma: no cover - GUI safety net
            messagebox.showerror("Run failed", str(exc))
            self.status_var.set("Run failed.")
            return

        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(result.stderr)
        if not output:
            output.append(f"Process exited with code {result.returncode}.")
        else:
            output.append(f"\nExit code: {result.returncode}")

        self.run_output.delete("1.0", "end")
        self.run_output.insert("1.0", "\n".join(output))
        self.status_var.set(f"Finished with exit code {result.returncode}.")


def main() -> int:
    app = CommandLineGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
