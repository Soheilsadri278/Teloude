"""Inject (or remove) the Telegram application credentials a release build ships.

    python scripts/inject_credentials.py --api-id 123456 --api-hash 0123456789abcdef...
    python scripts/inject_credentials.py --clear
    python scripts/inject_credentials.py --show          # prints only whether they are present

This writes ``app/teloude_api.json``, which is read by ``app.infrastructure.build_config`` and
bundled into the EXE by ``installer/teloude.spec``. With it in place the login screen goes straight
to phone -> code -> 2FA.

Security notes
--------------
* The file is listed in ``.gitignore`` and must never be committed.
* Writing it does NOT weaken the Telegram *session*: that is still stored DPAPI-encrypted and bound
  to the Windows user, exactly as before.
* Anyone with the built application can extract the file. Use credentials created for Teloude and
  accept that they are public inside your release - that is the normal trade-off for shipping a
  ready-to-use Telegram client. If you cannot accept it, do not inject them: users can still enter
  their own on the login screen, and ``TELOUDE_API_ID`` / ``TELOUDE_API_HASH`` still work for devs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "app" / "teloude_api.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-id", help="Telegram API ID (numeric, from my.telegram.org/apps)")
    ap.add_argument("--api-hash", help="Telegram API hash (32 hex characters)")
    ap.add_argument("--clear", action="store_true", help="delete the bundled credentials file")
    ap.add_argument("--show", action="store_true", help="report whether credentials are bundled")
    a = ap.parse_args()

    if a.show:
        print(f"{'present' if OUT.exists() else 'absent'}: {OUT}")
        return 0 if OUT.exists() else 1
    if a.clear:
        OUT.unlink(missing_ok=True)
        print("removed", OUT)
        return 0
    if not a.api_id or not a.api_hash:
        ap.error("--api-id and --api-hash are required (or use --clear / --show)")
        return 2
    try:
        api_id = int(a.api_id.strip())
    except ValueError:
        ap.error("API ID must be a number")
        return 2
    api_hash = a.api_hash.strip()
    if api_id <= 0 or not (20 <= len(api_hash) <= 64):
        ap.error("that does not look like a Telegram API id/hash pair")
        return 2
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}, indent=2) + "\n", encoding="utf-8")
    try:  # belt and braces on developer machines
        OUT.chmod(0o600)
    except OSError:
        pass
    print(f"wrote {OUT} (keep this file out of git: it is already in .gitignore)")
    print("Rebuild the EXE so the credentials are bundled: powershell -File scripts\\build_windows.ps1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
