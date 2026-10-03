# Teloude

**Back up your own files directly to your own Telegram account.**

Teloude is a Windows desktop backup application that uses your **personal Telegram account** as private, cloud-like storage for your files.

Teloude is designed for **backup, not sync**.
It never automatically deletes your local files.

[🇮🇷 فارسی](README.fa.md)

---

## ✨ Features

### 💾 Storage

* Uses your personal Telegram account
* No dedicated server required
* No monthly subscription
* No third-party cloud storage
* Uses Telegram MTProto through Telethon
* Uses a private Telegram forum supergroup as storage
* Maps local folders to Telegram forum topics
* Supports folders and subfolders
* Stores the local file structure in a SQLite database
* Stores self-describing metadata in message captions for reliable file identification and recovery

### 📦 Backup

* Scan files and folders
* Detect duplicate files
* Choose how to handle duplicates:

  * Skip
  * Upload again
  * Cancel
  * Apply to all
* Pipelined multipart uploads
* Resumable transfers
* Pause / Resume
* Cancel
* Retry
* Reconnect
* Resume operations after shutdown or crash
* Path traversal protection during restore
* Find files on Telegram and rebuild the local index

### 🔄 Restore

When restoring files that already exist locally, Teloude provides:

* Skip
* Keep both
* Replace
* Cancel

Restore operations validate destination paths to prevent files from escaping the selected destination directory.

### ⚡ Speed Control

Teloude includes an asynchronous Token Bucket speed limiter.

Available upload speed options include:

* Unlimited
* 10 MB/s
* 5 MB/s
* 2 MB/s
* Custom

The speed limiter operates globally across transfer operations without destroying pipeline concurrency.

### 🌐 Proxy

* MTProto Proxy support
* Global proxy configuration
* Proxy management from the application

### 🖥️ Desktop UI

* Windows desktop interface
* Built with PySide6
* System Tray support
* Notifications
* Theme support
* Single-instance protection
* Close-to-tray support
* Simple interface focused on backup operations

### 🔐 Security

* Local credential protection using Windows DPAPI
* No secrets in application logs
* No silent overwrites
* No automatic deletion of local files
* No silent deletion of files stored on Telegram
* No false success reporting

---

## 🛡️ Core Safety Rules

| Rule                     | Teloude behavior                                         |
| ------------------------ | -------------------------------------------------------- |
| Backup, not sync         | Local files are never automatically deleted              |
| No automatic deletion    | Teloude does not delete local files to free space        |
| No silent overwrite      | Replacing an existing file requires an explicit decision |
| No silent cloud deletion | Telegram files are not silently deleted                  |
| No secrets in logs       | Sensitive information is excluded from logs              |
| No fake success          | Failed operations are never reported as successful       |

---

## 🏗️ Project Structure

```text
Teloude/
├── teloude/
│   ├── app/
│   │   ├── domain/
│   │   ├── application/
│   │   ├── infrastructure/
│   │   └── presentation/
│   ├── tests/
│   ├── scripts/
│   ├── installer/
│   └── docs/
│
├── Logo&icon/
├── README.md
├── README.fa.md
└── ...
```

---

## 🚀 Installation & Setup

### Requirements

* Windows
* Python 3.11+
* Telegram account
* Telegram API ID
* Telegram API Hash

Main dependencies include:

* PySide6-Essentials
* Telethon
* Pillow
* pypdf

### Installation

Clone the repository:

```bash
git clone https://github.com/Soheilsadri278/Teloude.git
cd Teloude/teloude
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it in PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install the dependencies:

```powershell
pip install -e ".[dev]"
```

### Configure Telegram API

Set your API credentials using environment variables:

```powershell
$env:TELOUDE_API_ID="YOUR_API_ID"
$env:TELOUDE_API_HASH="YOUR_API_HASH"
```

Then run Teloude:

```powershell
python -m app
```

On the first launch, Teloude will guide you through Telegram authentication.

---

## 🔑 Telegram Login

On the first launch:

1. Enter your Telegram phone number.
2. Enter the Telegram login code.
3. Enter your Two-Step Verification password if enabled.

After authentication, the Telegram session is stored locally.

---

## 📁 Data Location

By default, Teloude stores its local application data in:

```text
%LOCALAPPDATA%\Teloude
```

Logs are stored in:

```text
%LOCALAPPDATA%\Teloude\logs\teloude.log
```

---

## 🧰 Command-Line Options

Teloude supports the following command-line options:

```text
--version
--data-dir PATH
--screenshot FILE
--open-proxy
```

For example:

```powershell
python -m app --version
```

---

## 📦 Building for Windows

Build the Windows application:

```powershell
powershell -File scripts\build_windows.ps1
```

Build the portable version:

```powershell
powershell -File scripts\build_portable.ps1
```

Build the portable ZIP package:

```powershell
powershell -File scripts\build_portable.ps1 -Zip
```

---

## 🔐 API Credentials

Teloude supports several ways to provide Telegram API credentials.

### 1. Environment Variables

```text
TELOUDE_API_ID
TELOUDE_API_HASH
```

### 2. Build-Time Configuration

Optional file:

```text
app/teloude_api.json
```

This file is excluded from Git.

### 3. User-Provided Credentials

Users can provide the required credentials during application setup.

Sensitive credentials are protected locally using Windows DPAPI.

---

## 🔄 Portability

The application and its assets are portable.

However, Telegram sessions and API credentials are intentionally not directly portable because sensitive data is protected using Windows DPAPI and is bound to the Windows user and machine.

Portable builds are shipped with empty application data.

---

## 🧪 Testing & Verification

Run the test suite:

```bash
python -m pytest -q
```

Run Ruff:

```bash
python -m ruff check .
```

Run benchmarks:

```bash
python scripts/benchmarks.py
```

---

## 🎨 Icons

The source icon files are located in:

```text
Logo&icon/
```

Rebuild the application icons with:

```bash
python scripts/make_icon.py
```

Generate an icon report with:

```bash
python scripts/icon_report.py
```

After changing the source icons, rebuild the application and shortcuts to update the generated assets.

---

## ⚠️ Current Limitations

* One file is processed as the primary transfer operation at a time, while its parts are transferred through a pipeline.
* Very small files, approximately 10 MB or less, do not support resumable transfers across application restarts.
* Telegram upload session lifetime is not guaranteed.
* Benchmarks use a fake transport and are not representative of real-world Telegram network performance.
* Current development is focused primarily on Windows.

---

## 📚 Documentation

Technical and development documentation is available in:

```text
docs/
```

This includes documentation for:

* Architecture
* Verification
* Packaging
* Development
* Testing
* Project checks

---

## 🧱 Technology Stack

* **Python**
* **PySide6**
* **Telethon**
* **SQLite**
* **Windows DPAPI**
* **Telegram MTProto**
* **Pillow**
* **pypdf**

---

## 📜 License

This project is released under the **MIT License**.

---

## 💡 The Idea

Teloude is designed for people who want to keep their own files in their own Telegram storage without relying on a separate third-party cloud storage provider.

**Your files. Your Telegram account. Your control.**
