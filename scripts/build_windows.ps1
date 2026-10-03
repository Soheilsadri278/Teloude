# Build Teloude for Windows: venv -> tests -> icon -> PyInstaller -> Inno Setup -> verification.
# Requirements: Python 3.12+, Inno Setup 6 (iscc.exe on PATH or in the default install dir).
# Optional:    -Portable also produces dist\Teloude-Portable\ (see scripts\build_portable.ps1).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

param([switch]$Portable)

python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev,build]"

.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest -q          # includes the Windows-only DPAPI tests
.\.venv\Scripts\python scripts\make_icon.py  # assets\icon.ico from the user's Logo&icon artwork
.\.venv\Scripts\python scripts\packaging_check.py  # static icon / shortcut / portable checks

# --- the login screen only asks for API credentials if this build has none -------------------
if (Test-Path ".\app\teloude_api.json") {
    Write-Host "Bundling app\teloude_api.json: users sign in with phone -> code -> 2FA only."
} else {
    Write-Host "NOTE: no app\teloude_api.json - this build will ask each user for my.telegram.org"
    Write-Host "      API credentials once. To bundle them instead:"
    Write-Host "      python scripts\inject_credentials.py --api-id <id> --api-hash <hash>"
}

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
.\.venv\Scripts\pyinstaller installer\teloude.spec --noconfirm

$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $iscc installer\teloude.iss

.\.venv\Scripts\python scripts\windows_verify.py --exe dist\Teloude\Teloude.exe --installer dist\Teloude-Setup-1.0.0.exe
Write-Host "Done: dist\Teloude-Setup-1.0.0.exe"

if ($Portable) {
    # dist\Teloude already exists at this point, so build_portable.ps1 only repackages it.
    & (Join-Path $PSScriptRoot "build_portable.ps1") -Zip
    Write-Host "Done: dist\Teloude-Portable\Teloude\Teloude.exe"
}
