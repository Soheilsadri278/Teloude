"""Static verification of a portable Teloude tree (``dist/Teloude-Portable/Teloude``).

    python scripts/portable_verify.py --root dist/Teloude-Portable/Teloude

It checks the *structure* of the distribution and, importantly, that no user data or plaintext
secret was shipped by accident. It does not execute the EXE; on Windows use
``scripts/windows_verify.py --exe ...`` for that.

Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="dist/Teloude-Portable/Teloude", help="the portable application folder")
    a = ap.parse_args()
    root = Path(a.root)

    check("portable folder exists", root.is_dir(), str(root))
    if not root.is_dir():
        return 1

    exe = root / "Teloude.exe"
    check("Teloude.exe present", exe.is_file(), str(exe))
    check("portable marker present", (root / "portable.ini").is_file())
    check("data folder present", (root / "data").is_dir())
    check("internal runtime present", (root / "_internal").is_dir() and any((root / "_internal").iterdir()))
    # PyInstaller >= 6 keeps bundled data under _internal in a onedir build; older versions (and
    # some datas) put it next to the EXE. The app finds it either way - here we only insist it exists.
    assets = [p for p in (root / "assets", root / "_internal" / "assets") if p.is_dir()]
    check("assets present", bool(assets), ", ".join(str(p.relative_to(root)) for p in assets))
    icons = [p / "icon.ico" for p in assets if (p / "icon.ico").is_file()]
    check("icon shipped with the app", bool(icons), ", ".join(str(p.relative_to(root)) for p in icons))

    # A portable build must never carry somebody else's backup state or decrypted secrets.
    forbidden = ("teloude.db", "teloude.db-wal", ".dpapi", ".insecure")
    shipped = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and (p.name in forbidden or p.name.startswith("teloude.db") or p.suffix in {".dpapi", ".insecure"})
    )
    check("no user data / secret files shipped", not shipped, ", ".join(shipped) if shipped else "data/ is clean")

    data = root / "data"
    leftovers = sorted(p.name for p in data.iterdir()) if data.is_dir() else []
    check("data folder is empty on a fresh build", not leftovers, ", ".join(leftovers))

    if sys.platform != "win32":
        print("\nNOTE: this machine is not Windows - the EXE was NOT executed and the icon resource")
        print("      was NOT read. Build and verify on Windows with scripts/windows_verify.py.")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
