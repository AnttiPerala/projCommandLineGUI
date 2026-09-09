$ErrorActionPreference = "Stop"

python -m PyInstaller `
  --noconfirm `
  --windowed `
  --onefile `
  --icon assets\CommandLineGUI.ico `
  --add-data "assets\CommandLineGUI.ico;assets" `
  --add-data "assets\CommandLineGUI.png;assets" `
  --name CommandLineGUI `
  app.py

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE."
}

Write-Host "Built dist\CommandLineGUI.exe"
