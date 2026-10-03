# Architecture
```
app/domain          pure logic: models, errors, paths (restore safety), ratelimit (async token bucket), retry (scoped Backoff), progress, planning (volumes, RangeSet), redact
app/application     use cases + ports: scanner, pipeline (pipelined upload), backup, restore, storage_service (create/discover/sync), recovery, search, preview, settings, notifications, metadata
app/infrastructure  sqlite (db, migrations, repositories), telethon_gateway, DPAPI secrets, safe JSON logging, config
app/presentation    PySide6: controller (Qt signals <-> asyncio thread), pages, login, proxy_ui (indicator + sheet), dialogs, tray, theme
app/testing         FakeGateway (in-memory transport shared by tests and benchmarks)
```
* Telegram is behind `TelegramGateway`; Telethon is one adapter. Everything else is tested against the fake.
* Upload engine: N workers pull part indexes (bounded memory + in-flight), parts go out of order, progress/checkpoint count only acknowledged parts.
  Observers (progress, DB checkpoint) run off the hot path via coalesced snapshots + threads. Limiter reserves tokens and sleeps per caller (no serialisation).
* Retry: one `Backoff` per part (no cross-failure accumulation), single-flight reconnect, FloodWait honoured separately, `UploadSessionInvalid` -> new session (the only restart-from-zero case).
* A file becomes BACKED_UP only inside the transaction that records every volume's message id. A crash between Telegram accepting the message and that transaction is
  repaired by `find_volume` (adopt, never duplicate).
* Topic title = `Root / Sub / Leaf`; each caption carries `teloude:v1` JSON (path, sha256, size, mtime, volume, version) so a fresh PC can rebuild its index.
* Proxy: one shared client, rebuilt on change, so auth/upload/download/search all use it. Proxy sheet is an in-window overlay painted with QPainter (no QGraphicsEffect, no translucent top-level window).
