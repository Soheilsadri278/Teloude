"""Teloude entry point.  ``python -m app``  (or the packaged Teloude.exe)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .infrastructure.config import APP_NAME, APP_VERSION, app_paths
from .infrastructure.logging_setup import configure_logging
from .infrastructure.windows_appid import set_app_user_model_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="teloude")
    parser.add_argument("--data-dir", help="override the data directory")
    parser.add_argument("--screenshot", help="render the window to PNG and exit (used by verification scripts)")
    parser.add_argument("--open-proxy", action="store_true", help="open the proxy sheet before the screenshot")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .presentation.controller import AppController
    from .presentation.icon import app_icon
    from .presentation.main_window import MainWindow
    from .presentation.theme import apply_theme

    paths = app_paths(Path(args.data_dir) if args.data_dir else None)
    configure_logging(paths.logs)
    log = logging.getLogger("teloude")
    log.info("starting %s %s", APP_NAME, APP_VERSION)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)  # closing the window minimises to tray instead of quitting
    lock = QLockFile(str(paths.root / "teloude.lock"))
    lock.setStaleLockTime(30_000)
    if not args.screenshot and not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "Teloude is already running (check the system tray).")
        return 0

    icon = app_icon()
    app.setWindowIcon(icon)
    set_app_user_model_id()  # taskbar identity: use OUR icon, not the host process's
    ctl = AppController(paths)
    if ctl.db.quarantined:
        log.error("database was corrupt and has been quarantined at %s", ctl.db.quarantined)
    palette = apply_theme(app, ctl.settings.load().theme)
    win = MainWindow(ctl, icon, palette)
    win.show()
    if not args.screenshot:
        ctl.start()
    if ctl.db.quarantined:
        win._toast(f"The local database was damaged. A copy was kept at {ctl.db.quarantined.name}. Use 'Find on Telegram' to rebuild your index.")
    if args.screenshot:
        def snap() -> None:
            if args.open_proxy:
                win.sheet.open_sheet(animated=False)
            QTimer.singleShot(400, lambda: (win.grab().save(args.screenshot), app.quit()))

        QTimer.singleShot(300, snap)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
