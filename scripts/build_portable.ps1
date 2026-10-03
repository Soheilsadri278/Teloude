# Build a genuine PORTABLE Teloude. No Python, no pip, no source, no Program Files installation.
#
#   powershell -File scripts\build_portable.ps1 [-Zip]
#
# Result:
#   dist\Teloude-Portable\
#       Teloude.exe                 <- launch this; nothing to install
#       portable.ini                <- switches the frozen app to <exe dir>\data
#       README.txt
#       _internal\                  <- PyInstaller runtime (Qt, Telethon, ...)
#       assets\                     <- icon.ico / icon.png (window + tray + toast icon)
#       data\                       <- EMPTY on purpose: db, logs, cache, protected secrets
#
# The installed version (scripts\build_windows.ps1 -> installer\teloude.iss) is untouched.
#
# PORTABILITY, HONESTLY
#   * portable: the application, its runtime and its assets. Put the folder on a USB stick and run
#     Teloude.exe on any Windows PC.
#   * NOT portable: the signed-in Telegram session and the API credentials. They stay encrypted with
#     Windows DPAPI, which is bound to the Windows user/machine by design. Copying the folder to
#     another PC therefore starts Teloude signed out - that is the intended security behaviour, not
#     a bug, and we never downgrade it to plaintext to make the folder "more portable".
#   * data\ ships empty on purpose: never copy an existing %LOCALAPPDATA%\Teloude into a release.
param([switch]$Zip)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$Version = "1.0.0"
$AppDir   = "dist\Teloude"                 # produced by PyInstaller (installer\teloude.spec)
$OutRoot  = "dist\Teloude-Portable"
$OutApp   = Join-Path $OutRoot "Teloude"

if (-not (Test-Path (Join-Path $AppDir "Teloude.exe"))) {
    Write-Host "PyInstaller output not found - building it first..."
    if (-not (Test-Path ".\.venv\Scripts\pyinstaller.exe")) {
        python -m venv .venv
        .\.venv\Scripts\python -m pip install --upgrade pip
        .\.venv\Scripts\python -m pip install -e ".[dev,build]"
    }
    .\.venv\Scripts\python -m ruff check .
    .\.venv\Scripts\python -m pytest -q
    .\.venv\Scripts\python scripts\make_icon.py
    .\.venv\Scripts\pyinstaller installer\teloude.spec --noconfirm
}

Remove-Item -Recurse -Force $OutRoot -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $OutApp | Out-Null
Copy-Item -Path (Join-Path $AppDir "*") -Destination $OutApp -Recurse -Force

# --- the login screen only asks for API credentials if this build has none -------------------
if (Test-Path ".\app\teloude_api.json") {
    Write-Host "Bundling app\teloude_api.json: users sign in with phone -> code -> 2FA only."
} else {
    Write-Host "NOTE: no app\teloude_api.json - this build will ask each user for my.telegram.org"
    Write-Host "      API credentials once. To bundle them instead:"
    Write-Host "      python scripts\inject_credentials.py --api-id <id> --api-hash <hash>"
}

# --- the two things that make it portable ---------------------------------------------------
New-Item -ItemType Directory -Force -Path (Join-Path $OutApp "data") | Out-Null
@"
[Teloude]
; This file makes the packaged app keep its database, logs, cache and protected secrets in the
; "data" folder next to Teloude.exe instead of %%LOCALAPPDATA%%\Teloude.
; Delete this file to have the portable build use the normal per-user data folder instead.
mode=portable
"@ | Set-Content -Encoding UTF8 (Join-Path $OutApp "portable.ini")

@"
Teloude $Version - portable

Run Teloude.exe. Nothing is installed and nothing is written outside this folder.
The first launch creates:

    data\teloude.db     the local backup index (folders, files, Telegram message ids)
    data\logs\          redacted logs (secrets are never written)
    data\secrets\       DPAPI-protected Telegram session / API hash / proxy secret
    data\cache\         preview thumbnails

Moving this folder
------------------
The application is portable: copy the whole folder to another Windows PC and run Teloude.exe.
The signed-in Telegram session is NOT portable and is not meant to be: it is encrypted with
Windows DPAPI, which is bound to the Windows user account. On a different PC (or a different
Windows user) Teloude simply asks you to sign in again; your storages are then rediscovered from
Telegram with "Find on Telegram". Your backed-up files are never affected by this.

Uninstalling
------------
Delete the folder. Nothing was installed and no registry entries were created.
"@ | Set-Content -Encoding UTF8 (Join-Path $OutRoot "README.txt")

# --- sanity: the portable tree must not ship any user data or plaintext secrets ---------------
$bad = Get-ChildItem -Path $OutApp -Recurse -File -ErrorAction SilentlyContinue |
       Where-Object { $_.Name -match '\.dpapi$|\.insecure$|teloude\.db|telegram_session' }
if ($bad) { throw "the portable build would ship user data: $($bad -join ', ')" }
foreach ($need in @("Teloude.exe", "portable.ini", "_internal")) {
    if (-not (Test-Path (Join-Path $OutApp $need))) { throw "portable build is missing $need" }
}
# PyInstaller >= 6 keeps bundled data under _internal; older versions put it next to the EXE.
$iconOk = (Test-Path (Join-Path $OutApp "assets\icon.ico")) -or
          (Test-Path (Join-Path $OutApp "_internal\assets\icon.ico"))
if (-not $iconOk) { throw "portable build is missing assets\icon.ico" }

if ($Zip) {
    # NB: PowerShell variable names are case-insensitive, so this must NOT be called $zip -
    # that would overwrite the [switch]$Zip parameter and throw ConvertToFinalInvalidCastException.
    $zipPath = "dist\Teloude-Portable-$Version.zip"
    Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $OutRoot "*") -DestinationPath $zipPath
    Write-Host "Zip: $zipPath"
}

$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $py scripts\portable_verify.py --root $OutApp
Write-Host "Portable build: $OutApp"
