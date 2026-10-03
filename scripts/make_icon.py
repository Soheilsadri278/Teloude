"""Generate ``assets/icon.ico`` (16..256 px, all embedded) and ``assets/icon.png``.

The user's own branding is authoritative: the highest-resolution artwork in ``Logo&icon/`` (today
``icon-2.png``) becomes ``assets/logo.png``, so the packaged EXE, the installer, the shortcuts and
the Qt window all show exactly the same artwork. The painted gradient below is only a last-resort
fallback for a checkout with no artwork at all; it is never used to override the real logo.

Run ``scripts/icon_report.py`` to see whether every icon currently carries that master artwork.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
# Where the user's original artwork lives. ``assets/logo.png`` is the in-project copy used by builds;
# the repository-level ``Logo&icon/`` folder is the upstream source it was taken from.
UPSTREAM = ROOT.parent / "Logo&icon"
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
SIZE = 1024


def build() -> Image.Image:
    """Fallback artwork (only used if the user's branding asset is missing)."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    grad = Image.new("RGBA", (SIZE, SIZE))
    px = grad.load()
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x + y) / (2 * SIZE)
            px[x, y] = (int(77 + (94 - 77) * t), int(163 + (92 - 163) * t), int(255 + (230 - 255) * t), 255)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=int(SIZE * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    s = SIZE
    for box in ((0.22, 0.40, 0.54, 0.70), (0.38, 0.28, 0.72, 0.66), (0.55, 0.40, 0.79, 0.70)):
        d.ellipse([c * s for c in box], fill="white")
    d.rounded_rectangle((0.30 * s, 0.50 * s, 0.72 * s, 0.70 * s), radius=int(0.08 * s), fill="white")
    arrow = [(0.5, 0.40), (0.62, 0.54), (0.54, 0.54), (0.54, 0.66), (0.46, 0.66), (0.46, 0.54), (0.38, 0.54)]
    d.polygon([(x * s, y * s) for x, y in arrow], fill=(79, 125, 240, 255))
    return img


def candidates() -> list[Path]:
    """Every artwork file in ``Logo&icon/``, best-known names first.

    ``Logo&icon/`` may hold small preview renders (a 32x32 EXE preview, an installer preview) next
    to the real logo, so a name-based lookup alone can silently keep using an old file. Any PNG in
    that folder is accepted and the highest-resolution one wins in :func:`master`.
    """
    if not UPSTREAM.is_dir():
        return []
    named = [UPSTREAM / n for n in ("icon-2.png", "icon.png", "logo.png")]
    others = sorted(
        (p for p in UPSTREAM.glob("*.png") if p not in named), key=lambda p: p.stat().st_size, reverse=True
    )
    return [p for p in named if p.is_file()] + others


def score(path: Path) -> tuple[int, float] | None:
    """``(pixels, mtime)`` of a readable image, or ``None`` for anything that is not one.

    Resolution decides the master; the modification time only breaks a tie, so artwork the user
    drops into ``Logo&icon/`` beats an equally large file that was already there.
    """
    try:
        with Image.open(path) as im:
            pixels = im.size[0] * im.size[1]
    except (OSError, ValueError):  # not an image PIL can read
        return None
    return pixels, path.stat().st_mtime


def master() -> tuple[Image.Image, Path | None]:
    """The highest-resolution artwork available: the real logo beats the small preview renders.

    ``Logo&icon/`` is authoritative, so ``assets/logo.png`` - the copy this script wrote on an
    earlier run - is only used when there is no upstream artwork or it is the higher-resolution file.
    """
    best: tuple[int, float, Path] | None = None
    for candidate in candidates():
        found = score(candidate)
        if found is not None and (best is None or found > (best[0], best[1])):
            best = (*found, candidate)
    copy = ASSETS / "logo.png"
    found = score(copy) if copy.is_file() else None
    if found is not None and (best is None or found[0] > best[0]):
        best = (*found, copy)
    if best is None:
        return build(), None
    source = best[2]
    with Image.open(source) as im:
        img = im.convert("RGBA")
    largest = max(SIZES)[0]
    if img.width != largest or img.height != largest:
        img = img.resize((largest, largest), Image.LANCZOS)
    return img, source


# Mean 0..255 deviation that still counts as "the same picture": downscaling or re-encoding the
# identical artwork measures around 1, a genuinely different logo measures in the tens.
TOLERANCE = 8.0


def deviation(image: Image.Image, master: Image.Image) -> float:
    """Mean per-channel difference (0..255) between ``image`` and ``master``, compared at one size."""
    box = (min(image.width, master.width), min(image.height, master.height))
    left = image.convert("RGBA").resize(box, Image.LANCZOS)
    right = master.convert("RGBA").resize(box, Image.LANCZOS)
    raw = ImageChops.difference(left, right).tobytes()  # every channel of every pixel
    return sum(raw) / len(raw) if raw else 0.0


def worst_deviation(path: Path, master: Image.Image) -> float:
    """How far one icon file stray from the master; for an .ico, the worst of its embedded sizes."""
    with Image.open(path) as im:
        if path.suffix.lower() != ".ico":
            return deviation(im.convert("RGBA"), master)
        return max(deviation(im.ico.getimage(size), master) for size in set(im.ico.sizes()))


def differs(image: Image.Image, path: Path) -> bool:
    """True when ``path`` is missing, unreadable, or does not hold exactly these pixels."""
    if not path.is_file():
        return True
    try:
        with Image.open(path) as im:
            return im.size != image.size or ImageChops.difference(im.convert("RGBA"), image).getbbox() is not None
    except (OSError, ValueError):
        return True


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    img, source = master()
    if differs(img, ASSETS / "icon.png"):
        img.save(ASSETS / "icon.png")
    img.save(ASSETS / "icon.ico", sizes=SIZES)  # the file PyInstaller and the installer embed
    copy = ASSETS / "logo.png"  # the checked-in copy builds fall back on when Logo&icon/ is absent
    if source is not None and source.resolve() != copy.resolve() and differs(img, copy):
        img.save(copy)
        print("updated", copy, "from", source)
    print("wrote", ASSETS / "icon.ico", f"({', '.join(f'{w}x{h}' for w, h in SIZES)})", "from", source or "built-in fallback artwork")


if __name__ == "__main__":
    main()
