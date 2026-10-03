"""Report which artwork every icon in the project currently carries.

    python scripts/icon_report.py

Reach for this when an icon "has not changed". It prints the size and sha256 of every icon file and
how far each one deviates from the master artwork ``make_icon.py`` picks:

* ``0.0`` - pixel-identical, the file already carries the current logo;
* a small number (a few units out of 255) - the same artwork, just downscaled or re-encoded;
* a large number - that file still shows a *different* image, so it was never regenerated (or the
  artwork it was made from was never replaced).

Exit code is 1 when some icon is still a different image, 0 when they all follow the master.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
UPSTREAM = ROOT.parent / "Logo&icon"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_icon  # noqa: E402  (scripts/ is a folder of executables, not an importable package)


def sha256(path: Path) -> str:
    """Short fingerprint of a file's exact bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def icon_files() -> list[Path]:
    """Every icon in the checkout: the upstream artwork first, then what the build actually ships."""
    found = sorted(p for p in UPSTREAM.glob("*.png") if p.is_file()) if UPSTREAM.is_dir() else []
    return found + [p for p in (ASSETS / "logo.png", ASSETS / "icon.png", ASSETS / "icon.ico") if p.is_file()]


def display_size(path: Path) -> str:
    """What to print in the size column: an .ico holds several sizes, so name them all."""
    with Image.open(path) as im:
        if path.suffix.lower() != ".ico":
            return f"{im.width}x{im.height}"
        sizes = sorted(set(im.ico.sizes()))
        return f"{sizes[-1][0]}x{sizes[-1][1]} x{len(sizes)}"


def main() -> int:
    master, source = make_icon.master()
    rows: list[tuple[Path, str, float]] = []
    for path in icon_files():
        rows.append((path, display_size(path), make_icon.worst_deviation(path, master)))

    print("master artwork:", source or "(none - painted fallback)")
    print(f"{'file':46s} {'size':>9s}  {'sha256':16s}  deviation")
    for path, box, delta in rows:
        where = (path.relative_to(ROOT.parent) if path.is_relative_to(ROOT.parent) else path).as_posix()
        print(f"{where:46s} {box:>9s}  {sha256(path):16s}  {delta:5.1f}")

    stale = [(p, d) for p, _, d in rows if d > make_icon.TOLERANCE]
    if stale:
        print(f"\nSTALE: {len(stale)} file(s) do not show the master artwork (run scripts/make_icon.py):")
        for path, delta in stale:
            print(f"  {path} - deviation {delta:.1f}")
        return 1
    print("\nOK: every icon carries the master artwork")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
