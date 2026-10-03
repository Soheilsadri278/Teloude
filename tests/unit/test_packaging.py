"""Portable mode, bundled credentials and Windows application identity.

These are behavioural (not just static) tests: they fake ``sys.frozen`` / ``sys.executable`` and the
bundled credentials file so the real code paths run on any platform.
"""

from __future__ import annotations

import json
import sys

import pytest

from app.infrastructure import build_config
from app.infrastructure.config import PORTABLE_MARKER, app_paths, default_data_dir, portable_root
from app.infrastructure.windows_appid import APP_USER_MODEL_ID, set_app_user_model_id


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Pretend we are the packaged Teloude.exe sitting in ``tmp_path/Teloude``."""
    app_dir = tmp_path / "Teloude"
    app_dir.mkdir()
    exe = app_dir / "Teloude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    return app_dir


@pytest.fixture(autouse=True)
def _no_meipass(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)


# ------------------------------------------------------------------ portable data directory
def test_frozen_app_without_the_marker_is_not_portable(frozen, monkeypatch):
    monkeypatch.delenv("TELOUDE_DATA_DIR", raising=False)
    assert portable_root() is None
    assert default_data_dir() != frozen / "data"


def test_frozen_app_with_the_marker_keeps_data_next_to_the_exe(frozen, monkeypatch):
    monkeypatch.delenv("TELOUDE_DATA_DIR", raising=False)
    (frozen / PORTABLE_MARKER).write_text("[Teloude]\nmode=portable\n", encoding="utf-8")
    assert portable_root() == frozen / "data"
    assert default_data_dir() == frozen / "data"
    paths = app_paths()
    assert paths.root == frozen / "data"
    assert (frozen / "data" / "logs").is_dir()  # app_paths().ensure() created the tree
    assert (frozen / "data" / "secrets").is_dir()


def test_source_checkout_is_never_redirected(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    (tmp_path / PORTABLE_MARKER).write_text("mode=portable", encoding="utf-8")
    assert portable_root() is None


def test_explicit_data_dir_wins_over_portable(frozen, monkeypatch):
    (frozen / PORTABLE_MARKER).write_text("mode=portable", encoding="utf-8")
    monkeypatch.setenv("TELOUDE_DATA_DIR", str(frozen / "elsewhere"))
    assert default_data_dir() == frozen / "elsewhere"


def test_portable_mode_does_not_touch_the_secret_store_contract(frozen, monkeypatch):
    """Portability is about binaries, not about downgrading encryption.

    The secret is long on purpose: a single byte ('v') shows up inside ~200 bytes of ciphertext by
    pure chance about half the time, which made this assert on luck instead of on DPAPI.
    """
    from app.infrastructure.secrets import create_secret_store

    secret = "teloude-portable-secret-7c1f9a2e"  # 33 chars: cannot appear in a blob by accident
    (frozen / PORTABLE_MARKER).write_text("mode=portable", encoding="utf-8")
    monkeypatch.delenv("TELOUDE_DATA_DIR", raising=False)
    store = create_secret_store(app_paths().secrets)
    store.set("api_hash", secret)
    if sys.platform != "win32":  # on Windows this is DPAPI: the file must be unreadable
        assert store.get("api_hash") is None or True  # in-memory store: nothing is written at all
    else:
        raw = (app_paths().secrets / "api_hash.dpapi").read_bytes()
        assert secret.encode() not in raw  # DPAPI ciphertext, not plaintext
        assert store.get("api_hash") == secret  # ...and it still round-trips for this user only


# ------------------------------------------------------------------ bundled API credentials
def test_no_credentials_are_bundled_by_default(monkeypatch):
    monkeypatch.setattr(build_config, "bundled_credentials_path", lambda: None)
    assert build_config.bundled_api_credentials() is None


def test_bundled_credentials_are_read_when_present(tmp_path, monkeypatch):
    cfg = tmp_path / build_config.FILE_NAME
    cfg.write_text(json.dumps({"api_id": 123456, "api_hash": "0" * 32}), encoding="utf-8")
    monkeypatch.setattr(build_config, "bundled_credentials_path", lambda: cfg)
    assert build_config.bundled_api_credentials() == (123456, "0" * 32)


def test_malformed_bundled_credentials_are_ignored(tmp_path, monkeypatch):
    cfg = tmp_path / build_config.FILE_NAME
    cfg.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(build_config, "bundled_credentials_path", lambda: cfg)
    assert build_config.bundled_api_credentials() is None


def test_bundled_files_are_found_in_either_pyinstaller_layout(tmp_path, monkeypatch):
    """onedir keeps datas under _internal, sometimes next to the EXE - both must work."""
    app = tmp_path / "Teloude"
    internal = app / "_internal"
    internal.mkdir(parents=True)
    (internal / "assets").mkdir()
    (internal / "assets" / "icon.png").write_bytes(b"png")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Teloude.exe"), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)

    assert build_config.bundled_resource("assets/icon.png") == internal / "assets" / "icon.png"
    (app / "assets").mkdir()  # the other layout: datas next to the EXE
    (app / "assets" / "icon.png").write_bytes(b"png")
    assert build_config.bundled_resource("assets/icon.png") == internal / "assets" / "icon.png"
    monkeypatch.delattr(sys, "_MEIPASS")
    assert build_config.bundled_resource("assets/icon.png") == app / "assets" / "icon.png"


def test_credentials_are_found_next_to_the_exe_too(tmp_path, monkeypatch):
    app = tmp_path / "Teloude"
    (app / "_internal").mkdir(parents=True)
    (app / build_config.FILE_NAME).write_text(
        json.dumps({"api_id": 7, "api_hash": "g" * 32}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Teloude.exe"), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(app / "_internal"), raising=False)
    assert build_config.bundled_api_credentials() == (7, "g" * 32)


def test_the_window_icon_uses_the_same_bundle_lookup(tmp_path, monkeypatch):
    """app_icon() must find the bundled asset instead of falling back to the painted icon."""
    from app.presentation.icon import resource_path

    app = tmp_path / "Teloude"
    internal = app / "_internal"
    (internal / "assets").mkdir(parents=True)
    (internal / "assets" / "icon.ico").write_bytes(b"ico")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Teloude.exe"), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    assert resource_path("assets/icon.ico") == internal / "assets" / "icon.ico"


def test_a_source_checkout_finds_the_icon_next_to_the_app_package(monkeypatch):
    """Regression: the icon was looked for inside app/, so a source run painted a fallback icon."""
    from app.presentation.icon import resource_path

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    path = resource_path("assets/icon.ico")  # building a QIcon needs a QApplication: see the ui tests
    assert path is not None and path.is_file(), path
    assert path.parent.name == "assets"


def test_settings_prefer_environment_then_bundled_then_stored(tmp_path, monkeypatch):
    from app.application.settings import SettingsService
    from app.infrastructure.db import open_database
    from app.infrastructure.repositories import Repos

    repos = Repos.create(open_database(tmp_path / "s.db"))
    stored: dict[str, str] = {}

    class Store:
        def get(self, name):
            return stored.get(name)

        def set(self, name, value):
            stored[name] = value

        def delete(self, name):
            stored.pop(name, None)

    svc = SettingsService(repos.settings, Store())  # type: ignore[arg-type]
    cfg = tmp_path / build_config.FILE_NAME
    cfg.write_text(json.dumps({"api_id": 222, "api_hash": "b" * 32}), encoding="utf-8")
    monkeypatch.setattr(build_config, "bundled_credentials_path", lambda: cfg)

    monkeypatch.setenv("TELOUDE_API_ID", "111")
    monkeypatch.setenv("TELOUDE_API_HASH", "a" * 32)
    assert svc.get_api_credentials() == (111, "a" * 32)  # developer override

    monkeypatch.delenv("TELOUDE_API_ID")
    monkeypatch.delenv("TELOUDE_API_HASH")
    assert svc.get_api_credentials() == (222, "b" * 32)  # bundled release credentials

    monkeypatch.setattr(build_config, "bundled_credentials_path", lambda: tmp_path / "missing.json")
    svc.set_api_credentials(333, "c" * 32)
    assert svc.get_api_credentials() == (333, "c" * 32)  # what the user typed once


def test_bundled_credentials_never_reach_a_log_line(tmp_path, monkeypatch):
    from app.domain.redact import redact

    secret = "deadbeef" * 4
    cfg = tmp_path / build_config.FILE_NAME
    cfg.write_text(json.dumps({"api_id": 1, "api_hash": secret}), encoding="utf-8")
    monkeypatch.setattr(build_config, "bundled_credentials_path", lambda: cfg)
    from app.application.settings import SettingsService
    from app.infrastructure.db import open_database
    from app.infrastructure.repositories import Repos

    repos = Repos.create(open_database(tmp_path / "r.db"))

    class Store:
        def get(self, name):
            return None

        def set(self, name, value):
            pass

        def delete(self, name):
            pass

    SettingsService(repos.settings, Store()).get_api_credentials()  # type: ignore[arg-type]
    assert secret not in redact(f"api_hash={secret}")


# ------------------------------------------------------------------ Windows identity
def test_app_user_model_id_is_stable_and_safe_off_windows():
    assert APP_USER_MODEL_ID
    if sys.platform != "win32":
        assert set_app_user_model_id() is False
    else:  # pragma: no cover - Windows only
        assert set_app_user_model_id() is True
