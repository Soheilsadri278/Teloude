# Requirements (condensed)
Windows-first backup (not sync) to the user's own Telegram account via MTProto/Telethon. Private forum-supergroup storages, topics = folders,
hierarchy in SQLite. Scan -> compare -> duplicate decision (Skip / Upload again / Cancel, apply to all) -> pipelined resumable upload -> record mapping.
Large files split into volumes by dynamically discovered Telegram limits. Pause/cancel/retry/reconnect/resume/crash recovery. Async token-bucket speed limit
(Unlimited/10/5/2/custom). Restore with path-traversal protection and explicit overwrite decisions. Global search, previews, global MTProto proxy with persistent
indicator + sheet, tray, notifications, DPAPI-protected secrets, redacted structured logs, PyInstaller + Inno Setup packaging.
Safety rules: never delete local files, never overwrite silently, cloud deletion needs explicit confirmation, never log secrets, never report success for partial/cancelled work.
