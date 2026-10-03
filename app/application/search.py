"""Global search over the local index of every Teloude storage (the DB is the search index)."""

from __future__ import annotations

from dataclasses import dataclass

from ..infrastructure.repositories import Repos

TYPE_GROUPS: dict[str, tuple[str, ...]] = {
    "Images": ("jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "cr2", "cr3", "nef", "arw", "dng", "raf", "orf", "rw2"),
    "Videos": ("mp4", "mov", "mkv", "avi", "wmv", "webm", "m4v"),
    "Documents": ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf", "odt"),
    "Archives": ("zip", "rar", "7z", "tar", "gz"),
}


@dataclass
class SearchHit:
    file_id: int
    name: str
    rel_path: str
    folder: str
    storage_id: int
    storage_title: str
    size: int
    ext: str


class SearchService:
    def __init__(self, repos: Repos):
        self.repos = repos

    def search(self, text: str, storage_id: int | None = None, type_group: str | None = None, limit: int = 500) -> list[SearchHit]:
        exts = TYPE_GROUPS.get(type_group or "")
        rows = self.repos.files.search(text, storage_id, None, limit * (4 if exts else 1))
        hits: list[SearchHit] = []
        for r in rows:
            if exts and r["ext"] not in exts:
                continue
            rel = r["rel_path"]
            hits.append(SearchHit(r["id"], r["name"], rel, rel.rsplit("/", 1)[0] if "/" in rel else "", r["storage_id"],
                                  r["storage_title"], r["size"], r["ext"]))  # fmt: skip
            if len(hits) >= limit:
                break
        return hits
