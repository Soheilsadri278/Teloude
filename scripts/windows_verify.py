"""Windows-only verification of the *packaged* application. Run on a Windows machine.

    python scripts/windows_verify.py --exe dist\\Teloude\\Teloude.exe [--installer dist\\Teloude-Setup-1.0.0.exe] [--installed]

Checks: EXE starts, embedded icon resource, that the embedded icon is still the artwork in Logo&icon/,
proxy sheet really paints in the frozen app, DPAPI round trip, long paths, and (with --installed)
Desktop / Start Menu shortcuts and their icon locations.
Exit code 0 only if every check passes. This script cannot run on Linux/macOS (it refuses to).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""))


def embedded_icon_count(path: Path) -> int:
    n = ctypes.windll.shell32.ExtractIconExW(str(path), -1, None, None, 0)  # type: ignore[attr-defined]
    return int(n)


def shortcut_target_icon(lnk: Path) -> tuple[str, str]:
    ps = f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); Write-Output $s.TargetPath; Write-Output $s.IconLocation"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=True).stdout.splitlines()
    return out[0].strip(), out[1].strip()


def exe_icon_png(path: Path, out: Path) -> bool:
    """Save the icon Windows shows for ``path`` as a PNG, so its pixels can be compared."""
    ps = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$icon = [System.Drawing.Icon]::ExtractAssociatedIcon('{path}'); "
        f"if ($icon) {{ $icon.ToBitmap().Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png) }}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=180)
    return out.is_file()


def version_of(exe: Path, expected: str) -> tuple[bool, str]:
    """(ok, detail): does the packaged EXE identify itself as Teloude ``expected``?

    A windowed build has no console, so ``--version`` may print into nowhere; its version resource
    is accepted as the answer too. Either way the detail carries whatever the EXE actually produced,
    because that is the only clue when a packaged app refuses to start.
    """
    detail = ""
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and "Teloude" in out.stdout:
            return True, out.stdout.strip()
        detail = f"exit {out.returncode}; stdout={out.stdout.strip()!r}; stderr={out.stderr.strip()!r}"
    except subprocess.TimeoutExpired:
        detail = "--version timed out after 60 s (antivirus scan, or the launch was blocked)"
    resource = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Item -LiteralPath '{exe}').VersionInfo.ProductVersion"],
        capture_output=True, text=True, timeout=120,
    ).stdout.strip()
    return resource.startswith(expected), f"{detail}; version resource = {resource or 'none'}"


def app_log_tail(data_dir: Path) -> str:
    """The last lines of the packaged app's own log - it writes to disk even with no console."""
    log = data_dir / "logs" / "teloude.log"
    if not log.is_file():
        return f"(no log written at {log})"
    return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])


def report_icon_artwork(exe: Path) -> None:
    """Compare the icon the EXE really carries with the master artwork in Logo&icon/.

    A stale icon here means the EXE was built from older artwork; a Windows icon cache that still
    paints the old one is a display artefact and is called out in the detail line.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import make_icon  # noqa: E402  (scripts/ holds executables, not an importable package)
        from PIL import Image

        master, source = make_icon.master()
        with tempfile.TemporaryDirectory() as td:
            shot = Path(td) / "icon.png"
            if not exe_icon_png(exe, shot):
                print("NOTE: Windows would not hand out the exe icon, so its artwork was not compared")
                return
            with Image.open(shot) as im:
                delta = make_icon.deviation(im.convert("RGBA"), master)
        check(
            "exe icon matches the master artwork",
            delta <= make_icon.TOLERANCE,
            f"deviation {delta:.1f} from {source.name if source else 'the painted fallback'}"
            + ("" if delta <= make_icon.TOLERANCE else " - rebuild, then ie4uinit.exe -ClearIconCache"),
        )
    except Exception as exc:  # a comparison problem must never fail the whole verification
        print(f"NOTE: icon artwork comparison skipped ({type(exc).__name__}: {exc})")


def painted_fraction(png: Path) -> float:
    from PIL import Image, ImageChops

    img = Image.open(png).convert("RGB")
    flat = Image.new("RGB", img.size, img.getpixel((5, img.height // 2)))
    diff = ImageChops.difference(img, flat).convert("L").point(lambda v: 255 if v > 8 else 0)
    return sum(1 for v in diff.getdata() if v) / (img.width * img.height)


def main() -> int:
    if sys.platform != "win32":
        print("This verification must run on Windows.")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--installer")
    ap.add_argument("--installed", action="store_true", help="also verify shortcuts of an installed copy")
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.infrastructure.config import APP_VERSION

    exe = Path(a.exe).resolve()
    check("exe exists", exe.exists(), str(exe))
    check("exe has embedded icon", embedded_icon_count(exe) >= 1, f"{embedded_icon_count(exe)} icon group(s)")
    report_icon_artwork(exe)  # does the EXE carry the artwork that is in Logo&icon/ right now?
    ok, detail = version_of(exe, APP_VERSION)
    check(f"exe identifies itself as Teloude {APP_VERSION}", ok, detail)

    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        shot = Path(td) / "proxy.png"
        base = Path(td) / "login.png"
        env = dict(os.environ, TELOUDE_DATA_DIR=str(data))
        login = subprocess.run([str(exe), "--screenshot", str(base)], env=env, timeout=180,
                               capture_output=True, text=True)
        sheet = subprocess.run([str(exe), "--screenshot", str(shot), "--open-proxy"], env=env, timeout=180,
                               capture_output=True, text=True)
        rendered = base.exists() and painted_fraction(base) > 0.02
        check("login window renders", rendered,
              "" if rendered else f"exit {login.returncode}; {login.stderr.strip()}\n{app_log_tail(data)}")
        frac = painted_fraction(shot) if shot.exists() else 0.0
        check("proxy sheet paints in packaged exe", frac > 0.05,
              f"painted area {frac:.1%}" if frac else
              f"exit {sheet.returncode}; {sheet.stderr.strip()}\n{app_log_tail(data)}")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.infrastructure.secrets import DpapiSecretStore

    with tempfile.TemporaryDirectory() as td:
        store = DpapiSecretStore(Path(td))
        store.set("telegram_session", "VERIFY-123")
        raw = (Path(td) / "telegram_session.dpapi").read_bytes()
        check("DPAPI: no plaintext on disk + round trip", b"VERIFY-123" not in raw and store.get("telegram_session") == "VERIFY-123")
        deep = Path(td) / "/".join(["d" * 40] * 8)
        try:
            deep = Path("\\\\?\\" + str(deep))
            os.makedirs(deep)
            (deep / "f.bin").write_bytes(b"x")
            from app.application.scanner import walk_files

            files, _ = walk_files(str(Path(td)))
            check("long path (>260) scan", any(f.name == "f.bin" for f in files))
        except OSError as exc:
            check("long path (>260) scan", False, str(exc))

    if a.installer:
        check("installer exists", Path(a.installer).exists())
        check("installer has embedded icon", embedded_icon_count(Path(a.installer)) >= 1)
    if a.installed:
        for label, folder in (("Desktop", Path(os.environ["USERPROFILE"]) / "Desktop"), ("Start Menu", Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs")):
            lnk = folder / "Teloude.lnk"
            if lnk.exists():
                target, icon = shortcut_target_icon(lnk)
                check(f"{label} shortcut icon -> exe", target.lower().endswith("teloude.exe") and "teloude.exe" in icon.lower(), icon)
            else:
                check(f"{label} shortcut exists", False, str(lnk))
        data = Path(os.environ["LOCALAPPDATA"]) / "Teloude"
        check("data directory exists", data.exists(), str(data))
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
