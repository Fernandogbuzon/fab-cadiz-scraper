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
