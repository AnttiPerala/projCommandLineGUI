from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import uuid
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Literal


ShellKind = Literal["powershell", "cmd"]
PickerKind = Literal["file", "folder", "text"]
ThemeKind = Literal["light", "dark"]


ANGLE_PLACEHOLDER_RE = re.compile(r"<(?P<body>[A-Za-z][^>\r\n]*)>")

PATH_PLACEHOLDER_RE = re.compile(
    r"(?P<token>"
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

THEMES: dict[ThemeKind, dict[str, str]] = {
    "light": {
        "bg": "#f5f7fb",
        "panel": "#ffffff",
        "panel_alt": "#eef2f7",
        "header": "#ffffff",
        "border": "#d6dce6",
        "text": "#172033",
        "muted": "#64748b",
        "badge": "#e0f2fe",
        "badge_text": "#075985",
        "row_alt": "#f8fafc",
        "status_bg": "#ecfdf5",
        "status_text": "#166534",
        "field": "#ffffff",
        "field_text": "#111827",
        "console_bg": "#012456",
        "console_text": "#f8fafc",
        "accent": "#2563eb",
        "accent_hover": "#1d4ed8",
        "run": "#15803d",
        "run_hover": "#166534",
        "danger": "#991b1b",
        "drop": "#dbeafe",
        "selection": "#2563eb",
    },
    "dark": {
        "bg": "#1f232a",
        "panel": "#2a2f38",
        "panel_alt": "#343a46",
        "header": "#262b34",
        "border": "#444b57",
        "text": "#e5e7eb",
        "muted": "#aeb7c4",
        "badge": "#1e3a5f",
        "badge_text": "#bfdbfe",
        "row_alt": "#1f2933",
        "status_bg": "#173528",
        "status_text": "#bbf7d0",
        "field": "#171b22",
        "field_text": "#f3f4f6",
        "console_bg": "#101820",
        "console_text": "#f8fafc",
        "accent": "#60a5fa",
        "accent_hover": "#3b82f6",
        "run": "#22c55e",
        "run_hover": "#16a34a",
        "danger": "#f87171",
        "drop": "#334155",
        "selection": "#3b82f6",
    },
}

POWERSHELL_VALUELESS_SWITCHES = {
    "file",
    "directory",
    "recurse",
    "force",
    "verbose",
    "whatif",
    "confirm",
    "readonly",
    "hidden",
    "system",
}


@dataclass
class PathSlot:
    id: int
    start: int
    end: int
    original: str
    label: str
    picker_kind: PickerKind
    param_name: str = ""
    default_value: str = ""
    is_placeholder: bool = False
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
    builtin: bool = False


PS_RENAME_PREFIX_PROGRESS = """$items = @(Get-ChildItem -LiteralPath <folder:folder:Folder> -File -Filter <filter:text:Filter:default=*>)
if ($items.Count -eq 0) { Write-Output '100% No files matched.'; return }
for ($i = 0; $i -lt $items.Count; $i++) {
    $percent = [int](($i / $items.Count) * 100)
    Write-Output "$percent% Renaming $($i + 1) of $($items.Count)"
    Rename-Item -LiteralPath $items[$i].FullName -NewName ('<prefix:text:Prefix:default=new_->' + $items[$i].Name)
}
Write-Output '100% Complete'"""

PS_RENAME_REPLACE_PROGRESS = """$items = @(Get-ChildItem -LiteralPath <folder:folder:Folder> -File -Filter <filter:text:Filter:default=*>)
if ($items.Count -eq 0) { Write-Output '100% No files matched.'; return }
for ($i = 0; $i -lt $items.Count; $i++) {
    $percent = [int](($i / $items.Count) * 100)
    Write-Output "$percent% Renaming $($i + 1) of $($items.Count)"
    Rename-Item -LiteralPath $items[$i].FullName -NewName ($items[$i].Name -replace '<find_text:text:Find text>', '<replace_text:text:Replace with:default=>')
}
Write-Output '100% Complete'"""

PS_EXPORT_FILE_LIST_PROGRESS = """$items = @(Get-ChildItem -LiteralPath <folder:folder:Folder> -Recurse:<include_subfolders:text:Include subfolders true/false:default=$true> -File)
$rows = New-Object System.Collections.Generic.List[object]
if ($items.Count -eq 0) { Write-Output '100% No files matched.' } else {
    for ($i = 0; $i -lt $items.Count; $i++) {
        $percent = [int](($i / $items.Count) * 100)
        Write-Output "$percent% Exporting $($i + 1) of $($items.Count)"
        $rows.Add([pscustomobject]@{
            FullName = $items[$i].FullName
            Length = $items[$i].Length
            LastWriteTime = $items[$i].LastWriteTime
        })
    }
}
$rows | Export-Csv -NoTypeInformation -Encoding UTF8 <output_csv|file|Output CSV|default=C:\\Temp\\files.csv>
Write-Output '100% Complete'"""

PS_FIND_LARGE_PROGRESS = """$items = @(Get-ChildItem -LiteralPath <folder:folder:Folder> -Recurse -File)
$matches = New-Object System.Collections.Generic.List[object]
if ($items.Count -eq 0) { Write-Output '100% No files matched.' } else {
    for ($i = 0; $i -lt $items.Count; $i++) {
        $percent = [int](($i / $items.Count) * 100)
        Write-Output "$percent% Scanning $($i + 1) of $($items.Count)"
        if ($items[$i].Length -ge ([int64]<min_mb:text:Minimum MB:default=100> * 1MB)) {
            $matches.Add($items[$i])
        }
    }
}
$matches | Sort-Object Length -Descending | Select-Object FullName,@{Name='MB';Expression={[math]::Round($_.Length / 1MB, 2)}}
Write-Output '100% Complete'"""

PS_SEARCH_PROGRESS = """$files = @(Get-ChildItem -LiteralPath <folder:folder:Folder> -Filter <filter:text:File filter:default=*.txt> -Recurse -File)
if ($files.Count -eq 0) { Write-Output '100% No files matched.'; return }
for ($i = 0; $i -lt $files.Count; $i++) {
    $percent = [int](($i / $files.Count) * 100)
    Write-Output "$percent% Searching $($i + 1) of $($files.Count)"
    Select-String -LiteralPath $files[$i].FullName -Pattern '<pattern:text:Search text>'
}
Write-Output '100% Complete'"""

PS_ZIP_PROGRESS = """Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$source = <source_folder:folder:Folder>
$zipPath = <output_zip|file|Output ZIP|default=C:\\Temp\\archive.zip>
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
$root = (Resolve-Path -LiteralPath $source).Path.TrimEnd('\\', '/')
$files = @(Get-ChildItem -LiteralPath $source -Recurse -File)
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    if ($files.Count -eq 0) { Write-Output '100% No files matched.' } else {
        for ($i = 0; $i -lt $files.Count; $i++) {
            $percent = [int](($i / $files.Count) * 100)
            Write-Output "$percent% Compressing $($i + 1) of $($files.Count)"
            $relative = $files[$i].FullName.Substring($root.Length).TrimStart('\\', '/')
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $files[$i].FullName, $relative) | Out-Null
        }
    }
} finally {
    $zip.Dispose()
}
Write-Output '100% Complete'"""

PS_EXTRACT_ZIP_PROGRESS = """Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipPath = <zip_file:file:ZIP file>
$target = <target_folder:folder:Target folder>
New-Item -ItemType Directory -Force -Path $target | Out-Null
$zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $entries = @($zip.Entries | Where-Object { $_.FullName -and -not $_.FullName.EndsWith('/') })
    if ($entries.Count -eq 0) { Write-Output '100% No entries found.' } else {
        for ($i = 0; $i -lt $entries.Count; $i++) {
            $percent = [int](($i / $entries.Count) * 100)
            Write-Output "$percent% Extracting $($i + 1) of $($entries.Count)"
            $dest = Join-Path $target $entries[$i].FullName
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entries[$i], $dest, $true)
        }
    }
} finally {
    $zip.Dispose()
}
Write-Output '100% Complete'"""

PS_FFMPEG_CONVERT_PROGRESS = """$inputPath = <input_video:file:Input video>
$outputPath = <output_mp4|file|Output MP4|default=C:\\Temp\\output.mp4>
$durationText = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $inputPath
$duration = 0.0
$culture = [System.Globalization.CultureInfo]::InvariantCulture
if ($LASTEXITCODE -ne 0 -or -not [double]::TryParse($durationText, [System.Globalization.NumberStyles]::Float, $culture, [ref]$duration)) {
    Write-Error 'Could not read input media duration with ffprobe.'
    exit 1
}
& ffmpeg -y -i $inputPath -c:v libx264 -preset <preset:text:Preset:default=medium> -crf <crf:text:CRF:default=23> -c:a aac -progress pipe:1 -nostats $outputPath 2>&1 | ForEach-Object {
    if ($_ -match '^out_time_ms=(\\d+)' -and $duration -gt 0) {
        $percent = [math]::Min(100, [int]((([double]$Matches[1] / 1000000) / $duration) * 100))
        Write-Output "$percent% Encoding"
    } elseif ($_ -notmatch '^progress=') {
        Write-Output $_
    }
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output '100% Complete'"""

PS_FFMPEG_AUDIO_PROGRESS = """$inputPath = <input_media:file:Input media>
$outputPath = <output_mp3|file|Output MP3|default=C:\\Temp\\audio.mp3>
$durationText = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $inputPath
$duration = 0.0
$culture = [System.Globalization.CultureInfo]::InvariantCulture
if ($LASTEXITCODE -ne 0 -or -not [double]::TryParse($durationText, [System.Globalization.NumberStyles]::Float, $culture, [ref]$duration)) {
    Write-Error 'Could not read input media duration with ffprobe.'
    exit 1
}
& ffmpeg -y -i $inputPath -vn -codec:a libmp3lame -q:a <quality:text:Quality 0-9:default=2> -progress pipe:1 -nostats $outputPath 2>&1 | ForEach-Object {
    if ($_ -match '^out_time_ms=(\\d+)' -and $duration -gt 0) {
        $percent = [math]::Min(100, [int]((([double]$Matches[1] / 1000000) / $duration) * 100))
        Write-Output "$percent% Extracting"
    } elseif ($_ -notmatch '^progress=') {
        Write-Output $_
    }
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output '100% Complete'"""

PS_FFMPEG_RESIZE_PROGRESS = """$inputPath = <input_video:file:Input video>
$outputPath = <output_video|file|Output video|default=C:\\Temp\\resized.mp4>
$durationText = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $inputPath
$duration = 0.0
$culture = [System.Globalization.CultureInfo]::InvariantCulture
if ($LASTEXITCODE -ne 0 -or -not [double]::TryParse($durationText, [System.Globalization.NumberStyles]::Float, $culture, [ref]$duration)) {
    Write-Error 'Could not read input media duration with ffprobe.'
    exit 1
}
& ffmpeg -y -i $inputPath -vf scale=<width:text:Width:default=1280>:-2 -c:a copy -progress pipe:1 -nostats $outputPath 2>&1 | ForEach-Object {
    if ($_ -match '^out_time_ms=(\\d+)' -and $duration -gt 0) {
        $percent = [math]::Min(100, [int]((([double]$Matches[1] / 1000000) / $duration) * 100))
        Write-Output "$percent% Resizing"
    } elseif ($_ -notmatch '^progress=') {
        Write-Output $_
    }
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output '100% Complete'"""

PS_FFMPEG_BATCH_MP4_PROGRESS = """$source = <source_folder:folder:Source folder>
$output = <output_folder:folder:Output folder>
$extensions = '<extensions:text:Extensions:default=*.mov,*.avi,*.mkv,*.webm,*.mp4>'
$preset = '<preset:text:Preset:default=medium>'
$crf = '<crf:text:CRF:default=23>'
$recurse = $env:CLGUI_PICKER_1_INCLUDE_SUBFOLDERS -eq 'true'
New-Item -ItemType Directory -Force -Path $output | Out-Null
$root = (Resolve-Path -LiteralPath $source).Path.TrimEnd('\\', '/')
$patterns = $extensions -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$files = @(
    foreach ($pattern in $patterns) {
        Get-ChildItem -LiteralPath $source -Filter $pattern -File -Recurse:$recurse
    }
) | Sort-Object -Property FullName -Unique
if ($files.Count -eq 0) {
    Write-Output '100% No matching video files found.'
    return
}
$failures = New-Object System.Collections.Generic.List[string]
for ($i = 0; $i -lt $files.Count; $i++) {
    $file = $files[$i]
    $percent = [int](($i / $files.Count) * 100)
    $relative = if ($recurse) { $file.FullName.Substring($root.Length).TrimStart('\\', '/') } else { $file.Name }
    $relativeOutput = [System.IO.Path]::ChangeExtension($relative, '.mp4')
    $outputPath = Join-Path $output $relativeOutput
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
    Write-Output "$percent% Converting $($i + 1) of $($files.Count): $($file.Name)"
    & ffmpeg -y -i $file.FullName -c:v libx264 -preset $preset -crf $crf -c:a aac $outputPath 2>&1 | ForEach-Object {
        Write-Output $_
    }
    if ($LASTEXITCODE -ne 0) {
        $failures.Add($file.FullName)
        Write-Output "Failed: $($file.FullName)"
    }
}
if ($failures.Count -gt 0) {
    Write-Error "$($failures.Count) conversion(s) failed."
    exit 1
}
Write-Output '100% Complete'"""


BUILTIN_TEMPLATES: list[ScriptTemplate] = [
    ScriptTemplate(
        id="builtin.web.python_server",
        name="Python static web server",
        shell="powershell",
        group="Web",
        tags="python,http,server,localhost",
        builtin=True,
        script="Set-Location <serve_folder:folder:Folder to serve>\npython -m http.server <port:text:Port:default=8000> --bind 127.0.0.1",
    ),
    ScriptTemplate(
        id="builtin.web.open_url",
        name="Open URL",
        shell="powershell",
        group="Web",
        tags="browser,url,open",
        builtin=True,
        script="Start-Process '<url:text:URL:default=http://127.0.0.1:8000/>'",
    ),
    ScriptTemplate(
        id="builtin.robocopy.mirror",
        name="Mirror folder",
        shell="cmd",
        group="Robocopy",
        tags="copy,mirror,backup",
        builtin=True,
        script="robocopy <source_folder:folder:Source> <target_folder:folder:Target> /MIR /ETA /R:<retries:text:Retries:default=2> /W:<wait_seconds:text:Wait seconds:default=2>",
    ),
    ScriptTemplate(
        id="builtin.robocopy.incremental",
        name="Incremental folder copy",
        shell="cmd",
        group="Robocopy",
        tags="copy,backup,incremental",
        builtin=True,
        script="robocopy <source_folder:folder:Source> <target_folder:folder:Target> /E /XO /ETA /R:<retries:text:Retries:default=2> /W:<wait_seconds:text:Wait seconds:default=2>",
    ),
    ScriptTemplate(
        id="builtin.robocopy.dry_run",
        name="Dry-run copy preview",
        shell="cmd",
        group="Robocopy",
        tags="copy,preview,dry-run",
        builtin=True,
        script="robocopy <source_folder:folder:Source> <target_folder:folder:Target> /E /L /ETA /R:0 /W:0",
    ),
    ScriptTemplate(
        id="builtin.rename.prefix",
        name="Add prefix to files",
        shell="powershell",
        group="Rename",
        tags="rename,prefix,files",
        builtin=True,
        script=PS_RENAME_PREFIX_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.rename.replace",
        name="Replace text in filenames",
        shell="powershell",
        group="Rename",
        tags="rename,replace,files",
        builtin=True,
        script=PS_RENAME_REPLACE_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.files.list_csv",
        name="Export file list to CSV",
        shell="powershell",
        group="Files",
        tags="list,csv,inventory",
        builtin=True,
        script=PS_EXPORT_FILE_LIST_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.files.find_large",
        name="Find large files",
        shell="powershell",
        group="Files",
        tags="search,size,cleanup",
        builtin=True,
        script=PS_FIND_LARGE_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.files.hash",
        name="Calculate file hash",
        shell="powershell",
        group="Files",
        tags="hash,checksum,sha256",
        builtin=True,
        script="Get-FileHash -Algorithm <algorithm:text:Algorithm:default=SHA256> <file:file:File>",
    ),
    ScriptTemplate(
        id="builtin.search.grep",
        name="Search text in files",
        shell="powershell",
        group="Search",
        tags="grep,find,text",
        builtin=True,
        script=PS_SEARCH_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.archive.zip",
        name="Create ZIP archive",
        shell="powershell",
        group="Archive",
        tags="zip,archive,compress",
        builtin=True,
        script=PS_ZIP_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.archive.extract",
        name="Extract ZIP archive",
        shell="powershell",
        group="Archive",
        tags="zip,extract,unpack",
        builtin=True,
        script=PS_EXTRACT_ZIP_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.ffmpeg.convert_mp4",
        name="Convert video to MP4",
        shell="powershell",
        group="FFmpeg",
        tags="video,convert,mp4",
        builtin=True,
        script=PS_FFMPEG_CONVERT_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.ffmpeg.extract_audio",
        name="Extract audio to MP3",
        shell="powershell",
        group="FFmpeg",
        tags="audio,mp3,extract",
        builtin=True,
        script=PS_FFMPEG_AUDIO_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.ffmpeg.resize",
        name="Resize video",
        shell="powershell",
        group="FFmpeg",
        tags="video,resize,scale",
        builtin=True,
        script=PS_FFMPEG_RESIZE_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.ffmpeg.batch_convert_mp4",
        name="Batch convert folder to MP4",
        shell="powershell",
        group="FFmpeg",
        tags="video,batch,convert,mp4,folder",
        builtin=True,
        script=PS_FFMPEG_BATCH_MP4_PROGRESS,
    ),
    ScriptTemplate(
        id="builtin.network.ping",
        name="Ping host",
        shell="cmd",
        group="Network",
        tags="ping,network,dns",
        builtin=True,
        script="ping <host:text:Host:default=127.0.0.1>",
    ),
    ScriptTemplate(
        id="builtin.network.port_test",
        name="Test TCP port",
        shell="powershell",
        group="Network",
        tags="tcp,port,network",
        builtin=True,
        script="Test-NetConnection -ComputerName <host:text:Host:default=127.0.0.1> -Port <port:text:Port:default=8000>",
    ),
    ScriptTemplate(
        id="builtin.system.processes",
        name="Find running process",
        shell="powershell",
        group="System",
        tags="process,task,system",
        builtin=True,
        script="Get-Process | Where-Object { $_.ProcessName -like '*<process_name:text:Process name>*' } | Sort-Object ProcessName",
    ),
    ScriptTemplate(
        id="builtin.system.env_path",
        name="Show PATH entries",
        shell="powershell",
        group="Environment",
        tags="env,path,system",
        builtin=True,
        script="$env:Path -split ';' | Where-Object { $_ } | Sort-Object",
    ),
]


def library_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / "CommandLineGUI" / "library.json"


def logs_path() -> Path:
    return Path(__file__).resolve().parent / "logs"


class LibraryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bookmarks: list[Bookmark] = []
        self.templates: list[ScriptTemplate] = []
        self.hidden_builtin_templates: set[str] = set()
        self.theme: ThemeKind = "light"
        self.save_output_logs = True
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        self.hidden_builtin_templates = {
            str(template_id) for template_id in data.get("hidden_builtin_templates", []) if template_id
        }
        raw_theme = data.get("theme") or data.get("settings", {}).get("theme")
        self.theme = "dark" if raw_theme == "dark" else "light"
        self.save_output_logs = bool(data.get("save_output_logs", data.get("settings", {}).get("save_output_logs", True)))
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
                builtin=False,
            )
            for item in data.get("templates", [])
            if item.get("script")
        ]

    def visible_templates(self) -> list[ScriptTemplate]:
        builtins = [template for template in BUILTIN_TEMPLATES if template.id not in self.hidden_builtin_templates]
        user_templates = [template for template in self.templates if not template.builtin]
        return builtins + user_templates

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "bookmarks": [bookmark.__dict__ for bookmark in self.bookmarks],
            "templates": [
                {
                    "id": template.id,
                    "name": template.name,
                    "shell": template.shell,
                    "script": template.script,
                    "group": template.group,
                    "tags": template.tags,
                }
                for template in self.templates
                if not template.builtin
            ],
            "hidden_builtin_templates": sorted(self.hidden_builtin_templates),
            "theme": self.theme,
            "save_output_logs": self.save_output_logs,
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def set_theme(self, theme: ThemeKind) -> None:
        self.theme = "dark" if theme == "dark" else "light"
        self.save()

    def set_save_output_logs(self, enabled: bool) -> None:
        self.save_output_logs = bool(enabled)
        self.save()

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
        if any(template.id == template_id for template in BUILTIN_TEMPLATES):
            self.hidden_builtin_templates.add(template_id)
        else:
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


