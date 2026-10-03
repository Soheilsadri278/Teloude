# Implementation plan (as executed)
1 domain + DB + repositories -> 2 scanner/pipeline/backup/restore/recovery against FakeGateway -> 3 Telethon adapter -> 4 PySide6 UI + tray + proxy -> 5 tests (unit, transfer, integration, UI, regression) -> 6 packaging scripts -> 7 benchmarks/docs.
Remaining work needs Windows and a real Telegram account: see docs/VERIFICATION.md.
