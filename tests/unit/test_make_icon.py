"""``scripts/make_icon.py`` must follow whatever artwork sits in ``Logo&icon/``.

These run against a throwaway ``Logo&icon/`` folder, so they never touch the real branding.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    """Import a file from ``scripts/`` (a folder of executables, not an importable package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def make_icon(tmp_path, monkeypatch):
    module = _load("make_icon")
    upstream, assets = tmp_path / "Logo&icon", tmp_path / "assets"
    upstream.mkdir()
    assets.mkdir()
    monkeypatch.setattr(module, "UPSTREAM", upstream)
    monkeypatch.setattr(module, "ASSETS", assets)
    return module


def _png(path: Path, size: int, colour: tuple[int, int, int, int]) -> Path:
    Image.new("RGBA", (size, size), colour).save(path)
    return path


def test_new_artwork_wins_even_under_an_unexpected_name(make_icon):
    """Regression: the old lookup only knew ``icon-2.png``, so new artwork was silently ignored."""
    upstream = make_icon.UPSTREAM
    _png(upstream / "icon-2.png", 256, (0, 0, 255, 255))  # the previous logo, known name
    _png(upstream / "brand-new-logo.png", 512, (255, 0, 0, 255))  # replacement, different name
    _png(upstream / "icon-teloude-exe.png", 32, (255, 0, 0, 255))  # small preview of the new one
    img, source = make_icon.master()
    assert source == upstream / "brand-new-logo.png"
    assert img.size == (256, 256)  # normalised to the largest .ico size


def test_newer_artwork_wins_when_the_resolution_is_the_same(make_icon):
    """A replacement of the same size is recognised by its mtime, not by its luck in the sort order."""
    upstream = make_icon.UPSTREAM
    old = _png(upstream / "icon-2.png", 256, (0, 0, 255, 255))
    new = _png(upstream / "logo-2026.png", 256, (0, 255, 0, 255))
    os.utime(old, (1, 1))  # the current logo has been sitting in the folder for a while
    os.utime(new, (2, 2))  # this one was just dropped in
    _img, source = make_icon.master()
    assert source == new


def test_main_writes_every_icon_from_the_master(make_icon):
    upstream, assets = make_icon.UPSTREAM, make_icon.ASSETS
    _png(upstream / "icon-2.png", 512, (12, 34, 56, 255))
    make_icon.main()
    assert not make_icon.differs(Image.new("RGBA", (256, 256), (12, 34, 56, 255)), assets / "icon.png")
    assert not make_icon.differs(Image.new("RGBA", (256, 256), (12, 34, 56, 255)), assets / "logo.png")
    with Image.open(assets / "icon.ico") as ico:
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(ico.ico.sizes())


def test_running_it_twice_changes_nothing(make_icon, capsys):
    upstream, assets = make_icon.UPSTREAM, make_icon.ASSETS
    _png(upstream / "icon-2.png", 512, (12, 34, 56, 255))
    make_icon.main()
    first = {p.name: p.read_bytes() for p in assets.iterdir()}
    capsys.readouterr()
    make_icon.main()
    assert {p.name: p.read_bytes() for p in assets.iterdir()} == first
    assert "updated" not in capsys.readouterr().out  # no needless churn in the artwork files
