"""Read-only previews. Originals (local or in Telegram) are never modified; downloads go to a cache."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..infrastructure.repositories import Repos
from .ports import StorageRef, TelegramGateway
from .scanner import _long
from .search import TYPE_GROUPS

log = logging.getLogger("teloude.preview")

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic"}
RAW_EXT = {"cr2", "cr3", "nef", "arw", "dng", "raf", "orf", "rw2"}
VIDEO_EXT = set(TYPE_GROUPS["Videos"])
TEXT_EXT = {"txt", "md", "log", "csv", "json", "xml", "ini", "py", "js", "ts", "html", "css", "yml", "yaml", "toml", "cfg"}
MAX_EDGE = 1024


@dataclass
class PreviewResult:
    kind: str  # image | text | none
    png: bytes | None = None
    text: str | None = None
    note: str = ""


def render_image(source: bytes | str | Path, max_edge: int = MAX_EDGE) -> bytes:
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(source) if isinstance(source, bytes) else str(source))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_edge, max_edge))
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def extract_embedded_jpeg(data: bytes) -> bytes | None:
    """RAW files (CR2/NEF/ARW/DNG...) embed a full-size JPEG preview. Find the largest decodable one."""
    from PIL import Image

    starts: list[int] = []
    pos = 0
    while (pos := data.find(b"\xff\xd8\xff", pos)) != -1:
        starts.append(pos)
        pos += 3
    candidates: list[bytes] = []
    for i, s in enumerate(starts):
        end = data.find(b"\xff\xd9", s + 4)
        nxt = starts[i + 1] if i + 1 < len(starts) else len(data)
        e = (end + 2) if end != -1 else nxt
        candidates.append(data[s : max(e, s + 4)])
    for cand in sorted(candidates, key=len, reverse=True)[:5]:
        try:
            Image.open(io.BytesIO(cand)).verify()
            return cand
        except Exception:  # noqa: S112 - heuristics: keep trying smaller candidates
            continue
    return None


def text_preview(data: bytes, limit: int = 64 * 1024) -> str | None:
    chunk = data[:limit]
    if b"\x00" in chunk:
        return None
    return chunk.decode("utf-8", errors="replace")


def pdf_preview_text(path: str | Path, max_chars: int = 6000) -> str | None:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    out = []
    for page in reader.pages[:3]:
        out.append(page.extract_text() or "")
        if sum(map(len, out)) > max_chars:
            break
    text = "\n\n".join(out).strip()
    return text[:max_chars] or None


def build_preview(path: Path, ext: str) -> PreviewResult:
    """CPU/IO work for one local file; call from a worker thread."""
    try:
        if ext in IMAGE_EXT:
            return PreviewResult("image", png=render_image(path))
        if ext in RAW_EXT:
            jpeg = extract_embedded_jpeg(path.read_bytes())
            return PreviewResult("image", png=render_image(jpeg)) if jpeg else PreviewResult("none", note="No embedded RAW preview")
        if ext == "pdf":
            text = pdf_preview_text(path)
            return PreviewResult("text", text=text) if text else PreviewResult("none", note="PDF has no extractable text")
        if ext in TEXT_EXT or ext == "":
            text = text_preview(path.read_bytes()[: 64 * 1024])
            return PreviewResult("text", text=text) if text is not None else PreviewResult("none", note="Binary file")
    except Exception as exc:
        log.info("preview failed", extra={"data": {"ext": ext, "error": type(exc).__name__}})
        return PreviewResult("none", note="Preview unavailable for this file")
    return PreviewResult("none", note="No preview for this file type")


class PreviewService:
    def __init__(self, repos: Repos, gateway: TelegramGateway, cache_dir: Path, max_download_mb: int = 150):
        self.repos, self.gateway = repos, gateway
        self.cache_dir = Path(cache_dir)
        self.max_download = max_download_mb * 1024 * 1024
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _local_source(self, row) -> Path | None:  # type: ignore[no-untyped-def]
        lp = row["local_path"]
        if not lp:
            return None
        try:
            st = os.stat(_long(lp))
        except OSError:
            return None
        return Path(lp) if st.st_size == row["size"] and st.st_mtime_ns == row["mtime_ns"] else None

    async def get(self, file_id: int) -> PreviewResult:
        row = self.repos.files.get(file_id)
        if row is None:
            return PreviewResult("none", note="File not found")
        ext = row["ext"]
        local = self._local_source(row)  # read-only use of the user's own file
        if local is not None:
            return await asyncio.to_thread(build_preview, local, ext)
        msgs = self.repos.messages.for_file(file_id)
        srow = self.repos.storages.get(row["storage_id"])
        if not msgs or srow is None:
            return PreviewResult("none", note="No cloud copy available")
        ref = StorageRef(srow["tg_chat_id"], srow["tg_access_hash"], srow["title"], srow["uuid"])
        if ext in VIDEO_EXT:
            thumb = await self.gateway.download_thumbnail(ref, msgs[0]["message_id"])
            if thumb:
                return PreviewResult("image", png=await asyncio.to_thread(render_image, thumb))
            return PreviewResult("none", note="Telegram has no thumbnail for this video")
        if ext not in IMAGE_EXT | RAW_EXT | TEXT_EXT | {"pdf", ""}:
            return PreviewResult("none", note="No preview for this file type")
        if len(msgs) > 1 or row["size"] > self.max_download:
            return PreviewResult("none", note="File is too large to preview from the cloud")
        cached = self.cache_dir / f"{row['sha256'] or file_id}.{ext or 'bin'}"
        if not cached.exists():
            await self._download(ref, msgs[0]["message_id"], cached)
        return await asyncio.to_thread(build_preview, cached, ext)

    async def _download(self, ref: StorageRef, message_id: int, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".part")
        h = hashlib.sha256()
        with open(tmp, "wb") as fh:
            async for chunk in self.gateway.iter_download(ref, message_id, 0, 512 * 1024):
                fh.write(chunk)
                h.update(chunk)
        os.replace(tmp, dest)

    def prune_cache(self, max_bytes: int = 500 * 1024 * 1024) -> None:
        files = sorted(self.cache_dir.glob("*"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:  # only our own cache files are ever removed here
            if total <= max_bytes:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