def normalize_param_name(value: str, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return name or fallback.lower().replace(" ", "_")


def title_from_name(value: str) -> str:
    cleaned = value.strip().replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in cleaned.split())


def parse_placeholder(raw: str) -> tuple[str, str, PickerKind, str]:
    body = raw.strip("<>{}$ ")
    separator = "|" if "|" in body else ":"
    pieces = [piece.strip() for piece in body.split(separator) if piece.strip()]
    base = pieces[0] if pieces else body
    name = normalize_param_name(base, "parameter")
    label = title_from_name(base) or "Parameter"
    kind = guess_picker_kind(base)
    default = ""

    for piece in pieces[1:]:
        lowered = piece.lower()
        if lowered in {"file", "folder", "text"}:
            kind = lowered  # type: ignore[assignment]
        elif lowered in {"dir", "directory"}:
            kind = "folder"
        elif lowered.startswith("type="):
            type_value = lowered.split("=", 1)[1].strip()
            if type_value in {"file", "folder", "text"}:
                kind = type_value  # type: ignore[assignment]
            elif type_value in {"dir", "directory"}:
                kind = "folder"
        elif lowered.startswith("label="):
            label = piece.split("=", 1)[1].strip() or label
        elif lowered.startswith("default="):
            default = piece.split("=", 1)[1].strip()
        elif not default and "=" not in piece:
            label = piece

    return name, label, kind, default


