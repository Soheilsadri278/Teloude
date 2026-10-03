"""Where Teloude keeps its data. Nothing here is a secret."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Teloude"
APP_VERSION = "1.0.0"

#: A file with this name next to ``Teloude.exe`` switches the *frozen* app into portable mode:
#: the database, logs, cache and protected session then live in ``<exe dir>\data`` instead of
#: ``%LOCALAPPDATA%\Teloude``. It is created by ``scripts/build_portable.ps1``.
PORTABLE_MARKER = "portable.ini"
PORTABLE_DATA_DIR = "data"


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "teloude.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    def ensure(self) -> AppPaths:
        for p in (self.root, self.logs, self.secrets, self.cache):
            p.mkdir(parents=True, exist_ok=True)
        return self


def portable_root() -> Path | None:
    """The portable data directory (``<exe dir>/data``) when running from a portable build.

    Only ever applies to a *packaged* application (``sys.frozen``) that ships the marker file, so a
    from-source checkout is never redirected. Returns ``None`` for an installed/portable-less build,
    and for any portable build whose marker was removed by the user.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    exe_dir = exe.parent
    if not (exe_dir / PORTABLE_MARKER).exists():
        return None
    return exe_dir / PORTABLE_DATA_DIR


def default_data_dir() -> Path:
    override = os.environ.get("TELOUDE_DATA_DIR")
    if override:
        return Path(override)
    portable = portable_root()
    if portable is not None:
        return portable
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / APP_NAME


def app_paths(root: Path | None = None) -> AppPaths:
    return AppPaths(root or default_data_dir()).ensure()
