"""``scripts/icon_report.py`` answers "does this icon still show the old logo?"."""

from __future__ import annotations

import importlib.util
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
def report():
    return _load("icon_report")


def test_identical_and_different_pictures_measure_apart(report):
    logo = Image.new("RGBA", (64, 64), (10, 20, 30, 255))
    deviation, tolerance = report.make_icon.deviation, report.make_icon.TOLERANCE
    assert deviation(logo, logo.copy()) == 0.0
    assert deviation(Image.new("RGBA", (64, 64), (240, 230, 220, 255)), logo) > tolerance


def test_the_artwork_shipped_today_is_reported_as_consistent(report, capsys):
    assert {p.name for p in report.icon_files()} >= {"icon-2.png", "icon.png", "icon.ico"}
    assert report.main() == 0
    out = capsys.readouterr().out
    assert "icon-2.png" in out and "assets/icon.ico" in out


def test_icons_that_do_not_carry_the_new_artwork_are_flagged(report, tmp_path, monkeypatch, capsys):
    """Drop a different logo in ``Logo&icon/``: the shipped icons must be reported stale."""
    upstream = tmp_path / "Logo&icon"
    upstream.mkdir()
    Image.new("RGBA", (512, 512), (255, 0, 0, 255)).save(upstream / "brand-new-logo.png")
    monkeypatch.setattr(report.make_icon, "UPSTREAM", upstream)
    monkeypatch.setattr(report, "UPSTREAM", upstream)
    assert report.main() == 1
    out = capsys.readouterr().out
    assert "STALE" in out and "icon.ico" in out