def looks_like_placeholder(raw: str) -> bool:
    body = raw.strip("<>{}$ ")
    lowered = body.lower()
    if any(marker in lowered for marker in (":file", ":folder", ":text", "|file", "|folder", "|text", "default=", "label=", "type=")):
        return True
    first = re.split(r"[:|]", lowered, maxsplit=1)[0]
    return any(word in first for word in ("input", "output", "source", "target", "src", "dst", "dest", "file", "folder", "dir", "directory", "path"))


def replacement_for_slot(slot: PathSlot, shell: ShellKind) -> str:
    if not slot.value:
        return slot.original
    if slot.picker_kind == "text":
        return slot.value
    return quote_for_shell(slot.value, shell)


def ranges_overlap(a_start: int, a_end: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(a_start < b_end and b_start < a_end for b_start, b_end in ranges)


def detect_path_slots(script: str, shell: ShellKind) -> list[PathSlot]:
    slots: list[PathSlot] = []
    occupied: list[tuple[int, int]] = []

    def add_slot(
        start: int,
        end: int,
        original: str,
        label: str,
        kind: PickerKind,
        param_name: str = "",
        default_value: str = "",
        is_placeholder: bool = False,
    ) -> None:
        if ranges_overlap(start, end, occupied):
            return
        occupied.append((start, end))
        initial_value = default_value if is_placeholder else strip_quotes(original)
        slots.append(
            PathSlot(
                id=len(slots) + 1,
                start=start,
                end=end,
                original=original,
                label=label,
                picker_kind=kind,
                param_name=param_name,
                default_value=default_value,
                is_placeholder=is_placeholder,
                value=initial_value,
            )
        )

    for match in ANGLE_PLACEHOLDER_RE.finditer(script):
        raw = match.group(0)
        if not looks_like_placeholder(raw):
            continue
        name, label, kind, default = parse_placeholder(raw)
        add_slot(match.start(), match.end(), raw, label, kind, name, default, True)

    for match in PATH_PLACEHOLDER_RE.finditer(script):
        raw = match.group("token")
        name, label, kind, default = parse_placeholder(raw)
        add_slot(match.start(), match.end(), raw, label, kind, name, default, True)

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
            if not re.fullmatch(r"[a-z][a-z0-9]*", option_name):
                continue
            if option_name in POWERSHELL_VALUELESS_SWITCHES:
                continue
        elif shell == "cmd" and normalized.startswith("/"):
            option_name = normalized.lower()
            if not re.fullmatch(r"/[a-z0-9]+", option_name):
                continue

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
        if (
            value_start < 0
            or next_token.startswith(("-", "/", "$", "(", "{"))
            or next_token in {"|", "{", "}", "(", ")"}
        ):
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


def is_output_file_slot(slot: PathSlot) -> bool:
    text = f"{slot.param_name} {slot.label}".lower()
    return slot.picker_kind == "file" and any(
        word in text for word in ("output", "out", "target", "dest", "save", "result")
    )


def default_file_extension(slot: PathSlot) -> str:
    for candidate in (slot.default_value, slot.value):
        suffix = Path(candidate.strip()).suffix
        if suffix:
            return suffix
    return ""


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


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self.schedule)
        widget.bind("<Leave>", self.hide)
        widget.bind("<ButtonPress>", self.hide)

    def schedule(self, _event=None) -> None:
        self.cancel()
        self.after_id = self.widget.after(500, self.show)

    def cancel(self) -> None:
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def show(self) -> None:
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.tip,
            text=self.text,
            justify="left",
            background="#111827",
            foreground="#f9fafb",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=5,
            wraplength=320,
            font=("Segoe UI", 9),
        )
        label.pack()

    def hide(self, _event=None) -> None:
        self.cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


