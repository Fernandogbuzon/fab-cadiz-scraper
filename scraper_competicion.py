#!/usr/bin/env python3
"""
Scraper Competiciones – Federación Andaluza de Baloncesto (Cádiz)
================================================================
Extrae calendario completo + clasificaciones de TODAS las competiciones,
categorías, fases y grupos.

Estrategia:
  • Playwright en modo headed (necesario para que Cloudflare auto-resuelva
    sus challenges).
  • stealth para enmascarar la automatización.
  • __doPostBack nativo del ASP.NET para cambiar filtros.
  • Pausas aleatorias entre interacciones.

Carpetas de salida:
  src/data/<Competición>/<Categoría>/<Grupo>/<Fase>/
    ├── equipo-1.json
    ├── equipo-2.json
    └── clasificacion.json

Uso:
  python scraper_competicion.py                                   # Todas las competiciones
  python scraper_competicion.py --competicion "copa andalucia a"   # Filtrar competición
  python scraper_competicion.py --categoria "Senior Fem"           # Filtrar categoría
  python scraper_competicion.py --watch                            # Modo cron
  python scraper_competicion.py --headless                         # Intentar headless
"""

import asyncio
import json
import re
import sys
import os
import argparse
import random
import logging
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Cargar .env desde la carpeta del script
load_dotenv(Path(__file__).parent / ".env")

# ─── Supabase ────────────────────────────────────────────────────────────────

_supabase_client = None

