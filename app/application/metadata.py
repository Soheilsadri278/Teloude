"""Caption metadata attached to every uploaded volume.

The local SQLite DB is the primary index, but captions make the cloud self-describing so another PC
(or this PC after losing its database) can rebuild the file index from Telegram alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..domain.errors import PermanentTransferError

MARKER = "teloude:v1"


@dataclass(frozen=True)
class VolumeMeta:
    rel_path: str
    sha256: str
    size: int  # size of the whole logical file
    mtime_ns: int
    volume_index: int
    volumes_total: int
    version: int
    upload_uid: str  # groups the volumes of one logical-file version

    def encode(self) -> str:
        payload = {
            "p": self.rel_path, "h": self.sha256, "s": self.size, "m": self.mtime_ns,
            "vi": self.volume_index, "vn": self.volumes_total, "ver": self.version, "u": self.upload_uid,
        }  # fmt: skip
        return f"{MARKER}\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"


def encode_caption(meta: VolumeMeta, limit: int) -> str:
    text = meta.encode()
    if len(text) > limit:
        raise PermanentTransferError(
            f"path too long for Telegram caption metadata ({len(text)} > {limit} characters)"
        )
    return text


def decode_caption(caption: str | None) -> VolumeMeta | None:
    if not caption or not caption.startswith(MARKER):
        return None
    try:
        d = json.loads(caption.split("\n", 1)[1])
        return VolumeMeta(
            rel_path=str(d["p"]), sha256=str(d["h"]), size=int(d["s"]), mtime_ns=int(d["m"]),
            volume_index=int(d["vi"]), volumes_total=int(d["vn"]), version=int(d.get("ver", 1)),
            upload_uid=str(d["u"]),
        )  # fmt: skip
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def volume_filename(name: str, index: int, total: int) -> str:
    return name if total == 1 else f"{name}.teloude-{index + 1:03d}of{total:03d}"
