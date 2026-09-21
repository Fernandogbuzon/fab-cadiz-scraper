"""Shared helpers for the FAB Cadiz scrapers.

Keep these functions pure: they are used by both the full calendar scraper and
the targeted results scraper, so changes here affect the DB matching contract.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def fecha_key(fecha: str) -> str:
    """Return DDMMYYYY with zero-padded day/month when possible."""
    parts = (fecha or "").split("/")
    if len(parts) == 3:
        return f"{parts[0].zfill(2)}{parts[1].zfill(2)}{parts[2]}"
    return (fecha or "").replace("/", "")


def legacy_fecha_key(fecha: str) -> str:
    """Existing match_key date component. Do not change without migration."""
    return (fecha or "").replace("/", "")


def fecha_iso(fecha: str) -> str:
    """Convert DD/MM/YYYY or D/M/YYYY to YYYY-MM-DD. Return '' if invalid."""
    parts = (fecha or "").split("/")
    if len(parts) != 3:
        return ""
    try:
        d, m, y = (int(parts[0]), int(parts[1]), int(parts[2]))
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except Exception:
        return ""


def equipos_key(local: str, visitante: str) -> str:
    return "_".join(sorted([slugify(local), slugify(visitante)]))


def build_match_key(fecha: str, local: str, visitante: str, categoria: str) -> str:
    """Current DB key format, intentionally preserved for compatibility."""
    return f"{legacy_fecha_key(fecha)}_{equipos_key(local, visitante)}_{slugify(categoria)}"


def build_stable_match_key(
    competition_slug: str,
    category: str,
    phase: str,
    group: str,
    jornada: str,
    local: str,
    visitante: str,
) -> str:
    """Date-independent key for future migrations and cross-app reconciliation."""
    parts = [
        slugify(competition_slug),
        slugify(category),
        slugify(phase),
        slugify(group),
        slugify(jornada),
        equipos_key(local, visitante),
    ]
    return "_".join(p for p in parts if p)


def competition_storage_slug(base_slug: str, season: str) -> str:
    """Identidad de una competición por temporada, sin reutilizar la histórica."""
    if not re.fullmatch(r"\d{4}-\d{4}", season):
        raise ValueError("season debe tener formato YYYY-YYYY")
    start, end = map(int, season.split("-"))
    if end != start + 1:
        raise ValueError("season debe abarcar dos años consecutivos")
    return f"{slugify(base_slug)}-{season}"


def seasonal_match_key(
    season: str, competition_slug: str, fecha: str, local: str, visitante: str, categoria: str
) -> str:
    """Evita que un partido nuevo sobrescriba otro de una temporada anterior."""
    return f"{season}_{slugify(competition_slug)}_{build_match_key(fecha, local, visitante, categoria)}"


def build_lookup_key(fecha: str, local: str, visitante: str) -> str:
    """Canonical lookup key for matching web rows to DB rows."""
    return f"{fecha_key(fecha)}_{equipos_key(local, visitante)}"


def parse_fecha_hora(fecha: str, hora: str = "") -> datetime:
    """Parse scraper date/time into a naive local datetime."""
    try:
        d, m, y = fecha.split("/")
        dt = datetime(int(y), int(m), int(d))
        if hora and ":" in hora:
            h, mi = hora.split(":")[:2]
            return dt.replace(hour=int(h), minute=int(mi))
        return dt.replace(hour=12, minute=0)
    except Exception:
        return datetime(2000, 1, 1)
