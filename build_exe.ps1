$ErrorActionPreference = "Stop"

python -m PyInstaller `
  --noconfirm `
  --windowed `
  --onefile `
  --name CommandLineGUI `
  app.py

Write-Host "Built dist\CommandLineGUI.exe"
