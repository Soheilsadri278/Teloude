# Verification status (honest)
Environment used: Linux sandbox, Python 3.11 (project targets 3.12+; code is 3.11-compatible), PySide6 6.11 offscreen, Telethon 1.45. No Windows, no Telegram account/credentials.

| Subsystem | Implemented | Tested here | Not yet verified / blocked |
|---|---|---|---|
| Scanner, hashing, change detection | yes | yes (unit) | huge folders (1M files) |
| SQLite, migrations, WAL, corruption quarantine | yes | yes | |
| Pipelined upload, bounded concurrency, ordering | yes | yes (fake transport) | real Telegram throughput/flood behaviour |
| Retry/backoff/reconnect/resume/session loss | yes | yes (fault injection) | real network drops |
| Failure state machine (retrying/paused/failed, resume from checkpoint, no duplicate records) | yes | yes (12 scenarios) | real network drops |
| Live progress (overall + per-file, monotonic across RECONNECTING/RETRYING) | yes | yes (Qt-level, 6 tests) | real Windows compositing |
| Speed limiter | yes | yes | |
| Crash recovery + adopt-after-crash | yes | yes (simulated process death) | real power loss |
| Duplicates, versions, cancel, pause | yes | yes | |
| Restore (traversal, overwrite decision, verify, resume) | yes | yes | |
| Discovery on another PC | yes | yes (fake) | live `iter_dialogs`/topics paging |
| Search / preview | yes | yes (PNG, text) | RAW (heuristic, untested on real RAW), PDF text only, video thumb only if Telegram supplies one |
| Telethon adapter | yes | request construction + error mapping with a recording client | **any live Telegram call: login, forum creation, topic creation, SendMedia, download** |
| MTProto proxy | yes (plain + dd) | secret parsing, wiring | live proxy; `ee` fake-TLS is unsupported by Telethon and rejected explicitly |
| UI (login, pages, tray logic, dialogs, proxy sheet) | yes | offscreen render + layout regression tests on all six pages | real Windows compositing, real tray, toast notifications |
| Login UX (phone → code → 2FA; credentials step only when needed) | yes | yes (3 Qt tests, bundled vs. source build) | live Telegram login |
| DPAPI secret store | yes | Windows-only test (skipped on Linux) | run on Windows |
| Portable mode (`portable.ini`, `<exe dir>\data`) | yes | yes (frozen-app tests + `portable_verify.py` 9/9) | real Windows build/EXE execution |
| Bundled API credentials | yes | yes (env > bundled > stored, redaction, git-ignored) | real Telegram login with them |
| Windows AppUserModelID (icon not grouped under python.exe) | yes | code + static check; no-op off Windows | taskbar pinning on Windows |
| PyInstaller / Inno Setup | scripts written | static regression tests on spec/iss/ico + `packaging_check.py` (26/26) | **never built or run; needs Windows** (`scripts/windows_verify.py`) |

## Windows-portability fixes (found by running the suite on Windows)
| Symptom | Cause | Fix |
|---|---|---|
| `KeyError: 'Portraits/2026/p1.jpg'` | the test helper keyed local files with `str(Path.relative_to(...))` → backslashes on Windows, while Teloude's logical paths are always `/` | `tree()` now uses `as_posix()`; a test pins that contract on every platform |
| local-file list looked "modified" after a backup | the same separator mismatch: `k != "keep/2.txt"` never matched `keep\2.txt`, so the deleted file stayed in the expected dict | same fix (the app never deletes or rewrites local files - verified, no code change needed) |
| 512 KiB at 1 MiB/s took 4.0 s instead of ~0.5 s | the limiter slept once per 1 KiB part; Windows' event-loop clock ticks every ~15.6 ms, so each sub-millisecond sleep cost a whole tick | `TokenBucket` now banks waits below `min_sleep` (20 ms) instead of sleeping for them: same global rate, ~16 sleeps instead of 512 |