def add_tooltip(widget: tk.Widget, text: str) -> tk.Widget:
    ToolTip(widget, text)
    return widget


class SlotRow(ttk.Frame):
    def __init__(self, master: tk.Widget, slot: PathSlot, on_change: callable, *args, **kwargs) -> None:
        super().__init__(master, *args, **kwargs)
        self.slot = slot
        self.on_change = on_change
        self.kind_var = tk.StringVar(value=slot.picker_kind)
        self.path_var = tk.StringVar(value=slot.value)
        self.subfolders_var = tk.BooleanVar(value=slot.include_subfolders)

        label_text = f"{slot.id}. {slot.label}"
        if slot.param_name:
            label_text += f"  ({slot.param_name})"
        ttk.Label(self, text=label_text, width=30).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.kind_combo = ttk.Combobox(
            self,
            textvariable=self.kind_var,
            values=("file", "folder", "text"),
            width=8,
            state="disabled" if slot.is_placeholder else "readonly",
        )
        self.kind_combo.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.path_entry = ttk.Entry(self, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        self.browse_button = ttk.Button(self, text="Browse", command=self.pick)
        self.browse_button.grid(row=0, column=3, sticky="ew", padx=(0, 8))
        self.subfolders = ttk.Checkbutton(self, text="Subfolders", variable=self.subfolders_var)
        self.subfolders.grid(row=0, column=4, sticky="w")
        add_tooltip(self.kind_combo, "Parameter type. Template placeholders lock this so the script gets the value it expects.")
        add_tooltip(self.path_entry, "Value inserted into the script for this parameter.")
        add_tooltip(self.browse_button, "Choose a file or folder with the normal Windows picker.")
        add_tooltip(self.subfolders, "Marks this folder parameter as recursive for templates that support subfolders.")

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

    def set_bookmark(self, bookmark: Bookmark) -> bool:
        if (
            self.slot.is_placeholder
            and self.slot.picker_kind in {"file", "folder"}
            and bookmark.picker_kind != self.slot.picker_kind
        ):
            return False
        self.path_var.set(bookmark.path)
        if bookmark.picker_kind in {"file", "folder"} and not self.slot.is_placeholder:
            self.kind_var.set(bookmark.picker_kind)
        self.subfolders_var.set(bookmark.include_subfolders if self.kind_var.get() == "folder" else False)
        return True

    def pick(self) -> None:
        kind = self.kind_var.get()
        if kind == "folder":
            value = filedialog.askdirectory(title=f"Choose {self.slot.label}")
        elif kind == "file":
            if is_output_file_slot(self.slot):
                extension = default_file_extension(self.slot)
                kwargs = {"title": f"Choose {self.slot.label}"}
                if extension:
                    kwargs["defaultextension"] = extension
                value = filedialog.asksaveasfilename(**kwargs)
            else:
                value = filedialog.askopenfilename(title=f"Choose {self.slot.label}")
        else:
            value = ""
        if value:
            self.path_var.set(value)

    def sync(self, *_args) -> None:
        self.slot.picker_kind = self.kind_var.get()  # type: ignore[assignment]
        self.slot.value = self.path_var.get()
        self.slot.include_subfolders = bool(self.subfolders_var.get())
        if self.slot.picker_kind == "folder":
            self.browse_button.state(["!disabled"])
            self.subfolders.state(["!disabled"])
        elif self.slot.picker_kind == "file":
            self.browse_button.state(["!disabled"])
            if self.subfolders_var.get():
                self.subfolders_var.set(False)
            self.subfolders.state(["disabled"])
        else:
            self.browse_button.state(["disabled"])
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
        self.theme_var = tk.StringVar(value=self.store.theme)
        self.save_output_logs_var = tk.BooleanVar(value=self.store.save_output_logs)
        self.bookmark_group_var = tk.StringVar()
        self.bookmark_tag_var = tk.StringVar()
        self.template_group_var = tk.StringVar()
        self.template_tag_var = tk.StringVar()
        self.slots: list[PathSlot] = []
        self.slot_rows: list[SlotRow] = []
        self.bookmark_ids: list[str] = []
        self.template_ids: list[str] = []
        self.framed_text_widgets: list[tk.Text] = []
        self.drag_bookmark: Bookmark | None = None
        self.drag_started = False
        self.drag_start_xy: tuple[int, int] | None = None
        self.drag_label: tk.Toplevel | None = None
        self.drag_hover_row: SlotRow | None = None
        self.running_process: subprocess.Popen | None = None
        self.output_queue: queue.Queue[str | None] = queue.Queue()
        self.process_streams_open = 0
        self.current_script = ""
        self.current_script_file: str | None = None
        self.progress_mode = "idle"

        self.create_widgets()
        self.bind("<Control-r>", lambda _event: self.detect_slots())
        self.bind("<Control-Return>", lambda _event: self.run_script())

    @property
    def theme(self) -> ThemeKind:
        return "dark" if self.theme_var.get() == "dark" else "light"

    def icon_path(self) -> Path:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        return base / "assets" / "CommandLineGUI.ico"

    def image_path(self) -> Path:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        return base / "assets" / "CommandLineGUI.png"

    def set_app_icon(self) -> None:
        icon = self.icon_path()
        if not icon.exists():
            return
        try:
            self.iconbitmap(str(icon))
        except tk.TclError:
            pass

    def text_widget_colors(self) -> dict[str, str]:
        palette = THEMES[self.theme]
        return {
            "background": palette["console_bg"],
            "foreground": palette["console_text"],
            "insertbackground": palette["console_text"],
            "selectbackground": palette["selection"],
            "selectforeground": "#ffffff",
        }

    def configure_app_theme(self) -> None:
        palette = THEMES[self.theme]
        self.configure(background=palette["bg"])
        self.option_add("*Font", "{Segoe UI} 9")
        self.option_add("*TCombobox*Listbox.background", palette["field"])
        self.option_add("*TCombobox*Listbox.foreground", palette["field_text"])
        self.option_add("*TCombobox*Listbox.selectBackground", palette["selection"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        self.style.configure(".", background=palette["bg"], foreground=palette["text"], fieldbackground=palette["field"])
        self.style.configure("TFrame", background=palette["bg"])
        self.style.configure("Panel.TFrame", background=palette["panel"])
        self.style.configure("Header.TFrame", background=palette["header"])
        self.style.configure("Toolbar.TFrame", background=palette["panel"])
        self.style.configure("TLabel", background=palette["bg"], foreground=palette["text"])
        self.style.configure("Muted.TLabel", background=palette["bg"], foreground=palette["muted"])
        self.style.configure("Title.TLabel", background=palette["header"], foreground=palette["text"], font="{Segoe UI} 13 bold")
        self.style.configure("Subtitle.TLabel", background=palette["header"], foreground=palette["muted"], font="{Segoe UI} 9")
        self.style.configure("Badge.TLabel", background=palette["badge"], foreground=palette["badge_text"], font="{Segoe UI} 9 bold", padding=(10, 4))
        self.style.configure("Status.TLabel", background=palette["status_bg"], foreground=palette["status_text"], font="{Segoe UI} 9 bold", padding=(10, 4))
        self.style.configure("TLabelframe", background=palette["bg"], foreground=palette["text"], bordercolor=palette["border"])
        self.style.configure("TLabelframe.Label", background=palette["bg"], foreground=palette["text"], font="{Segoe UI} 9 bold")
        self.style.configure("TButton", background=palette["panel_alt"], foreground=palette["text"], bordercolor=palette["border"], padding=(10, 5))
        self.style.map("TButton", background=[("active", palette["panel"]), ("pressed", palette["border"])])
        self.style.configure("TEntry", fieldbackground=palette["field"], foreground=palette["field_text"], bordercolor=palette["border"], insertcolor=palette["field_text"])
        self.style.configure("TCombobox", fieldbackground=palette["field"], foreground=palette["field_text"], bordercolor=palette["border"], arrowcolor=palette["text"])
        self.style.configure("TCheckbutton", background=palette["bg"], foreground=palette["text"])
        self.style.configure("TRadiobutton", background=palette["bg"], foreground=palette["text"])
        self.style.configure("TNotebook", background=palette["bg"], borderwidth=0)
        self.style.configure("TNotebook.Tab", background=palette["panel_alt"], foreground=palette["text"], padding=(12, 6))
        self.style.map("TNotebook.Tab", background=[("selected", palette["panel"])], foreground=[("selected", palette["text"])])
        self.style.configure("Treeview", background=palette["field"], foreground=palette["field_text"], fieldbackground=palette["field"], bordercolor=palette["border"], rowheight=24)
        self.style.configure("Treeview.Heading", background=palette["panel_alt"], foreground=palette["text"], relief="flat")
        self.style.map("Treeview", background=[("selected", palette["selection"])], foreground=[("selected", "#ffffff")])
        self.style.configure("Horizontal.TProgressbar", background=palette["run"], troughcolor=palette["panel_alt"], bordercolor=palette["border"], lightcolor=palette["run"], darkcolor=palette["run"])
        self.style.configure("DropTarget.TFrame", background=palette["drop"])

    def apply_theme_to_widgets(self) -> None:
        palette = THEMES[self.theme]
        self.console_colors = self.text_widget_colors()
        for widget in (self.input_text, self.output_text, self.run_output):
            widget.configure(**self.console_colors)
        self.slot_canvas.configure(background=palette["panel"])
        self.run_button.configure(
            bg=palette["run"],
            activebackground=palette["run_hover"],
            fg="white",
            activeforeground="white",
        )
        self.stop_button.configure(
            bg=palette["panel_alt"],
            activebackground=palette["panel"],
            fg=palette["danger"],
            activeforeground=palette["danger"],
        )
        self.update_shell_badge()
        self.update_theme_badge()
        self.configure_tree_tags()
        for widget in getattr(self, "framed_text_widgets", []):
            widget.configure(
                highlightthickness=1,
                highlightbackground=palette["border"],
                highlightcolor=palette["accent"],
                relief="flat",
                padx=10,
                pady=8,
            )

    def configure_tree_tags(self) -> None:
        palette = THEMES[self.theme]
        for tree_name in ("bookmarks_tree", "templates_tree"):
            if not hasattr(self, tree_name):
                continue
            tree = getattr(self, tree_name)
            tree.tag_configure("group", background=palette["panel_alt"], foreground=palette["text"], font="{Segoe UI} 9 bold")
            tree.tag_configure("odd", background=palette["field"], foreground=palette["field_text"])
            tree.tag_configure("even", background=palette["row_alt"], foreground=palette["field_text"])

    def update_shell_badge(self) -> None:
        if hasattr(self, "shell_badge_var"):
            self.shell_badge_var.set(f"Shell: {self.shell_display_name()}")

    def update_theme_badge(self) -> None:
        if hasattr(self, "theme_badge_var"):
            self.theme_badge_var.set(f"Skin: {self.theme.title()}")

    def create_widgets(self) -> None:
        self.set_app_icon()
        self.style = ttk.Style(self)
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self.console_font = ("Consolas", 10)
        self.console_colors = self.text_widget_colors()
        self.configure_app_theme()

        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=0)
        root.rowconfigure(1, weight=1)

        self.create_header(root)

        main = ttk.Frame(root)
        main.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)

        toolbar = ttk.Frame(main, style="Toolbar.TFrame", padding=(8, 8))
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        detect_button = ttk.Button(toolbar, text="Detect paths", command=self.detect_slots)
        detect_button.pack(side="left")
        add_tooltip(
            detect_button,
            "Scans the script for placeholders and path-like arguments, then creates editable parameter fields below.",
        )
        copy_button = ttk.Button(toolbar, text="Copy final script", command=self.copy_final_script)
        copy_button.pack(side="left", padx=(8, 0))
        add_tooltip(
            copy_button,
            "Copies the fully substituted script from the Final Script tab to the clipboard.",
        )
        select_all_button = ttk.Button(toolbar, text="Select all", command=self.select_all_active_script)
        select_all_button.pack(side="left", padx=(8, 0))
        add_tooltip(select_all_button, "Selects all text in the visible script tab.")
        copy_active_button = ttk.Button(toolbar, text="Copy", command=self.copy_active_script_text)
        copy_active_button.pack(side="left", padx=(8, 0))
        add_tooltip(copy_active_button, "Copies selected text from the visible script tab, or all of it if nothing is selected.")
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=(14, 2))
        self.run_button = tk.Button(
            toolbar,
            text="Run",
            command=self.run_script,
            bg=THEMES[self.theme]["run"],
            fg="white",
            activebackground=THEMES[self.theme]["run_hover"],
            activeforeground="white",
            disabledforeground="#d1d5db",
            font=("Segoe UI", 10, "bold"),
            padx=24,
            pady=4,
            relief="raised",
            borderwidth=1,
        )
        self.run_button.pack(side="left", padx=(12, 0))
        self.stop_button = tk.Button(
            toolbar,
            text="Stop",
            command=self.stop_script,
            bg=THEMES[self.theme]["panel_alt"],
            fg=THEMES[self.theme]["danger"],
            activebackground=THEMES[self.theme]["panel"],
            activeforeground=THEMES[self.theme]["danger"],
            disabledforeground="#9ca3af",
            font=("Segoe UI", 9, "bold"),
            padx=14,
            pady=3,
            relief="raised",
            borderwidth=1,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        add_tooltip(self.run_button, "Runs the final script with the selected shell.")
        add_tooltip(self.stop_button, "Stops the currently running script and any child processes.")

        self.script_tabs = ttk.Notebook(main)
        self.script_tabs.grid(row=1, column=0, sticky="nsew")
        input_frame = ttk.Frame(self.script_tabs, padding=8)
        output_frame = ttk.Frame(self.script_tabs, padding=8)
        input_frame.columnconfigure(0, weight=1)
        input_frame.rowconfigure(0, weight=1)
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        self.script_tabs.add(input_frame, text="Paste Script")
        self.script_tabs.add(output_frame, text="Final Script")
        add_tooltip(self.script_tabs, "Switch between your editable script and the script that will be executed.")

        self.input_text = tk.Text(input_frame, wrap="word", undo=True, font=self.console_font, **self.console_colors)
        self.input_text.grid(row=0, column=0, sticky="nsew")
        self.framed_text_widgets.append(self.input_text)
        self.input_text.insert(
            "1.0",
            'Get-ChildItem -Path <source_folder:folder:Source> -Filter <filter:text:default=*.txt> -Recurse | Copy-Item -Destination <target_folder:folder:Target>\n',
        )
        add_tooltip(
            self.input_text,
            "Paste or edit a PowerShell/CMD script. Use placeholders like <input:file:Input file> for form fields.",
        )

        self.output_text = tk.Text(output_frame, wrap="none", height=8, font=self.console_font, **self.console_colors)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        self.framed_text_widgets.append(self.output_text)
        output_xscroll = ttk.Scrollbar(output_frame, orient="horizontal", command=self.output_text.xview)
        output_xscroll.grid(row=1, column=0, sticky="ew")
        self.output_text.configure(xscrollcommand=output_xscroll.set)
        add_tooltip(self.output_text, "Preview of the exact script after parameter values are inserted.")

        slots_frame = ttk.Labelframe(main, text="Parameters")
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
        run_body = ttk.Frame(run_frame)
        run_body.pack(fill="both", expand=True, padx=8, pady=8)
        run_body.columnconfigure(0, weight=1)
        run_body.rowconfigure(1, weight=1)
        progress_row = ttk.Frame(run_body)
        progress_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        progress_row.columnconfigure(0, weight=1)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate", maximum=100, variable=self.progress_var)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_label_var = tk.StringVar(value="Idle")
        ttk.Label(progress_row, textvariable=self.progress_label_var, width=18).grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.run_output = tk.Text(run_body, wrap="none", height=8, font=self.console_font, **self.console_colors)
        self.run_output.grid(row=1, column=0, sticky="nsew")
        self.framed_text_widgets.append(self.run_output)
        run_xscroll = ttk.Scrollbar(run_body, orient="horizontal", command=self.run_output.xview)
        run_xscroll.grid(row=2, column=0, sticky="ew")
        self.run_output.configure(xscrollcommand=run_xscroll.set)
        add_tooltip(self.progress_bar, "Shows activity while a script runs, then switches to a percentage when output contains progress values.")
        add_tooltip(self.run_output, "Live stdout and stderr from the running command, plus the exact script launched.")

        self.create_library_sidebar(root)

        self.apply_theme_to_widgets()
        self.detect_slots()

    def create_header(self, root: ttk.Frame) -> None:
        header = ttk.Frame(root, style="Header.TFrame", padding=(12, 10))
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        header.columnconfigure(1, weight=1)

        self.logo_image = None
        image = self.image_path()
        if image.exists():
            try:
                self.logo_image = tk.PhotoImage(file=str(image)).subsample(4, 4)
                ttk.Label(header, image=self.logo_image, style="Title.TLabel").grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))
            except tk.TclError:
                pass

        ttk.Label(header, text="Command Line GUI", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(
            header,
            text="Turn scripts into reusable forms with Windows pickers, templates, and live output.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=1, sticky="w")

        self.shell_badge_var = tk.StringVar(value=f"Shell: {self.shell_display_name()}")
        self.theme_badge_var = tk.StringVar(value=f"Skin: {self.theme.title()}")
        ttk.Label(header, textvariable=self.shell_badge_var, style="Badge.TLabel").grid(row=0, column=2, sticky="e", padx=(12, 0))
        ttk.Label(header, textvariable=self.theme_badge_var, style="Badge.TLabel").grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=1, column=2, columnspan=2, sticky="e", padx=(12, 0), pady=(6, 0))
        add_tooltip(self.status_label, "Shows the latest app action or run status.")

    def create_library_sidebar(self, root: ttk.Frame) -> None:
        sidebar = ttk.Frame(root, width=360)
        sidebar.grid(row=1, column=1, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(sidebar)
        notebook.grid(row=0, column=0, sticky="nsew")

        bookmarks_tab = ttk.Frame(notebook, padding=8)
        templates_tab = ttk.Frame(notebook, padding=8)
        settings_tab = ttk.Frame(notebook, padding=8)
        notebook.add(bookmarks_tab, text="Bookmarks")
        notebook.add(templates_tab, text="Templates")
        notebook.add(settings_tab, text="Settings")

        self.create_bookmarks_tab(bookmarks_tab)
        self.create_templates_tab(templates_tab)
        self.create_settings_tab(settings_tab)

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
        if template.shell != self.shell:
            return False
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

        row_index = 0
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
                row_tag = "even" if row_index % 2 == 0 else "odd"
                item_id = self.bookmarks_tree.insert(
                    group_item,
                    "end",
                    iid=f"bookmark:{bookmark.id}",
                    text=bookmark.name,
                    values=(bookmark.tags, bookmark.picker_kind, bookmark.path),
                    tags=("bookmark", bookmark.id, row_tag),
                )
                row_index += 1
                self.bookmark_ids.append(bookmark.id)
                self.bookmarks_tree.set(item_id, "path", bookmark.path)
        self.configure_tree_tags()

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
        for template in self.store.visible_templates():
            if not self.template_matches_filters(template):
                continue
            grouped.setdefault(display_group(template.group), []).append(template)

        row_index = 0
        for group_name in sorted(grouped, key=group_sort_key):
            templates = sorted(grouped[group_name], key=lambda item: item.name.lower())
            group_id = f"template-group:{group_name}"
            group_item = self.templates_tree.insert(
                "",
                "end",
                iid=group_id,
                text=f"{group_name} ({len(templates)})",
                values=("",),
                open=(not had_groups and group_name != "Ungrouped") or group_name in open_groups,
                tags=("group",),
            )
            for template in templates:
                row_tag = "even" if row_index % 2 == 0 else "odd"
                item_id = self.templates_tree.insert(
                    group_item,
                    "end",
                    iid=f"template:{template.id}",
                    text=template.name,
                    values=(template.tags,),
                    tags=("template", template.id, row_tag),
                )
                row_index += 1
                self.template_ids.append(template.id)
        self.configure_tree_tags()

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
        return next((template for template in self.store.visible_templates() if template.id == template_id), None)

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
        if (
            target.is_placeholder
            and target.picker_kind in {"file", "folder"}
            and bookmark.picker_kind != target.picker_kind
        ):
            messagebox.showinfo(
                "Bookmark type mismatch",
                f"'{bookmark.name}' is a {bookmark.picker_kind} bookmark, but picker {target.id} expects a {target.picker_kind}.",
            )
            return
        target.value = bookmark.path
        if not target.is_placeholder:
            target.picker_kind = bookmark.picker_kind
        target.include_subfolders = bookmark.include_subfolders if target.picker_kind == "folder" else False
        self.render_slots()
        self.update_final_script()
        self.status_var.set(f"Applied bookmark '{bookmark.name}' to picker {target.id}.")

    def first_matching_slot(self, bookmark: Bookmark) -> PathSlot | None:
        empty_matching = [
            slot for slot in self.slots if not slot.value.strip() and slot.picker_kind == bookmark.picker_kind
        ]
        if empty_matching:
            return empty_matching[0]
        matching = [slot for slot in self.slots if slot.picker_kind == bookmark.picker_kind]
        if matching:
            return matching[0]
        return None

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
        if not target.set_bookmark(bookmark):
            messagebox.showinfo(
                "Bookmark type mismatch",
                f"'{bookmark.name}' is a {bookmark.picker_kind} bookmark, but picker {target.slot.id} expects a {target.slot.picker_kind}.",
            )
            return
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
        action = "Hide built-in template" if template.builtin else "Remove template"
        detail = "Hide" if template.builtin else "Remove"
        if not messagebox.askyesno(action, f"{detail} '{template.name}'?"):
            return
        self.store.remove_template(template.id)
        self.refresh_templates()
        status_action = "Hidden" if template.builtin else "Removed"
        self.status_var.set(f"{status_action} template '{template.name}'.")

    def restore_builtin_templates(self) -> None:
        if not self.store.hidden_builtin_templates:
            self.status_var.set("All built-in templates are already visible.")
            return
        self.store.hidden_builtin_templates.clear()
        self.store.save()
        self.refresh_templates()
        self.status_var.set("Restored built-in templates.")

    def create_bookmarks_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        filters = ttk.Labelframe(parent, text="Filter")
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Group").grid(row=0, column=0, sticky="w", padx=8, pady=(6, 2))
        bookmark_group_entry = ttk.Entry(filters, textvariable=self.bookmark_group_var)
        bookmark_group_entry.grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=(6, 2)
        )
        ttk.Label(filters, text="Tag").grid(row=1, column=0, sticky="w", padx=8, pady=(2, 8))
        bookmark_tag_entry = ttk.Entry(filters, textvariable=self.bookmark_tag_var)
        bookmark_tag_entry.grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=(2, 8)
        )
        add_tooltip(bookmark_group_entry, "Show bookmarks whose group contains this text.")
        add_tooltip(bookmark_tag_entry, "Show bookmarks whose tags contain this text.")
        self.bookmark_group_var.trace_add("write", lambda *_args: self.refresh_bookmarks())
        self.bookmark_tag_var.trace_add("write", lambda *_args: self.refresh_bookmarks())

        bookmark_hint = ttk.Label(parent, text="Double-click a bookmark to fill a matching picker.")
        bookmark_hint.grid(
            row=1, column=0, sticky="w", pady=(8, 4)
        )
        add_tooltip(bookmark_hint, "You can also drag a bookmark onto a compatible parameter field.")
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
        add_tooltip(self.bookmarks_tree, "Saved files and folders. Groups can be expanded or collapsed.")

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        use_button = ttk.Button(actions, text="Use", command=self.apply_selected_bookmark)
        copy_button = ttk.Button(actions, text="Copy", command=self.copy_selected_bookmark)
        add_button = ttk.Button(actions, text="Add", command=self.add_bookmark)
        remove_button = ttk.Button(actions, text="Remove", command=self.remove_selected_bookmark)
        use_button.pack(side="left")
        copy_button.pack(side="left", padx=(6, 0))
        add_button.pack(side="left", padx=(6, 0))
        remove_button.pack(side="left", padx=(6, 0))
        add_tooltip(use_button, "Fill the first compatible parameter field with the selected bookmark.")
        add_tooltip(copy_button, "Copy the selected bookmark path to the clipboard.")
        add_tooltip(add_button, "Save a common file or folder path as a bookmark.")
        add_tooltip(remove_button, "Remove the selected bookmark.")
        self.refresh_bookmarks()

    def create_templates_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        filters = ttk.Labelframe(parent, text="Filter")
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Group").grid(row=0, column=0, sticky="w", padx=8, pady=(6, 2))
        template_group_entry = ttk.Entry(filters, textvariable=self.template_group_var)
        template_group_entry.grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=(6, 2)
        )
        ttk.Label(filters, text="Tag").grid(row=1, column=0, sticky="w", padx=8, pady=(2, 8))
        template_tag_entry = ttk.Entry(filters, textvariable=self.template_tag_var)
        template_tag_entry.grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=(2, 8)
        )
        add_tooltip(template_group_entry, "Show templates whose group contains this text.")
        add_tooltip(template_tag_entry, "Show templates whose tags contain this text.")
        self.template_group_var.trace_add("write", lambda *_args: self.refresh_templates())
        self.template_tag_var.trace_add("write", lambda *_args: self.refresh_templates())

        template_hint = ttk.Label(parent, text="Double-click a template to load it into the editor.")
        template_hint.grid(
            row=1, column=0, sticky="w", pady=(8, 4)
        )
        add_tooltip(template_hint, "Only templates for the selected shell are shown. Change shell in Settings.")
        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.templates_tree = ttk.Treeview(
            tree_frame,
            columns=("tags",),
            show="tree headings",
            selectmode="browse",
            height=12,
        )
        templates_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.templates_tree.yview)
        self.templates_tree.configure(yscrollcommand=templates_scrollbar.set)
        self.templates_tree.heading("#0", text="Group / Name")
        self.templates_tree.heading("tags", text="Tags")
        self.templates_tree.column("#0", width=160, stretch=True)
        self.templates_tree.column("tags", width=160, stretch=True)
        self.templates_tree.grid(row=0, column=0, sticky="nsew")
        templates_scrollbar.grid(row=0, column=1, sticky="ns")
        self.templates_tree.bind("<Double-1>", self.handle_template_double_click)
        add_tooltip(self.templates_tree, "Built-in and saved templates for the selected shell. Groups can be expanded or collapsed.")

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        load_button = ttk.Button(actions, text="Load", command=self.load_selected_template)
        save_button = ttk.Button(actions, text="Save current", command=self.save_current_template)
        remove_button = ttk.Button(actions, text="Hide/Remove", command=self.remove_selected_template)
        restore_button = ttk.Button(actions, text="Restore built-ins", command=self.restore_builtin_templates)
        load_button.pack(side="left")
        save_button.pack(side="left", padx=(6, 0))
        remove_button.pack(side="left", padx=(6, 0))
        restore_button.pack(side="left", padx=(6, 0))
        add_tooltip(load_button, "Load the selected template into the Paste Script tab.")
        add_tooltip(save_button, "Save the current script as a reusable template for the selected shell.")
        add_tooltip(remove_button, "Hide a built-in template or remove one of your saved templates.")
        add_tooltip(restore_button, "Show all hidden built-in templates again.")
        self.refresh_templates()

    def create_settings_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        shell_box = ttk.Labelframe(parent, text="Shell")
        shell_box.grid(row=0, column=0, sticky="ew")
        shell_box.columnconfigure(0, weight=1)

        description = ttk.Label(
            shell_box,
            text="PowerShell is the default. Templates are filtered to the selected shell.",
            wraplength=320,
        )
        description.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 6))

        powershell_radio = ttk.Radiobutton(
            shell_box,
            text="PowerShell",
            variable=self.shell_var,
            value="powershell",
            command=self.shell_changed,
        )
        powershell_radio.grid(row=1, column=0, sticky="w", padx=8, pady=4)
        cmd_radio = ttk.Radiobutton(
            shell_box,
            text="CMD",
            variable=self.shell_var,
            value="cmd",
            command=self.shell_changed,
        )
        cmd_radio.grid(row=2, column=0, sticky="w", padx=8, pady=(4, 8))
        add_tooltip(powershell_radio, "Use PowerShell syntax for path quoting, templates, and script execution.")
        add_tooltip(cmd_radio, "Use CMD/batch syntax for path quoting, templates, and script execution.")
        add_tooltip(description, "Changing the shell refreshes detected parameters and hides templates for the other shell.")

        theme_box = ttk.Labelframe(parent, text="Appearance")
        theme_box.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        theme_box.columnconfigure(0, weight=1)
        theme_description = ttk.Label(
            theme_box,
            text="Choose a light skin or a dark gray skin for the app chrome.",
            wraplength=320,
        )
        theme_description.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))
        light_radio = ttk.Radiobutton(
            theme_box,
            text="Light mode",
            variable=self.theme_var,
            value="light",
            command=self.theme_changed,
        )
        light_radio.grid(row=1, column=0, sticky="w", padx=8, pady=4)
        dark_radio = ttk.Radiobutton(
            theme_box,
            text="Dark mode",
            variable=self.theme_var,
            value="dark",
            command=self.theme_changed,
        )
        dark_radio.grid(row=2, column=0, sticky="w", padx=8, pady=(4, 8))
        add_tooltip(light_radio, "Use the original light app skin.")
        add_tooltip(dark_radio, "Use the darker gray app skin.")
        add_tooltip(theme_description, "The selected skin is saved and restored next time the app opens.")

        output_box = ttk.Labelframe(parent, text="Output")
        output_box.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        output_box.columnconfigure(0, weight=1)
        save_logs_checkbox = ttk.Checkbutton(
            output_box,
            text="Save output logs",
            variable=self.save_output_logs_var,
            command=self.save_output_logs_changed,
        )
        save_logs_checkbox.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        log_location = ttk.Label(output_box, text=f"Logs folder: {logs_path()}", wraplength=320)
        log_location.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        add_tooltip(save_logs_checkbox, "Save every completed run output to a timestamped text file.")
        add_tooltip(log_location, "Run logs are stored next to the app source in the local logs folder.")

    def shell_changed(self) -> None:
        self.detect_slots()
        self.refresh_templates()
        self.update_shell_badge()

    def theme_changed(self) -> None:
        self.store.set_theme(self.theme)
        self.configure_app_theme()
        self.apply_theme_to_widgets()
        self.status_var.set(f"Switched to {self.theme} mode.")

    def save_output_logs_changed(self) -> None:
        enabled = bool(self.save_output_logs_var.get())
        self.store.set_save_output_logs(enabled)
        state = "enabled" if enabled else "disabled"
        self.status_var.set(f"Output log saving {state}.")

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
                if not slot.is_placeholder:
                    slot.picker_kind = old.picker_kind
                    slot.label = old.label
                    slot.param_name = old.param_name
                    slot.default_value = old.default_value
                slot.include_subfolders = old.include_subfolders if slot.picker_kind == "folder" else False
        self.render_slots()
        self.update_final_script()
        self.status_var.set(f"Detected {len(self.slots)} path picker(s).")

    def render_slots(self) -> None:
        for child in self.slot_inner.winfo_children():
            child.destroy()
        self.slot_rows.clear()

        if not self.slots:
            ttk.Label(self.slot_inner, text="No placeholders or path-like arguments found.").pack(
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
            parts.append(replacement_for_slot(slot, self.shell))
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

    def active_script_text_widget(self) -> tk.Text:
        selected_tab = self.script_tabs.index(self.script_tabs.select())
        return self.output_text if selected_tab == 1 else self.input_text

    def select_all_active_script(self) -> None:
        widget = self.active_script_text_widget()
        widget.focus_set()
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "1.0")
        self.status_var.set("Selected script text.")

    def copy_active_script_text(self) -> None:
        widget = self.active_script_text_widget()
        try:
            text = widget.get("sel.first", "sel.last")
        except tk.TclError:
            text = widget.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Copied script text.")

    def copy_final_script(self) -> None:
        script = self.build_final_script()
        self.current_script = script
        self.clipboard_clear()
        self.clipboard_append(script)
        self.status_var.set("Final script copied to clipboard.")

    def validate_slots_before_run(self) -> bool:
        problems: list[str] = []
        for slot in self.slots:
            if slot.picker_kind == "text":
                continue
            value = slot.value.strip()
            if not value:
                continue
            path = Path(value)
            if slot.picker_kind == "file":
                if path.exists() and path.is_dir():
                    problems.append(f"{slot.label} expects a file, but this is a folder:\n{value}")
                elif is_output_file_slot(slot):
                    parent = path.parent
                    if str(parent) not in {"", "."} and not parent.exists():
                        problems.append(f"{slot.label} parent folder does not exist:\n{parent}")
            elif slot.picker_kind == "folder" and path.exists() and path.is_file():
                problems.append(f"{slot.label} expects a folder, but this is a file:\n{value}")

        if not problems:
            return True

        messagebox.showerror("Check parameters", "\n\n".join(problems[:5]))
        self.status_var.set("Run canceled: check the highlighted parameter types.")
        return False

    def run_script(self) -> None:
        if self.running_process and self.running_process.poll() is None:
            messagebox.showinfo("Already running", "Stop the current script before starting another one.")
            return

        if not self.validate_slots_before_run():
            return

        script = self.build_final_script()
        if not script.strip():
            messagebox.showinfo("Nothing to run", "Paste a script first.")
            return

        command = self.command_for_script(script)

        self.run_output.delete("1.0", "end")
        self.run_output.insert("1.0", f"Executing with {self.shell_display_name()}:\n")
        self.run_output.insert("end", "-" * 72 + "\n")
        self.run_output.insert("end", script.rstrip() + "\n")
        self.run_output.insert("end", "-" * 72 + "\n")
        self.run_output.insert("end", "Running...\n")
        self.status_var.set("Running script...")
        self.run_button.configure(state="disabled", bg=THEMES[self.theme]["run_hover"])
        self.stop_button.configure(state="normal")
        self.start_progress()
        self.update_idletasks()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            self.running_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=self.execution_cwd(),
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            messagebox.showerror("Shell not found", str(exc))
            self.cleanup_script_file()
            self.finish_process_ui("Run failed.")
            return
        except Exception as exc:  # pragma: no cover - GUI safety net
            messagebox.showerror("Run failed", str(exc))
            self.cleanup_script_file()
            self.finish_process_ui("Run failed.")
            return

        assert self.running_process.stdout is not None
        assert self.running_process.stderr is not None
        self.process_streams_open = 2
        threading.Thread(target=self.read_process_stream, args=(self.running_process.stdout,), daemon=True).start()
        threading.Thread(target=self.read_process_stream, args=(self.running_process.stderr,), daemon=True).start()
        self.after(100, self.poll_running_process)

    def read_process_stream(self, stream) -> None:
        buffer = ""
        try:
            while True:
                char = stream.read(1)
                if char == "":
                    break
                buffer += char
                if char in {"\n", "\r"} or len(buffer) >= 4096:
                    self.output_queue.put(buffer)
                    buffer = ""
            if buffer:
                self.output_queue.put(buffer)
        finally:
            stream.close()
            self.output_queue.put(None)

    def shell_display_name(self) -> str:
        return "PowerShell" if self.shell == "powershell" else "CMD"

    def poll_running_process(self) -> None:
        while True:
            try:
                text = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if text is None:
                self.process_streams_open = max(0, self.process_streams_open - 1)
                continue
            self.run_output.insert("end", text)
            self.run_output.see("end")
            self.update_progress_from_output(text)

        if not self.running_process:
            return

        return_code = self.running_process.poll()
        if return_code is None:
            self.after(100, self.poll_running_process)
            return

        if self.process_streams_open > 0:
            self.after(50, self.poll_running_process)
            return

        self.run_output.insert("end", f"\nExit code: {return_code}")
        note = self.exit_code_note(return_code)
        if note:
            self.run_output.insert("end", f"\n{note}")
        self.run_output.see("end")
        log_file = self.save_run_log(return_code)
        self.running_process = None
        self.cleanup_script_file()
        status = f"Finished with exit code {return_code}."
        if log_file:
            status += f" Log saved: {log_file.name}"
        self.finish_process_ui(status)

    def save_run_log(self, return_code: int) -> Path | None:
        if not self.save_output_logs_var.get():
            return None
        try:
            folder = logs_path()
            folder.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{timestamp}-{self.shell}-exit{return_code}.txt"
            path = folder / filename
            path.write_text(self.run_output.get("1.0", "end-1c"), encoding="utf-8")
            return path
        except OSError as exc:
            self.run_output.insert("end", f"\nCould not save output log: {exc}")
            self.run_output.see("end")
            return None

    def exit_code_note(self, return_code: int) -> str:
        script = self.current_script.strip().lower()
        if not re.search(r"(^|\r?\n)\s*robocopy\b", script):
            return ""
        meanings = {
            0: "Robocopy: no files were copied and no failures were reported.",
            1: "Robocopy: files were copied successfully.",
            2: "Robocopy: extra files or directories were detected; no failures were reported.",
            3: "Robocopy: files were copied and extra files/directories were detected.",
        }
        if return_code in meanings:
            return meanings[return_code]
        if return_code < 8:
            return "Robocopy: completed with non-fatal differences. Review the output above."
        return "Robocopy: failure reported. Exit codes 8 and above indicate at least one failure."

    def command_for_script(self, script: str) -> list[str]:
        self.cleanup_script_file()
        if self.shell == "powershell":
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]

        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".cmd",
            prefix="command-line-gui-",
            delete=False,
        )
        with handle:
            handle.write("@echo off\n")
            handle.write(script.replace("\n", "\r\n"))
            if not script.endswith(("\n", "\r\n")):
                handle.write("\r\n")
        self.current_script_file = handle.name
        return ["cmd.exe", "/d", "/c", handle.name]

    def cleanup_script_file(self) -> None:
        if not self.current_script_file:
            return
        try:
            os.remove(self.current_script_file)
        except OSError:
            pass
        self.current_script_file = None

    def execution_cwd(self) -> str | None:
        cwd = os.getcwd()
        if os.name == "nt" and cwd.startswith("\\\\"):
            return os.environ.get("USERPROFILE") or os.environ.get("SystemDrive", "C:\\")
        return cwd

    def stop_script(self) -> None:
        if not self.running_process or self.running_process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.running_process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            self.running_process.terminate()
        self.status_var.set("Stopping script...")

    def finish_process_ui(self, status: str) -> None:
        self.stop_progress()
        self.run_button.configure(state="normal", bg=THEMES[self.theme]["run"])
        self.stop_button.configure(state="disabled")
        self.status_var.set(status)

    def start_progress(self) -> None:
        self.progress_mode = "indeterminate"
        self.progress_var.set(0)
        self.progress_bar.configure(mode="indeterminate", maximum=100)
        self.progress_bar.start(12)
        self.progress_label_var.set("Running")

    def update_progress_from_output(self, text: str) -> None:
        matches = re.findall(r"(?<!\d)(100|[1-9]?\d)(?:\.\d+)?\s*%", text)
        if not matches:
            return
        percent = max(0, min(100, int(matches[-1])))
        if self.progress_mode != "determinate":
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=100)
            self.progress_mode = "determinate"
        self.progress_var.set(percent)
        self.progress_label_var.set(f"{percent}%")

    def stop_progress(self) -> None:
        if self.progress_mode == "indeterminate":
            self.progress_bar.stop()
            self.progress_var.set(100)
        elif self.progress_mode == "determinate" and self.progress_var.get() < 100:
            self.progress_label_var.set(f"{int(self.progress_var.get())}%")
        else:
            self.progress_var.set(100)
        if self.progress_mode != "idle":
            if self.running_process and self.running_process.poll() is None:
                self.progress_label_var.set("Stopping")
            elif self.progress_mode == "indeterminate":
                self.progress_label_var.set("Done")
        self.progress_mode = "idle"


def main() -> int:
    app = CommandLineGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
