# Command Line GUI

A generic Windows GUI for people who like `cmd.exe` and PowerShell scripts but prefer choosing files and folders with normal file picker dialogs.

## What it does

- Paste a PowerShell or CMD script.
- Detect likely file and folder path positions:
  - placeholders such as `<file>`, `<folder>`, `{path}`, `${outputFile}`
  - existing Windows paths such as `C:\Temp\file.txt` or `\\server\share`
  - common path-ish options such as `-Path`, `-LiteralPath`, `-Destination`, `/D`
- Turn detections into picker rows.
- Turn intentional placeholders into a small parameter form.
- Pick individual files or whole folders.
- Mark folder picks as including subfolders.
- Save common files/folders as bookmarks.
- Save common scripts as templates.
- Organize bookmarks and templates with groups and tags.
- Filter templates to the selected shell so PowerShell and CMD workflows do not mix.
- Copy the final script or run it directly in PowerShell/CMD.
- Stream command output into the app while long-running scripts continue.
- Stop a running script from the toolbar.

The subfolder checkbox is exposed to the generated script as an environment variable:

- PowerShell: `$env:CLGUI_PICKER_1_INCLUDE_SUBFOLDERS`
- CMD: `%CLGUI_PICKER_1_INCLUDE_SUBFOLDERS%`

That keeps the GUI generic while still letting scripts opt into recursion behavior.

## Bookmarks

Bookmarks are saved file or folder locations. Use the Bookmarks tab to:

- Add a bookmark from a picked path or by browsing.
- Remove old bookmarks.
- Browse bookmarks in collapsible groups.
- Filter bookmarks by group or tag.
- Apply a bookmark to the first matching empty path picker.
- Drag a bookmark onto a detected path picker field.
- Copy a bookmarked path to the clipboard.

Each bookmark stores:

- name
- path
- file/folder type
- optional subfolder preference
- group
- tags

## Templates

Templates are reusable scripts. Use the Templates tab to:

- Save the current script and shell mode.
- Load a saved script back into the editor.
- Remove old templates.
- Browse templates in collapsible groups.
- Filter templates by group or tag.
- Use built-in templates shipped with the app.
- Hide built-in templates you do not want to see.
- Restore hidden built-in templates.

Templates work best with placeholders such as `<folder>`, `<file>`, `<source_folder>`, and `<target_folder>` so the app can turn those spots into pickers.

Built-in templates are included inside the executable. They cover common workflows in these groups:

- Archive
- Environment
- FFmpeg
- Files
- Network
- Rename
- Robocopy
- Search
- System
- Web

Removing a built-in template hides it from the template list instead of deleting it from the executable. Use `Restore built-ins` to show hidden built-ins again.

The app defaults to PowerShell. Change the active shell in the Settings tab; the Templates tab shows only templates that match that shell. Settings also includes Light mode and Dark mode skins.

## Parameter placeholders

Use placeholders to make a script feel like a small form:

```powershell
Get-ChildItem -Path <source_folder:folder:Source> -Filter <filter:text:default=*.txt> |
  Copy-Item -Destination <target_folder:folder:Target>
```

Supported placeholder types:

- `folder`: folder picker
- `file`: file picker
- `text`: plain text input

Basic syntax:

```text
<name:type:Label>
<name:type:Label:default=value>
```

For defaults that contain drive letters or other colons, use `|` separators:

```text
<output_file|file|Output log|default=C:\Temp\run.log>
```

File and folder values are quoted for the selected shell. Text values are inserted as-is, which is useful for filters, switches, names, and other non-path arguments.

Explicit placeholder types are enforced at run time. A `file` field cannot point at an existing folder, and a `folder` field cannot point at an existing file. Output-style file placeholders, such as `Output MP4`, open a Save As dialog when browsed.

## Data location

Bookmarks and templates are stored as JSON here:

```text
%APPDATA%\CommandLineGUI\library.json
```

When `Save output logs` is enabled in Settings, completed run output is written to timestamped `.txt` files in the local `logs` folder. This is enabled by default.

## Run from source

```powershell
python app.py
```

No third-party Python packages are required to run the app.

When a script runs, the shell process is hidden and stdout/stderr are streamed into the Run output panel. This keeps server commands such as `python -m http.server` visible in the app instead of opening an empty PowerShell window.

Paste Script and Final Script are separate tabs using a terminal-style dark editor. The Run output panel prints the exact fully substituted script before execution. The Final script and Run output panels preserve full command lines with horizontal scrolling.

CMD scripts are executed through a temporary `.cmd` file so multiline scripts run exactly like a normal batch file.

The Run output panel includes a progress bar. It shows activity for every running command, and switches to a percentage when command output includes values such as `42%`. Commands that do not report progress still show an indeterminate running state.

Many built-in templates emit progress percentages themselves, including Robocopy, rename workflows, file list/export/search workflows, ZIP create/extract workflows, and FFmpeg workflows when `ffprobe` is available. FFmpeg templates stop with a nonzero exit code if `ffprobe` cannot read the input or FFmpeg fails.

The FFmpeg group includes a `Batch convert folder to MP4` template. It converts matching video files from a source folder into an output folder, emits per-file progress, and preserves subfolder layout when the source folder picker has `Subfolders` checked.

Robocopy uses unusual exit codes. For example, exit code `0` means no files needed copying and no failures were reported, while exit code `1` means files were copied successfully. The app adds a short Robocopy note after those runs.

## Build a standalone exe

Install PyInstaller once:

```powershell
python -m pip install pyinstaller
```

Then build:

```powershell
.\build_exe.ps1
```

The build uses the custom app icon in `assets\CommandLineGUI.ico`.

The executable will be created in `dist\CommandLineGUI.exe`.

## Notes

Path detection is intentionally heuristic. The most reliable way to make a picker is to put a placeholder where a path belongs, for example:

```powershell
Get-ChildItem -Path <folder> -Recurse | Copy-Item -Destination <folder>
```

or:

```cmd
robocopy <source_folder> <target_folder> /E
```
