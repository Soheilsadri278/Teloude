"""Static / structural regressions that are not covered by behavioural tests elsewhere.

* sequential-upload, blocking-callback, retry-accumulation, restart-from-zero  -> tests/transfer/test_pipeline.py,
  tests/integration/test_backup.py (named explicitly there)
* invisible proxy window -> tests/ui/test_proxy_ui.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def sources():
    for p in (ROOT / "app").rglob("*.py"):
        yield p, p.read_text(encoding="utf-8")


def test_only_allow_listed_modules_may_delete_files():
    """Accidental local-file deletion guard: Teloude's own temp/cache/secret files are the only things ever removed."""
    pat = re.compile(r"\.unlink\(|os\.remove\(|os\.unlink\(|rmtree\(|\bsend2trash\b|\.rmdir\(")
    allowed = {
        "app/application/restore.py",  # its own corrupt .teloude-part temp file
        "app/application/preview.py",  # its own cache directory
        "app/infrastructure/secrets.py",  # secret files
    }
    offenders = sorted({str(p.relative_to(ROOT)).replace("\\", "/") for p, s in sources() if pat.search(s)} - allowed)
    assert offenders == []


def test_restore_unlink_only_touches_the_temp_file():
    src = (ROOT / "app/application/restore.py").read_text()
    calls = re.findall(r"(\w+)\.unlink\(", src)
    assert calls == ["temp"]


def test_backup_and_scanner_never_write_to_user_files():
    for rel in ("app/application/backup.py", "app/application/scanner.py", "app/application/pipeline.py"):
        s = (ROOT / rel).read_text()
        assert not re.search(r"open\([^)]*['\"][wa+][b]?['\"]", s), rel  # read-only access to the user's data


def test_no_sleep_in_async_hot_paths():
    for rel in ("app/application/pipeline.py", "app/domain/ratelimit.py", "app/application/backup.py"):
        assert "time.sleep(" not in (ROOT / rel).read_text(), rel


def test_no_hardcoded_telegram_credentials_or_secrets_in_source():
    for p, s in sources():
        assert not re.search(r"api_hash\s*=\s*['\"][0-9a-f]{32}['\"]", s), p
        assert "TELOUDE_API_HASH" not in s or p.name == "settings.py"


def test_no_bot_api_used():
    for p, s in sources():
        assert "api.telegram.org/bot" not in s and "python-telegram-bot" not in s and "telebot" not in s


# --------------------------------------------------------------------------- packaging (stale icon regression)
def test_ico_contains_all_required_sizes():
    from PIL import IcoImagePlugin, Image

    with Image.open(ROOT / "assets/icon.ico") as ico:
        sizes = set(ico.info.get("sizes", set()) or ico.ico.sizes())  # type: ignore[attr-defined]
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes
    assert IcoImagePlugin


def test_pyinstaller_embeds_icon_and_version_resource():
    spec = (ROOT / "installer/teloude.spec").read_text()
    assert 'icon=ICON' in spec and 'assets" / "icon.ico"' in spec and "version_info.txt" in spec
    assert "console=False" in spec
    assert (ROOT / "installer/version_info.txt").exists()


def test_installer_shortcuts_point_at_exe_icon_and_refresh_icon_cache():
    iss = (ROOT / "installer/teloude.iss").read_text()
    icons = re.findall(r"^Name: \"\{auto(?:programs|desktop)\}.*$", iss, re.M)
    assert len(icons) == 2
    for line in icons:  # explicit icon source + index: shortcuts can never keep a stale cached/generic icon
        assert 'IconFilename: "{app}\\{#AppExe}"' in line and "IconIndex: 0" in line
    assert "SetupIconFile=..\\assets\\icon.ico" in iss
    assert "UninstallDisplayIcon={app}\\{#AppExe}" in iss
    assert re.search(r"^ChangesAssociations=yes", iss, re.M)  # makes setup broadcast an icon-cache refresh
    assert 'Name: "desktopicon"' in iss and "autostart" in iss and "[Run]" in iss
    assert "UninstallDelete" not in iss.split("; User data", 1)[0]  # user data preserved on uninstall


def test_windows_verify_script_covers_required_checks():
    s = (ROOT / "scripts/windows_verify.py").read_text()
    for needle in ("embedded icon", "proxy sheet paints", "DPAPI", "long path", "Desktop", "Start Menu", "IconLocation"):
        assert needle in s


def test_portable_build_script_produces_a_real_portable_tree():
    ps1 = (ROOT / "scripts/build_portable.ps1").read_text(encoding="utf-8")
    # Teloude.exe + _internal + assets + a writable data folder + the marker that redirects it
    for needle in ("Teloude-Portable", "_internal", "assets", '"data"', "portable.ini", "README.txt"):
        assert needle in ps1, needle
    assert "Copy-Item" in ps1  # it really copies the PyInstaller tree instead of re-inventing one
    # ...and it must never ship a database or a decrypted secret with it
    assert ".dpapi" in ps1 and "teloude.db" in ps1 and "throw" in ps1
    assert (ROOT / "scripts/portable_verify.py").exists()


def test_portable_mode_is_detected_by_the_app_not_only_by_the_installer():
    cfg = (ROOT / "app/infrastructure/config.py").read_text(encoding="utf-8")
    assert "PORTABLE_MARKER" in cfg and "portable_root" in cfg
    main = (ROOT / "app/__main__.py").read_text(encoding="utf-8")
    assert "--data-dir" in main  # an explicit override is still possible for support/debug


def test_packaging_check_script_exists_and_runs_on_any_platform():
    src = (ROOT / "scripts/packaging_check.py").read_text(encoding="utf-8")
    for needle in ("icon.ico", "IconIndex: 0", "SetupIconFile", "portable.ini", "teloude_api.json"):
        assert needle in src
    assert "packaging_check.py" in (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")


def test_windows_taskbar_identity_is_set_explicitly():
    main = (ROOT / "app/__main__.py").read_text(encoding="utf-8")
    assert "set_app_user_model_id" in main
    src = (ROOT / "app/infrastructure/windows_appid.py").read_text(encoding="utf-8")
    assert "SetCurrentProcessExplicitAppUserModelID" in src and "APP_USER_MODEL_ID" in src


def test_login_only_asks_for_api_credentials_when_none_are_available():
    """Bundled or environment credentials must let the user go straight to phone -> code -> 2FA."""
    settings = (ROOT / "app/application/settings.py").read_text(encoding="utf-8")
    assert "bundled_api_credentials" in settings
    assert "TELOUDE_API_ID" in settings
    build = (ROOT / "app/infrastructure/build_config.py").read_text(encoding="utf-8")
    assert "_MEIPASS" in build  # works from source AND from the frozen bundle
    assert not (ROOT / "app" / "teloude_api.json").exists()


def test_credentials_file_is_git_ignored_in_both_projects():
    for gi in (ROOT / ".gitignore", ROOT.parent / ".gitignore"):
        assert gi.is_file(), gi
        assert "teloude_api.json" in gi.read_text(encoding="utf-8"), gi


def test_pyproject_has_ruff_and_pytest_config():
    t = (ROOT / "pyproject.toml").read_text()
    assert "[tool.ruff" in t and "[tool.pytest.ini_options]" in t
    with pytest.raises(AssertionError):
        assert "telegram-bot" in t
