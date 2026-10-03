"""Build-time configuration that is bundled *into* the application.

Currently exactly one thing lives here: the Telegram application credentials (API ID / API hash)
that a release build can ship so a normal user only ever has to enter phone -> code -> 2FA.

Rules
-----
* Nothing is ever hard-coded in the source tree. The file is optional and absent by default.
* It is ``app/teloude_api.json`` in a source checkout and ``<bundle>/teloude_api.json`` in a
  PyInstaller build (added by ``installer/teloude.spec``), so the same code path serves both.
* The values are handed straight to ``register_secret()`` by the settings service so they can never
  reach a log file, and this module never logs them itself.
* ``scripts/inject_credentials.py`` writes (and can remove) the file; the name is in ``.gitignore``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FILE_NAME = "teloude_api.json"

_SOURCE_ROOT = Path(__file__).resolve().parents[1]  # ``…/app`` in a source checkout


def bundle_roots() -> list[Path]:
    """Directories that may hold files bundled by PyInstaller, most specific first.

    The exact layout depends on the PyInstaller version and on ``onedir`` vs ``onefile``: modern
    ``onedir`` builds keep everything but the EXE under ``_internal`` (which is ``sys._MEIPASS``),
    while some data files end up next to the EXE instead. Checking every plausible root keeps the
    app working in both layouts instead of silently losing its icon or its credentials. A source
    checkout returns the ``app`` directory and the project root, because that is where the two
    bundled files live: ``app/teloude_api.json`` and ``assets/icon.ico``.
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass = Path(meipass)
        roots += [meipass, meipass.parent]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots += [exe_dir, exe_dir / "_internal"]
    if not roots:
        roots = [_SOURCE_ROOT, _SOURCE_ROOT.parent]  # app/ holds the credentials, ../assets the icon
    seen: set[Path] = set()
    unique = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def bundled_resource(rel: str) -> Path | None:
    """First existing bundled file called ``rel``, or ``None`` if it is not bundled."""
    for root in bundle_roots():
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def bundled_credentials_path() -> Path | None:
    """Where the bundled credentials file would be, whether or not it exists."""
    return bundled_resource(FILE_NAME) or bundle_roots()[0] / FILE_NAME


def bundled_api_credentials() -> tuple[int, str] | None:
    """``(api_id, api_hash)`` from the file bundled at build time, or ``None``."""
    path = bundled_credentials_path()
    if path is None or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        api_id = int(str(raw["api_id"]).strip())
        api_hash = str(raw["api_hash"]).strip()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if api_id <= 0 or not api_hash:
        return None
    return api_id, api_hash
