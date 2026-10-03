"""Windows application identity (AppUserModelID).

Why this exists
---------------
Windows groups taskbar buttons by *AppUserModelID*. Without an explicit one, a PySide6 application
inherits the identity of its host process (``python.exe`` when run from source, or the PyInstaller
bootloader), and Windows may then show that host's icon in the taskbar even though the window and
the EXE both show the Teloude icon. Setting an explicit AppUserModelID pins the taskbar entry (and
its jump list and toast attribution) to Teloude.

This is the only place that touches the Win32 shell API; on every other platform the call is a
no-op, and a failure is logged and ignored because it can only ever affect cosmetics.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("teloude.appid")

# Keep this stable for the lifetime of the product: changing it detaches existing taskbar pins.
APP_USER_MODEL_ID = "Teloude.Backup.1"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Register ``app_id`` for this process. Returns True when Windows accepted it."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)  # type: ignore[attr-defined]
        ok = int(hr) == 0
        if not ok:
            log.warning("SetCurrentProcessExplicitAppUserModelID returned 0x%08X", int(hr) & 0xFFFFFFFF)
        return ok
    except Exception as exc:  # never let a cosmetic Windows call break startup
        log.warning("could not set the Windows application identity: %s", type(exc).__name__)
        return False
