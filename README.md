# Teloude

**Back up your own files to your own Telegram account.**

Teloude is a Windows-first personal backup app that uses *your* Telegram account as private storage. Each storage is a private Telegram forum supergroup, where folders become forum topics and the real folder hierarchy is preserved locally and inside self-describing message captions.

**Backup, not sync: Teloude never deletes your local files.**

---

## Features

### Storage

* Your Telegram account is the storage. No separate server or subscription.
* Each storage is a private forum supergroup.
* Folders are represented as Telegram forum topics.
* Large files are split into volumes using Telegram's reported upload limits.

### Backup & Restore

* Scan → compare → decide: **Skip / Upload again / Cancel**, with apply-to-all.
* Pipelined and resumable uploads.
* Pause, cancel, retry, reconnect and resume transfers.
* Interrupted backups remain resumable after crashes or shutdowns.
* Restore with explicit **Skip / Keep both / Replace / Cancel** decisions.
* Path-traversal protection during restore.
* **Find on Telegram** can rediscover storages and rebuild the local index on another PC.

### Control

* Global upload speed limit.
* Unlimited, 10, 5, 2 MB/s and custom speed settings.
* Global MTProto proxy with persistent status.
* Global search and previews across storages.

### Desktop & Safety

* Windows system tray support.
* Notifications.
* Light and dark themes.
* Single-instance protection.
* Closing the window moves Teloude to the system tray.
* Telegram sessions and API credentials are protected with Windows DPAPI.
* Structured logs with sensitive information redacted.

## Safety Rules

These are core rules of the project:

| Rule                         | Meaning                                                                      |
| ---------------------------- | ---------------------------------------------------------------------------- |
| **Backup, not sync**         | Local files are never deleted automatically.                                 |
| **No silent overwrite**      | Every overwrite requires an explicit decision.                               |
| **No silent cloud deletion** | Removing anything from Telegram requires confirmation.                       |
| **No secrets in logs**       | API credentials, sessions and proxy information are redacted.                |
| **No fake success**          | Cancelled or incomplete operations are never reported as successful backups. |

## Repository Layout

```text
teloude/
├── app/
│   ├── domain/
│   ├── application/
│   ├── infrastructure/
│   └── presentation/
├── tests/
├── scripts/
├── installer/
└── docs/

Logo&icon/
```

## Quick Start

```bash
cd teloude
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m app
```

On first launch, Teloude asks for:

1. Phone number
2. Telegram login code
3. 2FA password, if enabled

Application data is stored under:

```text
%LOCALAPPDATA%\Teloude
```

Useful command-line options:

| Flag                | Effect                                             |
| ------------------- | -------------------------------------------------- |
| `--version`         | Print the current Teloude version                  |
| `--data-dir PATH`   | Use a custom data directory                        |
| `--screenshot FILE` | Render the application window to PNG and exit      |
| `--open-proxy`      | Open the proxy settings before taking a screenshot |

## Telegram API Credentials

Teloude supports three credential sources:

1. `TELOUDE_API_ID` / `TELOUDE_API_HASH` environment variables.
2. An optional build-time `app/teloude_api.json` file.
3. Credentials entered through the login screen.

Credentials are never committed to the repository and are not written to logs.

## Portability

The application itself is portable.

The packaged application, runtime and assets can be copied to another Windows PC and run without installation when using the portable build.

The Telegram session is intentionally **not** portable. Telegram session data and credentials are protected using Windows DPAPI, which binds them to the Windows user and machine.

Copying the application folder therefore starts Teloude signed out on another computer.

## Build for Windows

Build the normal Windows application:

```powershell
powershell -File scripts\build_windows.ps1
```

Build the portable version:

```powershell
powershell -File scripts\build_portable.ps1 -Zip
```

The Windows build requires Python 3.11+.

The installer additionally requires Inno Setup 6.

## Tests

Run the test suite:

```powershell
python -m pytest -q
```

Run linting:

```powershell
python -m ruff check .
```

Run benchmarks:

```powershell
python scripts/benchmarks.py
```

The test suite is designed to run without a real Telegram account. Telegram communication is replaced with a fake gateway during tests.

## Icons & Branding

`Logo&icon/` is the source of truth for Teloude's branding.

The icon generation script creates the application assets:

```powershell
python scripts/make_icon.py
```

To verify that the generated assets are up to date:

```powershell
python scripts/icon_report.py
```

After changing the artwork, the application must be rebuilt for the new icons to appear in the executable and installer.

## Known Limitations

* Files are uploaded one at a time, while parts inside each file are pipelined.
* Very small files may be uploaded again after a restart instead of being resumed.
* Telegram's upload-session lifetime is not guaranteed, so Teloude may fall back to a fresh upload when necessary.
* Benchmarks use a fake transport and therefore do not represent actual Telegram network throughput.
* Teloude is Windows-first. Linux and macOS can run the source code and test suite, but packaged builds target Windows.

## Documentation

| Document                      | Description                  |
| ----------------------------- | ---------------------------- |
| `docs/REQUIREMENTS.md`        | Project requirements         |
| `docs/ARCHITECTURE.md`        | Architecture and data model  |
| `docs/IMPLEMENTATION_PLAN.md` | Implementation plan          |
| `docs/VERIFICATION.md`        | Verification and test status |
| `docs/BENCHMARKS.md`          | Performance benchmarks       |

## Requirements

* Python 3.11+
* PySide6
* Telethon
* Pillow
* pypdf
* A Telegram account
* Telegram API credentials from [my.telegram.org](https://my.telegram.org/apps)

Windows is required for the packaged builds.

## License

MIT
