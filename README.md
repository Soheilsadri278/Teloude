# Teloude

Windows-first personal backup that uses **your own Telegram account** (MTProto via Telethon, not the Bot API) as private storage.
Each storage is a private forum supergroup; folders map to Forum Topics; the real hierarchy lives in local SQLite and in
self-describing message captions, so another PC can rediscover and restore everything. Backup, not sync: **local files are never deleted automatically.**

## Run from source
```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
export TELOUDE_API_ID=... TELOUDE_API_HASH=...       # optional; otherwise the login screen asks (my.telegram.org/apps)
python -m app
```
API credentials are never in the source. They come from the environment, from a bundled build file, or from the login screen,
and are stored with Windows DPAPI.

## Test / lint
```bash
python -m pytest -q      # 186 pass, 1 Windows-only skip on Linux
python -m ruff check .
python scripts/benchmarks.py   # fake-transport benchmarks -> docs/BENCHMARKS.md
```

## Build (on Windows)
| Command | Result |
| --- | --- |
| `powershell -File scripts\build_windows.ps1` | installed app: PyInstaller EXE + Inno Setup installer, verified with `scripts/windows_verify.py` |
| `powershell -File scripts\build_portable.ps1 [-Zip]` | portable folder: `dist\Teloude-Portable\Teloude\` — run `Teloude.exe`, nothing is installed, verified with `scripts/portable_verify.py` |

Both scripts run lint + tests first. `python scripts/packaging_check.py` (26 checks) verifies the Windows icon/AppID/credential
wiring without needing Windows.

### API credentials: three sources, in order
1. `TELOUDE_API_ID` / `TELOUDE_API_HASH` environment variables (developer override, tests).
2. A build-time file `app/teloude_api.json` — optional, **git-ignored**, written with
   `python scripts/inject_credentials.py --api-id … --api-hash …` (and `--clear` / `--show`). A release that ships it lets the user go
   straight to **phone → code → 2FA**; the credentials step is skipped completely.
3. What the user typed once in the login screen, stored DPAPI-protected.

The file is read by `app/infrastructure/build_config.py`, never logged (`app/domain/redact.py` masks it) and never committed.

### Portability, honestly
* **Portable:** the application, its runtime and its assets. Copy the folder to a USB stick and run `Teloude.exe` on any Windows PC.
* **Not portable, on purpose:** the signed-in Telegram session and the API credentials. They are encrypted with Windows DPAPI,
  which is bound to the Windows user/machine by design, so a copied folder starts Teloude signed out. Nothing is downgraded to
  plaintext to make the folder "more portable". Storages are rediscovered from Telegram with **Find on Telegram**.
* `data/` ships empty: a release never carries someone else's backup index or secrets (`portable_verify.py` enforces this).

See `docs/` for REQUIREMENTS, ARCHITECTURE, IMPLEMENTATION_PLAN, VERIFICATION (what is and is not verified) and BENCHMARKS.
