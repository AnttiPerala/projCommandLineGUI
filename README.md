# Command Line GUI

A generic Windows GUI for people who like `cmd.exe` and PowerShell scripts but prefer choosing files and folders with normal file picker dialogs.

## What it does

- Paste a PowerShell or CMD script.
- Detect likely file and folder path positions:
  - placeholders such as `<file>`, `<folder>`, `{path}`, `${outputFile}`
  - existing Windows paths such as `C:\Temp\file.txt` or `\\server\share`
  - common path-ish options such as `-Path`, `-LiteralPath`, `-Destination`, `/D`
- Turn detections into picker rows.
- Pick individual files or whole folders.
- Mark folder picks as including subfolders.
- Save common files/folders as bookmarks.
- Save common scripts as templates.
- Organize bookmarks and templates with groups and tags.
- Copy the final script or run it directly in PowerShell/CMD.

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

Templates work best with placeholders such as `<folder>`, `<file>`, `<source_folder>`, and `<target_folder>` so the app can turn those spots into pickers.

## Data location

Bookmarks and templates are stored as JSON here:

```text
%APPDATA%\CommandLineGUI\library.json
```

## Run from source

```powershell
python app.py
```

No third-party Python packages are required to run the app.

## Build a standalone exe

Install PyInstaller once:

```powershell
python -m pip install pyinstaller
```

Then build:

```powershell
.\build_exe.ps1
```

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
