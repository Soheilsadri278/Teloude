"""Storage lifecycle: create, discover from Telegram (no local DB needed) and sync the file index."""

from __future__ import annotations

import logging
import posixpath
import uuid as uuidlib
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.models import FileState
from ..infrastructure.repositories import Repos
from .metadata import VolumeMeta, decode_caption
from .ports import StorageRef, TelegramGateway

log = logging.getLogger("teloude.storage")

STORAGE_PREFIX = "Teloude - "


@dataclass
class SyncReport:
    storage_id: int
    files_found: int = 0
    incomplete: int = 0
    topics: int = 0


class StorageService:
    def __init__(self, repos: Repos, gateway: TelegramGateway):
        self.repos, self.gateway = repos, gateway

    def ref(self, storage_id: int) -> StorageRef:
        r = self.repos.storages.get(storage_id)
        if r is None:
            raise KeyError(storage_id)
        return StorageRef(r["tg_chat_id"], r["tg_access_hash"], r["title"], r["uuid"])

    async def create_storage(self, name: str) -> int:
        name = " ".join(name.split())
        if not name:
            raise ValueError("storage name is empty")
        title = name if name.startswith(STORAGE_PREFIX) else f"{STORAGE_PREFIX}{name}"
        ref = await self.gateway.create_storage(title, str(uuidlib.uuid4()))
        return self.repos.storages.add(ref.uuid, ref.title, ref.chat_id, ref.access_hash)

    async def discover(self) -> list[int]:
        """Register every Teloude storage visible to this Telegram account. Returns *new* storage ids."""
        new_ids: list[int] = []
        for ref in await self.gateway.list_storages():
            if self.repos.storages.by_chat(ref.chat_id) is None:
                new_ids.append(self.repos.storages.add(ref.uuid, ref.title, ref.chat_id, ref.access_hash))
        return new_ids

    async def sync_storage(self, storage_id: int, on_progress: Callable[[int], None] | None = None) -> SyncReport:
        """Rebuild the file index of a storage from topic names + message captions."""
        ref = self.ref(storage_id)
        report = SyncReport(storage_id)
        topics = await self.gateway.list_topics(ref)
        report.topics = len(topics)
        groups: dict[tuple[str, str], dict[int, tuple[VolumeMeta, int, int, int]]] = {}
        for topic in topics:
            folder = self.repos.folders.ensure(storage_id, "/".join(p.strip() for p in topic.title.split(" / ") if p.strip()) or topic.title)
            if folder.tg_topic_id is None:
                self.repos.folders.set_topic(folder.id, topic.id)
            async for msg in self.gateway.iter_messages(ref, topic.id):
                meta = decode_caption(msg.caption)
                if meta is None:
                    continue
                groups.setdefault((meta.rel_path, meta.upload_uid), {})[meta.volume_index] = (meta, msg.message_id, topic.id, msg.size)
                if on_progress:
                    on_progress(len(groups))
        latest: dict[str, tuple[int, dict[int, tuple[VolumeMeta, int, int, int]]]] = {}
        for (rel, _uid), vols in groups.items():
            any_meta = next(iter(vols.values()))[0]
            if rel not in latest or any_meta.version > latest[rel][0]:
                latest[rel] = (any_meta.version, vols)
        for rel, (version, vols) in latest.items():
            first = next(iter(vols.values()))[0]
            if len(vols) != first.volumes_total or set(vols) != set(range(first.volumes_total)):
                report.incomplete += 1
                continue
            folder = self.repos.folders.ensure(storage_id, posixpath.dirname(rel))
            records = [(i, first.volumes_total, vols[i][1], vols[i][2], vols[i][3]) for i in sorted(vols)]
            if folder.tg_topic_id is None:
                self.repos.folders.set_topic(folder.id, records[0][3] or 0)
            self.repos.files.add_remote(storage_id, folder.id, rel, first.size, first.mtime_ns, first.sha256, version, records)
            report.files_found += 1
        self.repos.storages.touch_synced(storage_id)
        return report

    async def delete_cloud_files(self, file_ids: list[int], confirmed: bool) -> int:
        """Delete backed-up files from TELEGRAM only. Requires an explicit confirmation flag; local
        files are never touched."""
        if not confirmed:
            raise PermissionError("cloud deletion requires explicit confirmation")
        deleted = 0
        for fid in file_ids:
            row = self.repos.files.get(fid)
            if row is None or row["state"] != FileState.BACKED_UP:
                continue
            ids = [int(m["message_id"]) for m in self.repos.messages.for_file(fid)]
            await self.gateway.delete_messages(self.ref(row["storage_id"]), ids)
            self.repos.files.mark_remote_deleted(fid)
            deleted += 1
        return deleted

    def forget_storage(self, storage_id: int) -> None:
        """Remove this PC's record only. The Telegram group, topics and files stay untouched."""
        self.repos.storages.forget(storage_id)
