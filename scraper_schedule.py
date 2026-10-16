"""Pure scheduling policy shared by the lightweight check and results scraper.

Kickoffs are published in Madrid local time. All comparisons, persisted retry
timestamps and elapsed durations use UTC, including across midnight and DST.
Unknown kickoff times are left for the twice-daily general scrape.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
ACTIVE_STATES = frozenset({"proximo", "en_curso", "esperando_resultado"})


def utc_now() -> datetime:
    return datetime.now(UTC)


def _local_to_utc(value: datetime) -> datetime | None:
    # fold=0 chooses the first occurrence of an ambiguous autumn clock time.
    localized = value.replace(tzinfo=MADRID, fold=0)
    result = localized.astimezone(UTC)
    # A spring clock time which never existed must not invent a kickoff.
    if result.astimezone(MADRID).replace(tzinfo=None) != value:
        return None
    return result


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """Normalize ISO timestamps; legacy naive values represent Madrid time."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return _local_to_utc(value)
    return value.astimezone(UTC)


def kickoff_at(match: dict) -> datetime | None:
    """Parse a real D/M/YYYY + H:MM[:SS] kickoff without guessing missing data."""
    date = match.get("fecha")
    hour = match.get("hora")
    if not isinstance(date, str) or not isinstance(hour, str):
        return None
    date = date.strip()
    hour = hour.strip()
    if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", date):
        return None
    if not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", hour):
        return None
    try:
        day, month, year = map(int, date.split("/"))
        parts = list(map(int, hour.split(":")))
        local = datetime(year, month, day, parts[0], parts[1], parts[2] if len(parts) == 3 else 0)
    except ValueError:
        return None
    return _local_to_utc(local)


def _positive_number(value: object, default: float) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else default
    except (TypeError, ValueError):
        return default


def _eligible(match: dict, cfg: dict) -> bool:
    if match.get("es_resultado") is not False or match.get("estado") not in ACTIVE_STATES:
        return False
    if not isinstance(match.get("match_key"), str) or not match["match_key"].strip():
        return False
    if cfg.get("watch_all_clubs", False):
        return True
    team_words = str(cfg.get("team_name") or "").upper().split()
    if not team_words:
        return False
    return any(team_words[0] in str(match.get(side) or "").upper() for side in ("local", "visitante"))


def _retry_state(retry: dict | None, first_due: datetime) -> tuple[int, datetime | None]:
    retry = retry or {}
    last = parse_timestamp(retry.get("ultimo"))
    # A changed kickoff or malformed legacy state starts a fresh budget.
    if last is None or last < first_due:
        return 0, None
    try:
        count = max(0, int(retry.get("intentos", retry.get("intento", 0))))
    except (TypeError, ValueError):
        count = 0
    return count, last


def _first_due(match: dict, cfg: dict) -> datetime | None:
    kickoff = kickoff_at(match)
    if kickoff is None:
        return None
    return kickoff + timedelta(hours=_positive_number(cfg.get("match_duration_hours"), 1))


def _deadline(first_due: datetime, cfg: dict) -> datetime:
    return first_due + timedelta(hours=_positive_number(cfg.get("retry_window_hours"), 24))


def retry_due_at(match: dict, retry: dict | None, cfg: dict) -> datetime | None:
    """Return the next useful targeted query, or None when its budget is over.

Intervals start at the last actual request so delayed CI runs never cause a
catch-up burst. Technical failures may persist count=0 with a timestamp: those
requests receive a cooldown while keeping the full result-attempt budget.
"""
    if not _eligible(match, cfg):
        return None
    first_due = _first_due(match, cfg)
    if first_due is None:
        return None
    count, last = _retry_state(retry, first_due)
    max_attempts = int(_positive_number(cfg.get("max_retry_attempts"), 11))
    if count >= max_attempts:
        return None
    due = first_due
    if last is not None:
        minutes = _positive_number(cfg.get("retry_interval_minutes"), 10)
        intervals = cfg.get("retry_intervals_minutes")
        if count > 0 and isinstance(intervals, (list, tuple)) and intervals:
            minutes = _positive_number(intervals[min(count - 1, len(intervals) - 1)], minutes)
        # Small CI setup/jitter tolerance lets the next 10-minute tick run even
        # when the previous source read finished a minute or two after its tick.
        # Never advance the first query or turn repeated manual runs into a burst.
        grace = min(_positive_number(cfg.get("retry_poll_grace_seconds"), 0), 180)
        seconds = minutes * 60
        delay = max(seconds - grace, min(seconds, 300))
        due = max(first_due, last + timedelta(seconds=delay))
    return due if due <= _deadline(first_due, cfg) else None


def pending_matches(
    matches: list[dict], retries: dict[str, dict], cfg: dict, now: datetime | None = None
) -> list[dict]:
    """Keep due matches and their original fields, adding scheduling metadata."""
    current = utc_now() if now is None else parse_timestamp(now)
    if current is None:
        raise ValueError("now debe ser una fecha válida")
    pending = []
    for match in matches:
        retry = retries.get(match.get("match_key"), {})
        due = retry_due_at(match, retry, cfg)
        if due is None or due > current:
            continue
        first_due = _first_due(match, cfg)
        if current > _deadline(first_due, cfg):
            continue
        count, _ = _retry_state(retry, first_due)
        pending.append({
            **match,
            "dt_inicio": kickoff_at(match),
            "intento": count + 1,
            "pid": match["match_key"],
            "next_check_at": due,
        })
    return pending
