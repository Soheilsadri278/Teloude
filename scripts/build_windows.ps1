# Build Teloude for Windows: venv -> tests -> icon -> PyInstaller -> Inno Setup -> verification.
# Requirements: Python 3.11+, Inno Setup 6 (iscc.exe on PATH or in the default install dir).
# Optional:    -Portable also produces dist\Teloude-Portable\ (see scripts\build_portable.ps1).
param([switch]$Portable)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

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

# A session started before Inno Setup was installed does not have it on PATH yet, so re-read PATH.
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {  # not on PATH: look where Inno Setup actually installs, and ask the registry
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 5\ISCC.exe"
    )
    foreach ($key in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
                       "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
                       "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1")) {
        $dir = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallLocation
        if ($dir) { $candidates += (Join-Path $dir "ISCC.exe") }
    }
    foreach ($candidate in $candidates) {
        if (-not $iscc -and (Test-Path $candidate)) { $iscc = $candidate }
    }
}
if (-not $iscc) {
    throw "Inno Setup 6 not found (iscc.exe). Install it with:  winget install -e --id JRSoftware.InnoSetup" +
          "`nIf it is already installed, run iscc.exe /? once, or open a new PowerShell window and retry."
}
Write-Host "Inno Setup: $iscc"
& $iscc installer\teloude.iss

.\.venv\Scripts\python scripts\windows_verify.py --exe dist\Teloude\Teloude.exe --installer dist\Teloude-Setup-1.0.0.exe
Write-Host "Done: dist\Teloude-Setup-1.0.0.exe"

if ($Portable) {
    # dist\Teloude already exists at this point, so build_portable.ps1 only repackages it.
    & (Join-Path $PSScriptRoot "build_portable.ps1") -Zip
    Write-Host "Done: dist\Teloude-Portable\Teloude\Teloude.exe"
}