Measured with a harness that quantises the clock/sleep to 15.625 ms ticks (the Windows default): the
speed-limit test went from **3.998 s to 0.658 s**, and the whole suite passes with that harness loaded.
The per-part behaviour is pinned without any wall clock by
`test_limiter_coalesces_micro_sleeps_on_a_coarse_clock` (virtual coarse clock, 512 acquires).

## What was measured, not assumed
* **Progress semantics:** with 1 KiB parts the pipeline emits `(0, total) … (len(data), total)`; the per-file bar never jumps
  back to 0 when a run goes through RECONNECTING/RETRYING. Both are asserted in `tests/ui/test_backups_page.py`.
* **Resume:** after a mid-file failure the next run uploads only the remaining 20 parts, creates no duplicate Telegram message
  and leaves local files untouched (`tests/transfer/test_failure_resume.py`).
* **Layout:** 0 clipped/overlapping/squeezed widgets across all six pages at the minimum window size (670×600 viewport).
* **Packaging:** `scripts/packaging_check.py` → 23/23. `scripts/portable_verify.py` → 9/9 on a portable tree in both
  PyInstaller layouts, and 7/9 + exit 1 when a `data/teloude.db` is planted (the negative test).

## Branding: where every icon comes from

One rule: **`Logo&icon/` is the only source of truth.** `scripts/make_icon.py` picks the highest-resolution
PNG in that folder (mtime breaks a tie), normalises it to 256 px and writes `assets/logo.png`,
`assets/icon.png` and `assets/icon.ico` (16/24/32/48/64/128/256, all embedded). The PyInstaller spec,
the Inno Setup wizard, both shortcuts, the Qt window, the tray and the toasts all take their icon from
those files, so replacing the artwork is a two-command job:

```powershell
# put the new PNG in Logo&icon\ (or overwrite icon-2.png), then:
python scripts\make_icon.py     # prints which file it used
python scripts\icon_report.py   # deviation 0.0 = carries it, exit 1 = still stale
```

`make_icon.py` used to look for `icon-2.png` by name, so artwork saved under any other name was silently
ignored and the old icon was rebuilt every time — the "the icons never change" symptom.

Enforced, not assumed:

* `scripts/packaging_check.py` → *every shipped icon shows the master artwork*: compares the pixels of
  `logo.png`, `icon.png` and every embedded `.ico` size against the master (worst deviation **0.0** today).
* `tests/ui/test_app_ui.py::test_window_icon_shows_the_master_artwork` renders the real `QIcon` at 64 px
  and compares it with the master artwork, so the window/taskbar/tray icon cannot fall back unnoticed.
* `scripts/windows_verify.py` → *exe icon matches the master artwork*: asks Windows for the icon of the
  built `Teloude.exe` and compares that too.

**The EXE, Start Menu and Desktop icons only change after a rebuild**, and Windows caches icons per file
path, so an old icon can survive a correct rebuild:

```powershell
powershell -File scripts\build_windows.ps1   # runs make_icon.py itself
ie4uinit.exe -ClearIconCache                 # then restart Explorer
```

Running from source (`python -m app`) needs no rebuild: the window and tray read `assets/icon.ico`, and
`SetCurrentProcessExplicitAppUserModelID` pins the taskbar entry to Teloude instead of python.exe.

## Not verified here (needs Windows and/or a Telegram account)
* Building `Teloude.exe`, the Inno Setup installer, or the portable ZIP.
* Reading the EXE's icon resource, taskbar grouping, tray icon, toasts.
* Any live Telegram operation, and DPAPI at runtime.

Known limitations: files are uploaded one at a time (parts are pipelined; many tiny files are slower), small files <=10 MiB are not resumable across restarts (cheap to resend),
Telegram does not document upload-session retention, so resume after long gaps is attempted and falls back to a new session if rejected.
A copied portable folder starts signed out by design (DPAPI is bound to the Windows user) — that is a security property, not a defect.
