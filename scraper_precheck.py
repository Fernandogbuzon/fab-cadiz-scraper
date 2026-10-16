"""Lightweight, paginated result detection; requires only the Python stdlib.

The workflow and the browser scraper use the same snapshot and scheduling policy.
No credentials are written to the snapshot or logs.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scraper_schedule import MADRID, parse_timestamp, pending_matches, utc_now

SCRIPT_DIR = Path(__file__).resolve().parent
PAGE_SIZE = 500
MATCH_SELECT = (
    "match_key,fecha,hora,local,visitante,estado,es_resultado,group_id,competition_id,"
    "competitions!inner(season),"
    "comp_groups!inner(phase,name,comp_categories!inner(slug,competitions!inner(slug,url)))"
)


def rest_rows(table: str, params: dict[str, str]) -> list[dict]:
    """Read every page with a stable ordering, rather than silently stopping at 1000."""
    base = (os.environ.get("CENTRAL_SUPABASE_URL") or os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("CENTRAL_SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not base or not key:
        raise RuntimeError("Supabase no configurado para detectar resultados")
    rows = []
    offset = 0
    while True:
        query = urlencode({**params, "order": "match_key.asc", "limit": str(PAGE_SIZE), "offset": str(offset)})
        request = Request(
            f"{base.rstrip('/')}/rest/v1/{table}?{query}",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Prefer": "count=exact"},
        )
        with urlopen(request, timeout=20) as response:
            page = json.load(response)
            content_range = response.headers.get("Content-Range", "")
        if not isinstance(page, list):
            raise RuntimeError(f"Respuesta inesperada al consultar {table}")
        rows.extend(page)
        offset += len(page)
        total = content_range.rsplit("/", 1)[-1]
        if not page or (total.isdigit() and offset >= int(total)):
            return rows
        # Without a total, an empty page still terminates even if the API's
        # configured maximum is smaller than our requested page size.


def fetch_context(cfg: dict, now=None) -> tuple[list[dict], dict]:
    now = parse_timestamp(now) if now is not None else utc_now()
    if now is None:
        raise ValueError("now debe ser una fecha válida")
    local_now = now.astimezone(MADRID)
    lookback = timedelta(hours=cfg.get("retry_window_hours", 24) + cfg.get("match_duration_hours", 1))
    params = {
        "select": MATCH_SELECT,
        "es_resultado": "eq.false",
        "estado": "in.(proximo,en_curso,esperando_resultado)",
        "competitions.season": f"eq.{cfg['season']}",
        "and": f"(fecha_iso.gte.{(now - lookback).astimezone(MADRID).date().isoformat()},fecha_iso.lte.{local_now.date().isoformat()})",
    }
    if not cfg.get("watch_all_clubs", False):
        team = cfg["team_name"].split()[0]
        params["or"] = f"(local.ilike.*{team}*,visitante.ilike.*{team}*)"
    matches = rest_rows("matches", params)
    if not matches:
        return [], {}
    retries = {}
    keys = sorted({match["match_key"] for match in matches})
    # Bound URL length; seasonal keys can be several hundred characters long.
    for index in range(0, len(keys), 20):
        quoted = [json.dumps(key, ensure_ascii=False) for key in keys[index:index + 20]]
        for row in rest_rows("scraper_retries", {
            "select": "match_key,intento,ultimo", "match_key": f"in.({','.join(quoted)})",
        }):
            retries[row["match_key"]] = {"intentos": row["intento"], "ultimo": row["ultimo"]}
    return matches, retries


def load_context(cfg: dict) -> tuple[list[dict], dict]:
    """Reuse the precheck while dependencies install, but never an old/local run."""
    snapshot_path = os.environ.get("SCRAPER_PENDING_FILE")
    if snapshot_path and Path(snapshot_path).exists():
        snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        created = parse_timestamp(snapshot.get("created_at"))
        if created and timedelta(0) <= utc_now() - created < timedelta(minutes=15) and snapshot.get("config") == cfg:
            return snapshot["matches"], snapshot["retries"]
    return fetch_context(cfg)


def main() -> None:
    cfg = json.loads((SCRIPT_DIR / "team_config.json").read_text(encoding="utf-8"))
    now = utc_now()
    matches, retries = fetch_context(cfg, now)
    due = pending_matches(matches, retries, cfg, now)
    print(f"Hora Madrid: {now.astimezone(MADRID):%d/%m/%Y %H:%M}; candidatos: {len(matches)}; pendientes: {len(due)}")
    for match in due:
        print(f"  Intento {match['intento']}: {match['local']} - {match['visitante']} ({match['fecha']} {match['hora']})")
    snapshot_path = os.environ.get("SCRAPER_PENDING_FILE")
    if due and snapshot_path:
        Path(snapshot_path).write_text(json.dumps({
            "created_at": now.isoformat(), "config": cfg, "matches": matches, "retries": retries,
        }, ensure_ascii=False), encoding="utf-8")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"pendientes={'true' if due else 'false'}\nnum={len(due)}\n")


if __name__ == "__main__":
    main()