def get_supabase():
    """Inicializa Supabase client (lazy). Usa el proyecto CENTRAL de competiciones."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    url = os.environ.get("CENTRAL_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    key = os.environ.get("CENTRAL_SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        logger.warning("⚠️ CENTRAL_SUPABASE_URL/CENTRAL_SUPABASE_SERVICE_KEY no configuradas. Solo se guardarán JSON locales.")
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        logger.info("✅ Supabase conectado")
        return _supabase_client
    except Exception as e:
        logger.error(f"❌ Error conectando Supabase: {e}")
        return None

# ─── Configuración (desde team_config.json) ─────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "team_config.json"

def cargar_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"No se encontró {CONFIG_FILE}. "
            "Copia team_config.example.json → team_config.json y ajústalo a tu equipo."
        )
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

_CFG = cargar_config()
TEAM_NAME = _CFG["team_name"]
TEAM_SLUG = _CFG["team_slug"]
COMPETICIONES = _CFG["competitions"]
PLAYOFF_FORMATS = _CFG.get("playoff_formats", [])


def get_series_format(comp_slug: str, cat_slug: str, fase_slug: str) -> dict:
    """
    Retorna el formato de serie (series_games, tiebreak) para una combinación
    competicion/categoría/fase, según lo definido en team_config.json.
    Fallback: {series_games: 3, tiebreak: None} (mejor de 3, sin desempate).
    """
    is_senior = "senior" in cat_slug.lower()
    for fmt in PLAYOFF_FORMATS:
        # Comprobar que aplica a esta competición (lista vacía = default, no match here)
        comp_list = fmt.get("competitions", [])
        if comp_list and comp_slug not in comp_list:
            continue
        # Comprobar que aplica a esta categoría
        cats = fmt.get("categories", "all")
        if cats == "non-senior" and is_senior:
            continue
        if cats == "senior" and not is_senior:
            continue
        if isinstance(cats, list) and cat_slug not in cats:
            continue
        # Buscar la fase específica o el default del formato
        phases = fmt.get("phases", {})
        phase_cfg = phases.get(fase_slug) or phases.get("_default")
        if phase_cfg:
            return phase_cfg
    # Fallback: mejor de 3
    return {"series_games": 3, "tiebreak": None}

# ASP.NET dropdown names (para __doPostBack)
DDL_CATEGORIAS = "ctl00$ctl00$contenedor_informacion$contenedor_informacion_con_lateral$DDLCategorias"
DDL_FASES = "ctl00$ctl00$contenedor_informacion$contenedor_informacion_con_lateral$DDLFases"
DDL_GRUPOS = "ctl00$ctl00$contenedor_informacion$contenedor_informacion_con_lateral$DDLGrupos"

# CSS selectors
SEL_CAT = f"select[name='{DDL_CATEGORIAS}']"
SEL_FASE = f"select[name='{DDL_FASES}']"
SEL_GRUPO = f"select[name='{DDL_GRUPOS}']"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scraper_competicion.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ─── Utilidades ──────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text


def normalizar_carpeta(nombre: str) -> str:
    nombre = re.sub(r"\s+", " ", nombre).strip()
    nombre = re.sub(r"\s", "-", nombre)
    # Eliminar puntos finales (invalido en Windows y problematico para git)
    nombre = nombre.rstrip(".")
    return nombre


def generar_id(fecha: str, local: str, visitante: str, categoria: str) -> str:
    return slugify(f"{fecha}_{local}_{visitante}_{categoria}")


def _fecha_sort(f: str) -> str:
    try:
        p = f.split("/")
        return f"{p[2]}{p[1]}{p[0]}" if len(p) == 3 else "00000000"
    except Exception:
        return "00000000"


async def pausa(lo: float = 0.8, hi: float = 2.5):
    await asyncio.sleep(random.uniform(lo, hi))


# ─── Browser helpers ─────────────────────────────────────────────────────────

async def crear_browser(headless: bool = False):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    stealth = Stealth()
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1366, "height": 768},
        locale="es-ES",
        timezone_id="Europe/Madrid",
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    await stealth.apply_stealth_async(context)
    page = await context.new_page()
    return pw, browser, context, page


async def esperar_pagina(page, timeout: int = 60000) -> bool:
    """Espera a que la página real cargue (selector de categoría visible)."""
    try:
        await page.wait_for_selector(SEL_CAT, timeout=timeout, state="visible")
        await asyncio.sleep(0.5)  # Pequeña pausa adicional para asegurar estabilidad
        return True
    except Exception:
        try:
            title = await page.title()
            if "moment" in title.lower() or "momento" in title.lower():
                logger.info("  ⏳ Challenge CF detectado, esperando resolución...")
                try:
                    await page.wait_for_selector(SEL_CAT, timeout=120000, state="visible")
                    await asyncio.sleep(1.0)  # Pausa adicional tras resolver CF
                    return True
                except Exception:
                    logger.error("  ❌ CF challenge no se resolvió")
                    return False
        except Exception:
            pass
        return False


async def obtener_opciones(page, selector: str) -> list[dict]:
    """Lee las opciones de un <select> en la página."""
    return await page.eval_on_selector_all(
        selector + " option",
        "opts => opts.map(o => ({value: o.value, text: o.textContent.trim().replace(/\\s+/g, ' ')}))",
    )


async def seleccionar_dropdown(page, selector: str, ddl_name: str, value: str, max_retries: int = 3):
    """Selecciona valor en dropdown y espera la navegación del postback ASP.NET."""
    for intento in range(max_retries):
        try:
            await page.evaluate("() => { window.__cFRLUnblockHandlers = true; }")
            
            # Esperar a que el selector esté disponible
            await page.wait_for_selector(selector, timeout=10000, state="visible")
            
            # Intentar selección con navegación
            try:
                async with page.expect_navigation(wait_until="load", timeout=90000):
                    await page.select_option(selector, value)
                await asyncio.sleep(1.0)
            except Exception as nav_error:
                # La navegación puede fallar si es muy rápida o no hay cambio real
                logger.debug(f"  Navigation event timeout (puede ser normal): {nav_error}")
                await asyncio.sleep(2.0)
            
            # Verificar que la página esté lista
            ok = await esperar_pagina(page, timeout=90000)
            if ok:
                await pausa(1.5, 3.0)  # Pausa más larga para estabilidad
                return True
            
            if intento < max_retries - 1:
                logger.warning(f"  ⚠️ Intento {intento + 1}/{max_retries} falló para {ddl_name}, reintentando...")
                await asyncio.sleep(3.0)
            else:
                logger.error(f"  ❌ Error tras {max_retries} intentos de postback de {ddl_name}")
                return False
                
        except Exception as e:
            if intento < max_retries - 1:
                logger.warning(f"  ⚠️ Error en intento {intento + 1}/{max_retries}: {e}")
                await asyncio.sleep(3.0)
            else:
                logger.error(f"  ❌ Error crítico tras {max_retries} intentos: {e}")
                return False
    
    return False


# ─── Extracción de partidos ──────────────────────────────────────────────────

async def extraer_partidos(page, categoria: str, fase: str, grupo: str, competicion_nombre: str = "") -> list[dict]:
    """Extrae todos los partidos del calendario visible."""
    return await page.evaluate("""
        (params) => {
            const { categoria, fase, grupo, competicion } = params;
            const resultados = [];

            const calendarioTab = document.getElementById('calendario');
            if (!calendarioTab) return resultados;

            const headers = calendarioTab.querySelectorAll('header.nombre_tabla');

            headers.forEach(header => {
                const h5 = header.querySelector('h5');
                const jornadaText = h5 ? h5.textContent.trim().replace(/\\s+/g, ' ') : '';

                let tc = header.nextElementSibling;
                while (tc && !tc.classList.contains('table-responsive')) tc = tc.nextElementSibling;
                if (!tc) return;

                const tabla = tc.querySelector('table');
                if (!tabla) return;

                tabla.querySelectorAll('tbody tr').forEach(fila => {
                    const c = fila.querySelectorAll('td');
                    if (c.length < 6) return;

                    const local = c[0].textContent.trim();
                    const ptL = c[1].textContent.trim();
                    const ptV = c[2].textContent.trim();
                    const visitante = c[3].textContent.trim();

                    if (!local || !visitante) return;

                    const strong = c[4].querySelector('strong');
                    let fecha = '', hora = '';
                    if (strong) {
                        const parts = strong.innerHTML.split(/<br\\s*\\/?>/);
                        fecha = (parts[0] || '').replace(/"/g, '').trim();
                        if (parts[1]) hora = parts[1].replace(/"/g, '').trim();
                    }

                    const pabellon = c[5] ? c[5].textContent.trim() : '';
                    const ml = ptL && !isNaN(parseInt(ptL)) ? parseInt(ptL) : null;
                    const mv = ptV && !isNaN(parseInt(ptV)) ? parseInt(ptV) : null;

                    resultados.push({
                        local, visitante,
                        marcador_local: ml, marcador_visitante: mv,
                        fecha, hora, pabellon,
                        es_resultado: ml !== null && mv !== null,
                        jornada: jornadaText,
                        categoria_completa: `${categoria} - ${fase} - ${grupo}`,
                        fase, grupo, competicion
                    });
                });
            });

            return resultados;
        }
    """, {
        "categoria": categoria,
        "fase": fase,
        "grupo": grupo,
        "competicion": competicion_nombre,
    })


# ─── Agrupación y clasificación ──────────────────────────────────────────────

def agrupar_por_equipo(partidos: list[dict]) -> dict[str, list[dict]]:
    equipos: dict[str, list[dict]] = {}
    for p in partidos:
        loc, vis = p["local"], p["visitante"]
        if "DESCANSA" in loc or "DESCANSA" in vis:
            continue
        base = {
            "competicion": p["competicion"],
            "marcador_local": p["marcador_local"],
            "marcador_visitante": p["marcador_visitante"],
            "fecha": p["fecha"], "hora": p["hora"],
            "pabellon": p["pabellon"],
            "es_resultado": p["es_resultado"],
            "estado": "finalizado" if p["es_resultado"] else "proximo",
            "jornada": p["jornada"],
        }
        equipos.setdefault(loc, []).append({
            **base, "categoria": p["categoria_completa"],
            "equipo": loc, "rival": vis, "ubicacion": "Local",
            "id": generar_id(p["fecha"], loc, vis, p["categoria_completa"]),
        })
        equipos.setdefault(vis, []).append({
            **base, "categoria": p["categoria_completa"],
            "equipo": vis, "rival": loc, "ubicacion": "Visitante",
            "id": generar_id(p["fecha"], loc, vis, p["categoria_completa"]),
        })
    return equipos


def calcular_clasificacion(partidos: list[dict], cat: str, fase: str, grupo: str, competicion_nombre: str = "") -> dict:
    stats: dict[str, dict] = {}
    for p in partidos:
        if not p["es_resultado"]:
            continue
        loc, vis = p["local"], p["visitante"]
        if "DESCANSA" in loc or "DESCANSA" in vis:
            continue
        ml, mv = p["marcador_local"], p["marcador_visitante"]
        if ml is None or mv is None:
            continue

        for eq in (loc, vis):
            if eq not in stats:
                stats[eq] = {"equipo": eq, "partidos_jugados": 0, "partidos_ganados": 0,
                             "partidos_perdidos": 0, "puntos_favor": 0, "puntos_contra": 0,
                             "diferencia": 0, "puntos": 0}

        stats[loc]["partidos_jugados"] += 1
        stats[loc]["puntos_favor"] += ml
        stats[loc]["puntos_contra"] += mv
        stats[vis]["partidos_jugados"] += 1
        stats[vis]["puntos_favor"] += mv
        stats[vis]["puntos_contra"] += ml

        if ml > mv:
            stats[loc]["partidos_ganados"] += 1
            stats[loc]["puntos"] += 2
            stats[vis]["partidos_perdidos"] += 1
            stats[vis]["puntos"] += 1
        elif mv > ml:
            stats[vis]["partidos_ganados"] += 1
            stats[vis]["puntos"] += 2
            stats[loc]["partidos_perdidos"] += 1
            stats[loc]["puntos"] += 1

    clasificacion = list(stats.values())
    for eq in clasificacion:
        eq["diferencia"] = eq["puntos_favor"] - eq["puntos_contra"]

    # ── Desempate por enfrentamientos directos ────────────────────────────────
    # Paso 1: ordenar por puntos para identificar grupos empatados
    clasificacion.sort(key=lambda x: (-x["puntos"], -x["diferencia"], -x["puntos_favor"]))

    # Paso 2: para cada grupo con los mismos puntos, calcular mini-tabla h2h
    i = 0
    resultado_final: list[dict] = []
    while i < len(clasificacion):
        j = i + 1
        while j < len(clasificacion) and clasificacion[j]["puntos"] == clasificacion[i]["puntos"]:
            j += 1
        grupo = clasificacion[i:j]
        if len(grupo) > 1:
            nombres_grupo = {eq["equipo"] for eq in grupo}
            # Acumular stats sólo entre equipos del grupo
            h2h: dict[str, dict] = {eq["equipo"]: {"wins": 0, "pf": 0, "pc": 0} for eq in grupo}
            for p in partidos:
                if not p["es_resultado"]:
                    continue
                loc, vis = p["local"], p["visitante"]
                if loc not in nombres_grupo or vis not in nombres_grupo:
                    continue
                ml, mv = p["marcador_local"], p["marcador_visitante"]
                if ml is None or mv is None:
                    continue
                h2h[loc]["pf"] += ml; h2h[loc]["pc"] += mv
                h2h[vis]["pf"] += mv; h2h[vis]["pc"] += ml
                if ml > mv:
                    h2h[loc]["wins"] += 1
                elif mv > ml:
                    h2h[vis]["wins"] += 1
            # Ordenar grupo: victorias h2h → diferencia h2h → diferencia global → puntos_favor
            grupo.sort(key=lambda x: (
                -h2h[x["equipo"]]["wins"],
                -(h2h[x["equipo"]]["pf"] - h2h[x["equipo"]]["pc"]),
                -x["diferencia"],
                -x["puntos_favor"],
            ))
        resultado_final.extend(grupo)
        i = j

    clasificacion = resultado_final
    for i, eq in enumerate(clasificacion, 1):
        eq["posicion"] = i

    return {
        "categoria": f"{cat} - {fase} - {grupo}",
        "competicion": competicion_nombre,
        "ultima_actualizacion": datetime.now().isoformat(),
        "clasificacion": clasificacion,
    }


# ─── Guardado Supabase ───────────────────────────────────────────────────────

def _upsert_competition(sb, comp_nombre: str, comp_carpeta: str, comp_url: str) -> str | None:
    """Asegura que la competición existe. Devuelve su UUID."""
    slug = comp_carpeta
    try:
        res = sb.table("competitions").upsert(
            {"slug": slug, "name": comp_nombre, "url": comp_url, "updated_at": datetime.now().isoformat()},
            on_conflict="slug"
        ).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        logger.error(f"  ❌ Supabase upsert competition: {e}")
        return None


def _upsert_category(sb, comp_id: str, cat_nombre: str) -> str | None:
    slug = normalizar_carpeta(cat_nombre)
    try:
        res = sb.table("comp_categories").upsert(
            {"competition_id": comp_id, "slug": slug, "name": cat_nombre},
            on_conflict="competition_id,slug"
        ).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        logger.error(f"  ❌ Supabase upsert category: {e}")
        return None


def _upsert_group(sb, cat_id: str, fase: str, grupo: str) -> str | None:
    phase = normalizar_carpeta(fase)
    name = normalizar_carpeta(grupo)
    try:
        res = sb.table("comp_groups").upsert(
            {"category_id": cat_id, "phase": phase, "name": name},
            on_conflict="category_id,phase,name"
        ).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        logger.error(f"  ❌ Supabase upsert group: {e}")
        return None


def guardar_supabase(
    partidos: list[dict], clasif: dict,
    cat: str, grupo: str, fase: str,
    comp_nombre: str, comp_carpeta: str, comp_url: str
):
    """Guarda partidos y clasificaciones en Supabase (además de JSON)."""
    sb = get_supabase()
    if not sb:
        return

    # 1. Competición
    comp_id = _upsert_competition(sb, comp_nombre, comp_carpeta, comp_url)
    if not comp_id:
        return

    # 2. Categoría
    cat_id = _upsert_category(sb, comp_id, cat)
    if not cat_id:
        return

    # 3. Grupo
    group_id = _upsert_group(sb, cat_id, fase, grupo)
    if not group_id:
        return

    # 4. Partidos – upsert por match_key (dedup por match_key para evitar error 21000)
    matches_dict = {}
    for p in partidos:
        loc = p.get("local", "").strip()
        vis = p.get("visitante", "").strip()
        if "DESCANSA" in loc or "DESCANSA" in vis:
            continue

        # match_key: fecha + equipos ordenados + categoría (dedup)
        fecha_clean = p.get("fecha", "").replace("/", "")
        equipos_sorted = "_".join(sorted([slugify(loc), slugify(vis)]))
        cat_slug = slugify(cat)
        match_key = f"{fecha_clean}_{equipos_sorted}_{cat_slug}"

        estado = "finalizado" if p.get("es_resultado") else "proximo"
        # Si ya existe, preferir la versión con resultado
        if match_key in matches_dict and not p.get("es_resultado") and matches_dict[match_key].get("es_resultado"):
            continue
        matches_dict[match_key] = {
            "match_key": match_key,
            "group_id": group_id,
            "competition_id": comp_id,
            "jornada": p.get("jornada", ""),
            "fecha": p.get("fecha", ""),
            "hora": p.get("hora", ""),
            "local": loc,
            "visitante": vis,
            "marcador_local": p.get("marcador_local"),
            "marcador_visitante": p.get("marcador_visitante"),
            "pabellon": p.get("pabellon", ""),
            "es_resultado": p.get("es_resultado", False),
            "estado": estado,
            "updated_at": datetime.now().isoformat(),
        }

    # ── Detectar series ya cerradas en fases playoff para eliminar ghost games ──
    # Se consulta el formato de serie en team_config.json (playoff_formats).
    # Para formatos "best of 2": la serie está cerrada cuando se han jugado los 2 partidos
    # (tanto si queda 2-0 como si queda 1-1 con desempate por puntos).
    # Para "best of 3" (por defecto): la serie cierra cuando un equipo acumula 2 victorias.
    fase_upper = fase.upper().replace("-", " ")
    is_playoff_phase = any(kw in fase_upper for kw in ("CUARTOS", "SEMIFINAL", "FASE FINAL", "PLAY OFF", "PLAYOFF"))
    if is_playoff_phase:
        # Obtener formato desde configuración
        comp_slug_fmt = slugify(comp_carpeta)
        cat_slug_fmt  = slugify(cat)
        fase_slug_fmt = slugify(fase)
        fmt = get_series_format(comp_slug_fmt, cat_slug_fmt, fase_slug_fmt)
        series_games = fmt.get("series_games", 3)

        # Contar victorias Y partidos jugados por emparejamiento
        series_wins: dict[str, dict[str, int]] = {}   # pair_key → {team: wins}
        series_played: dict[str, int] = {}             # pair_key → games played (with result)
        for mk, m in matches_dict.items():
            if not m.get("es_resultado"):
                continue
            ml = m.get("marcador_local")
            mv = m.get("marcador_visitante")
            if ml is None or mv is None:
                continue
            pair_key = "_".join(sorted([m["local"], m["visitante"]]))
            series_played[pair_key] = series_played.get(pair_key, 0) + 1
            if not series_wins.get(pair_key):
                series_wins[pair_key] = {}
            winner = m["local"] if ml > mv else m["visitante"] if mv > ml else None
            if winner:
                series_wins[pair_key][winner] = series_wins[pair_key].get(winner, 0) + 1

        # Eliminar ghost games (partidos sin resultado):
        # - Serie cerrada por mayoría de victorias (any wins >= ceil(series_games/2)+1)
        # - O todos los partidos del formato ya se han jugado (played >= series_games)
        majority_wins = (series_games // 2) + 1
        ghost_keys = []
        for mk, m in matches_dict.items():
            if m.get("es_resultado"):
                continue
            pair_key = "_".join(sorted([m["local"], m["visitante"]]))
            wins = series_wins.get(pair_key, {})
            played = series_played.get(pair_key, 0)
            clinched_by_wins   = any(w >= majority_wins for w in wins.values())
            all_games_played   = played >= series_games
            if clinched_by_wins or all_games_played:
                ghost_keys.append(mk)
        for gk in ghost_keys:
            del matches_dict[gk]
            logger.info(f"      🚫 Ghost game eliminado (serie cerrada): {gk}")

    matches_to_upsert = list(matches_dict.values())
    current_keys = set(matches_dict.keys())

    if matches_to_upsert:
        # Preservar estados 'terminales' (esperando_resultado, aplazado, cancelado)
        # para que el scraper completo diario no reestablezca "proximo" y cause un bucle infinito
        TERMINAL_ESTADOS = {"esperando_resultado", "aplazado", "cancelado"}
        keys_sin_resultado = [m["match_key"] for m in matches_to_upsert if not m.get("es_resultado")]
        if keys_sin_resultado:
            try:
                ex_res = sb.table("matches").select("match_key, estado").in_("match_key", keys_sin_resultado).execute()
                preserved_estados = {
                    r["match_key"]: r["estado"]
                    for r in (ex_res.data or [])
                    if r.get("estado") in TERMINAL_ESTADOS
                }
                if preserved_estados:
                    logger.info(f"      🔒 Preservando {len(preserved_estados)} estado(s) terminal(es)")
                    for mdu in matches_to_upsert:
                        if not mdu.get("es_resultado") and mdu["match_key"] in preserved_estados:
                            mdu["estado"] = preserved_estados[mdu["match_key"]]
            except Exception as e:
                logger.warning(f"      ⚠️ No se pudo preservar estados terminales: {e}")

        try:
            # Upsert en bloques de 100
            for i in range(0, len(matches_to_upsert), 100):
                batch = matches_to_upsert[i:i+100]
                sb.table("matches").upsert(batch, on_conflict="match_key").execute()
            logger.info(f"      🗄️  Supabase: {len(matches_to_upsert)} partidos")
        except Exception as e:
            logger.error(f"      ❌ Supabase upsert matches: {e}")

    # ── Limpiar partidos obsoletos de este grupo ──────────────────────────────
    # Elimina: matches con fecha cambiada por la federación (Issue 3),
    # ghost games ya conocidos (Issue 4), y matches con group_id incorrecto (Issue 1).
    # Solo actúa sobre partidos de ESTE group_id — no toca otros grupos.
    try:
        existing_res = sb.table("matches").select("match_key").eq("group_id", group_id).execute()
        existing_keys = {r["match_key"] for r in (existing_res.data or [])}
        stale_keys = existing_keys - current_keys
        if stale_keys:
            stale_list = list(stale_keys)
            for i in range(0, len(stale_list), 50):
                batch = stale_list[i:i+50]
                sb.table("matches").delete().in_("match_key", batch).execute()
            logger.info(f"      🗄️  Supabase: eliminados {len(stale_keys)} partidos obsoletos {list(stale_keys)[:3]}")
    except Exception as e:
        logger.error(f"      ❌ Supabase cleanup matches: {e}")

    # 5. Clasificaciones – reemplazar por group_id
    clasif_data = clasif.get("clasificacion", [])
    if clasif_data:
        try:
            # Borrar clasificación anterior del grupo
            sb.table("standings").delete().eq("group_id", group_id).execute()
            standings_rows = []
            for eq in clasif_data:
                standings_rows.append({
                    "group_id": group_id,
                    "equipo": eq.get("equipo", ""),
                    "posicion": eq.get("posicion", 0),
                    "partidos_jugados": eq.get("partidos_jugados", 0),
                    "partidos_ganados": eq.get("partidos_ganados", 0),
                    "partidos_perdidos": eq.get("partidos_perdidos", 0),
                    "puntos_favor": eq.get("puntos_favor", 0),
                    "puntos_contra": eq.get("puntos_contra", 0),
                    "diferencia": eq.get("diferencia", 0),
                    "puntos": eq.get("puntos", 0),
                    "updated_at": datetime.now().isoformat(),
                })
            if standings_rows:
                sb.table("standings").insert(standings_rows).execute()
            logger.info(f"      🗄️  Supabase: {len(standings_rows)} clasificaciones")
        except Exception as e:
            logger.error(f"      ❌ Supabase standings: {e}")


# ─── Nombre de competición desde la página ──────────────────────────────────

def nombre_competicion_desde_url(url: str) -> str:
    """Extrae un nombre legible del slug de la URL como fallback."""
    from urllib.parse import unquote
    slug = unquote(url.rstrip("/").split("/")[-1])
    return slug.replace("-", " ").title()


async def obtener_nombre_competicion(page) -> str:
    """Lee el título de la competición del <h1> de la página."""
    try:
        h1 = await page.eval_on_selector(
            "h1, .titulo_seccion h2, .titulo_seccion h1",
            "el => el.textContent.trim().replace(/\\s+/g, ' ')"
        )
        if h1:
            return h1
    except Exception:
        pass
    return ""


def carpeta_competicion(nombre: str) -> str:
    """Convierte nombre de competición a nombre de carpeta."""
    nombre = re.sub(r"\s+", " ", nombre).strip()
    # Capitalizar palabras, reemplazar espacios por guiones
    nombre = re.sub(r"\s", "-", nombre)
    # Eliminar puntos finales (invalido en Windows y problematico para git)
    nombre = nombre.rstrip(".")
    return nombre


# ─── Scraper de una competición ──────────────────────────────────────────────

async def scrape_una_competicion(
    page, url: str, filtro_cat: Optional[str] = None
) -> tuple[int, str]:
    """Scrapea una competición completa. Devuelve (total_partidos, comp_carpeta)."""

    logger.info(f"📡 Navegando a {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if not await esperar_pagina(page, timeout=60000):
        logger.error("❌ No se pudo cargar la página")
        return 0, ""
    await pausa(2.0, 4.0)

    # Obtener nombre real de la competición
    comp_nombre = await obtener_nombre_competicion(page)
    if not comp_nombre:
        comp_nombre = nombre_competicion_desde_url(url)
    comp_carpeta = carpeta_competicion(comp_nombre)

    logger.info(f"🏆 COMPETICIÓN: {comp_nombre}")
    logger.info(f"📂 Carpeta: {comp_carpeta}")

    # Leer categorías
    categorias = await obtener_opciones(page, SEL_CAT)
    categorias = [c for c in categorias if c["value"]]
    logger.info(f"📋 Categorías: {len(categorias)}")
    for c in categorias:
        logger.info(f"   - {c['text']}")

    if not categorias:
        logger.warning("⚠️ Sin categorías — puede que la página no tenga dropdowns")
        return 0, comp_carpeta

    total_partidos = 0

    for cat_idx, cat in enumerate(categorias):
        cat_nombre = cat["text"]
        cat_value = cat["value"]

        if filtro_cat and filtro_cat.lower() not in cat_nombre.lower():
            continue

        logger.info(f"\n{'─' * 55}")
        logger.info(f"📂 CATEGORÍA: {cat_nombre}")

        ok = await seleccionar_dropdown(page, SEL_CAT, DDL_CATEGORIAS, cat_value, max_retries=3)
        if not ok:
            logger.error(f"  ❌ No se pudo cambiar a {cat_nombre} tras múltiples intentos")
            # Esperar antes de continuar con la siguiente categoría
            await asyncio.sleep(5.0)
            continue

        # Leer fases
        fases = await obtener_opciones(page, SEL_FASE)
        fases = [f for f in fases if f["value"]]
        logger.info(f"  📑 Fases: {[f['text'] for f in fases]}")

        if not fases:
            logger.warning(f"  ⚠️ Sin fases")
            continue

        for fase_idx, fase in enumerate(fases):
            fase_nombre = fase["text"]
            fase_value = fase["value"]
            logger.info(f"  📄 Fase: {fase_nombre}")

            ok = await seleccionar_dropdown(page, SEL_FASE, DDL_FASES, fase_value, max_retries=2)
            if not ok:
                logger.warning(f"    ⚠️ No se pudo cambiar a fase {fase_nombre}")
                await asyncio.sleep(3.0)
                continue

            # Leer grupos
            grupos = await obtener_opciones(page, SEL_GRUPO)
            grupos = [g for g in grupos if g["value"]]
            logger.info(f"    📁 Grupos: {[g['text'] for g in grupos]}")

            if not grupos:
                logger.warning(f"    ⚠️ Sin grupos")
                continue

            for grupo_idx, grupo in enumerate(grupos):
                grupo_nombre = grupo["text"]
                grupo_value = grupo["value"]
                logger.info(f"    🏷️  Grupo: {grupo_nombre}")

                ok = await seleccionar_dropdown(page, SEL_GRUPO, DDL_GRUPOS, grupo_value, max_retries=2)
                if not ok:
                    logger.warning(f"      ⚠️ No se pudo cambiar a grupo {grupo_nombre}")
                    await asyncio.sleep(3.0)
                    continue

                # Asegurar tab CALENDARIO activo
                try:
                    cal_tab = page.locator("#calendario-tab")
                    if await cal_tab.count() > 0:
                        aria = await cal_tab.get_attribute("aria-selected")
                        if aria != "true":
                            await cal_tab.click()
                            await pausa(0.5, 1.0)
                except Exception:
                    pass

                # Extraer partidos
                partidos = await extraer_partidos(
                    page, cat_nombre, fase_nombre, grupo_nombre, comp_nombre
                )
                if not partidos:
                    logger.warning(f"      ⚠️ Sin partidos")
                    continue

                logger.info(f"      📊 {len(partidos)} partidos")
                total_partidos += len(partidos)

                # Agrupar + clasificar + guardar en Supabase
                por_equipo = agrupar_por_equipo(partidos)
                logger.info(f"      👥 {len(por_equipo)} equipos")

                clasif = calcular_clasificacion(
                    partidos, cat_nombre, fase_nombre, grupo_nombre, comp_nombre
                )
                guardar_supabase(
                    partidos, clasif,
                    cat_nombre, grupo_nombre, fase_nombre,
                    comp_nombre, comp_carpeta, url
                )

                await pausa(0.8, 1.8)
            await pausa(1.0, 2.5)
        await pausa(2.0, 4.0)

    logger.info(f"\n  ✅ {comp_nombre}: {total_partidos} partidos")
    return total_partidos, comp_carpeta


# ─── Scraper principal (todas las competiciones) ─────────────────────────────

async def scrape_todas(
    filtro_comp: Optional[str] = None,
    filtro_cat: Optional[str] = None,
    headless: bool = False,
):
    logger.info("=" * 60)
    logger.info(f"🏀 SCRAPER COMPETICIONES – {TEAM_NAME}")
    logger.info(f"📋 {len(COMPETICIONES)} competiciones registradas")
    logger.info("=" * 60)

    pw, browser, context, page = await crear_browser(headless=headless)

    gran_total_partidos = 0
    resultados = []

    try:
        for comp_idx, url in enumerate(COMPETICIONES):
            # Filtrar por nombre de competición si se especificó
            if filtro_comp:
                slug = url.rstrip("/").split("/")[-1]
                from urllib.parse import unquote
                slug_decoded = unquote(slug).lower()
                # Normalizar el filtro: espacios → guiones para que coincida con el slug de la URL
                filtro_norm = filtro_comp.lower().replace(" ", "-")
                if filtro_norm not in slug_decoded:
                    continue

            logger.info(f"\n{'═' * 60}")
            logger.info(f"🏆 [{comp_idx + 1}/{len(COMPETICIONES)}] {url}")
            logger.info(f"{'═' * 60}")

            try:
                tp, comp_carpeta = await scrape_una_competicion(page, url, filtro_cat)
                gran_total_partidos += tp
                resultados.append((url, tp, "✅", comp_carpeta))
            except Exception as e:
                logger.error(f"❌ Error en competición: {e}", exc_info=True)
                resultados.append((url, 0, f"❌ {e}", ""))
                # Renavegar a una página limpia para recuperar
                try:
                    await page.goto("about:blank")
                    await pausa(1.0, 2.0)
                except Exception:
                    pass

            await pausa(3.0, 6.0)

        # Resumen final
        logger.info(f"\n{'═' * 60}")
        logger.info("📊 RESUMEN FINAL")
        logger.info(f"{'═' * 60}")
        for url, tp, status, _ in resultados:
            slug = url.rstrip("/").split("/")[-1]
            logger.info(f"  {status} {slug}: {tp} partidos")
        logger.info(f"{'─' * 60}")
        logger.info(f"  TOTAL: {gran_total_partidos} partidos → Supabase")
        logger.info(f"{'═' * 60}")

    except Exception as e:
        logger.error(f"❌ Error crítico: {e}", exc_info=True)
        raise
    finally:
        await browser.close()
        await pw.stop()


# ─── Modo automático ─────────────────────────────────────────────────────────

async def modo_automatico(headless: bool = False, filtro_comp: Optional[str] = None):
    """
    Lun–Vie: cada 2 horas.
    Sáb–Dom 8:00–23:59: cada 30 minutos.
    """
    logger.info("🔄 Modo automático activado")
    while True:
        try:
            await scrape_todas(filtro_comp=filtro_comp, headless=headless)
        except Exception as e:
            logger.error(f"❌ Error: {e}")

        ahora = datetime.now()
        es_finde = ahora.weekday() in (5, 6)
        if es_finde and 8 <= ahora.hour < 24:
            intervalo = 30
        elif es_finde:
            proxima = ahora.replace(hour=8, minute=0, second=0)
            if proxima <= ahora:
                proxima += timedelta(days=1)
            intervalo = int((proxima - ahora).total_seconds() / 60)
        else:
            intervalo = 120

        logger.info(f"⏰ Próxima ejecución en {intervalo} min")
        await asyncio.sleep(intervalo * 60)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scraper Baloncesto Cádiz")
    parser.add_argument("--watch", action="store_true", help="Modo automático")
    parser.add_argument("--competicion", type=str, default=None,
                        help="Filtrar por nombre de competición (busca en el slug de la URL)")
    parser.add_argument("--categoria", type=str, default=None, help="Filtrar categoría")
    parser.add_argument("--headless", action="store_true", help="Modo headless (puede fallar con CF)")
    args = parser.parse_args()

    if args.watch:
        asyncio.run(modo_automatico(headless=args.headless, filtro_comp=args.competicion))
    else:
        asyncio.run(scrape_todas(
            filtro_comp=args.competicion,
            filtro_cat=args.categoria,
            headless=args.headless,
        ))


if __name__ == "__main__":
    main()
