#!/usr/bin/env python3
"""
Upload team logos to the Central Supabase Storage bucket `team-logos`.

The hosted Supabase MCP server has no storage:write scope, so the file BYTES must
be pushed with the service_role key. This script does that with stdlib only.

Credentials (in priority order, the secret never needs to be typed in chat):
  1. env vars CENTRAL_SUPABASE_URL / CENTRAL_SUPABASE_SERVICE_KEY, else
  2. parsed from C:/Users/ferna/adesa80-web/.env (CENTRAL_SUPABASE_* keys).

Usage (PowerShell):
  python scripts/upload_logos.py            # upload all logos referenced by clubs.logo_url
  python scripts/upload_logos.py --dry-run  # list what would upload, no network

Idempotent: uses x-upsert so re-runs overwrite.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import urllib.request
from pathlib import Path

LOGO_DIR = Path(r"C:/Users/ferna/adesa80-web/public/logos")
WEB_ENV = Path(r"C:/Users/ferna/adesa80-web/.env")
BUCKET = "team-logos"
DEFAULT_URL = "https://dzpkmkqmtdzwmrynzgus.supabase.co"

# Exact files referenced by the seed (clubs.logo_url). Upload only these.
FILES = [
    "ubjerez.webp", "gymnastica.webp", "cbportuense.webp", "cbsanfernando.webp",
    "sanroque.webp", "adesa80.webp", "cbgades.webp", "cbciudaddecadiz.webp",
    "admarianistas.webp", "cdmergablo.webp", "chipionacb.webp", "puertoreal.webp",
    "cbmedinasidonia.webp", "candray.webp", "cabu.webp", "cbcimbis.webp",
    "urbaluz.webp", "sanfelipeneri.webp", "ADCarteia.webp", "cdudeaalgeciras.webp",
    "cbalgeciras.webp", "arcos.webp", "cbchiclana.webp", "alcaladelosgazules.webp",
    "cbrota.webp", "losbarrios.webp", "cpdonbosco.webp", "ebmvejer.webp",
    "xerezcd.webp", "ulblinense.webp", "cdbarbate.webp", "cbpradodelrey.webp",
    "sanctipetri.webp", "cdmontera.webp", "adcalgaida.webp", "Gabba.webp",
    "cbtrebujena.svg", "cbinmaculadaceuta.svg",
]


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_credentials() -> tuple[str, str]:
    url = os.environ.get("CENTRAL_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    key = os.environ.get("CENTRAL_SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        env = load_env_file(WEB_ENV)
        url = url or env.get("CENTRAL_SUPABASE_URL") or env.get("SUPABASE_URL")
        key = key or env.get("CENTRAL_SUPABASE_SERVICE_KEY") or env.get("SUPABASE_SERVICE_KEY")
    return (url or DEFAULT_URL).rstrip("/"), (key or "")


def content_type(fname: str) -> str:
    if fname.lower().endswith(".webp"):
        return "image/webp"
    if fname.lower().endswith(".svg"):
        return "image/svg+xml"
    return mimetypes.guess_type(fname)[0] or "application/octet-stream"


def upload(url: str, key: str, fname: str) -> tuple[bool, str]:
    src = LOGO_DIR / fname
    if not src.exists():
        return False, "missing local file"
    data = src.read_bytes()
    endpoint = f"{url}/storage/v1/object/{BUCKET}/{fname}"
    req = urllib.request.Request(endpoint, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("apikey", key)
    req.add_header("Content-Type", content_type(fname))
    req.add_header("x-upsert", "true")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url, key = get_credentials()
    print(f"Bucket : {url}/storage/v1/object/public/{BUCKET}/")
    print(f"Source : {LOGO_DIR}")
    print(f"Files  : {len(FILES)}")
    if args.dry_run:
        for f in FILES:
            exists = (LOGO_DIR / f).exists()
            print(f"  {'ok ' if exists else 'MISS'} {f}")
        return 0
    if not key:
        print("\nERROR: no service key. Set CENTRAL_SUPABASE_SERVICE_KEY or ensure it is in "
              f"{WEB_ENV}", file=sys.stderr)
        return 2

    ok = 0
    for f in FILES:
        success, msg = upload(url, key, f)
        print(f"  {'OK  ' if success else 'FAIL'} {f}  ({msg})")
        ok += int(success)
    print(f"\n{ok}/{len(FILES)} uploaded.")
    return 0 if ok == len(FILES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
