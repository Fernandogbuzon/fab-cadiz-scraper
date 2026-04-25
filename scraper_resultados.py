#!/usr/bin/env python3
"""
Scraper Rápido de Resultados (reutilizable)
==========================================
Detecta partidos del equipo configurado en team_config.json que deberían
haber terminado (hora inicio + duración) pero aún no tienen resultado,
y scrapea SOLO esos grupos específicos.

Lógica de reintentos (max configurable):
  - Intento 1: hora_partido + duración
  - Intento 2: +10 min (llamado de nuevo por el disparador)
  - Intento 3: +10 min más
  - Si al último intento no hay resultado, se rinde (aplazado/no jugado)

Estado de intentos guardado en intentos_resultados.json

Uso:
  python scraper_resultados.py                # Una pasada (scraping)
  python scraper_resultados.py --check        # Solo mostrar pendientes (sin navegador)
  python scraper_resultados.py --headless     # Intentar headless
  python scraper_resultados.py --reset        # Limpiar estado de intentos
"""

import asyncio
import json
import re
import sys
import io
import os
import argparse
import random
import logging
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Cargar .env
load_dotenv(Path(__file__).parent / ".env")

# ─── Supabase ────────────────────────────────────────────────────────────────

_supabase_client = None

def get_supabase():
    """Usa el proyecto CENTRAL de competiciones."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    url = (os.environ.get("CENTRAL_SUPABASE_URL") or os.environ.get("SUPABASE_URL") or "")
    key = (os.environ.get("CENTRAL_SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY") or "")
    # Garantizar str puro (httpx rechaza bytes como header value)
    if isinstance(url, bytes):
        url = url.decode("utf-8")
    if isinstance(key, bytes):
        key = key.decode("utf-8")
    url = url.strip()
    key = key.strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        return _supabase_client
    except Exception as e:
        logger.error(f"Error conectando Supabase: {e}")
        return None

# ─── Configuracion (desde team_config.json) ─────────────────────────────

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
DURACION_PARTIDO_HORAS = _CFG.get("match_duration_hours", 1)
MAX_INTENTOS = _CFG.get("max_retry_attempts", 5)
RETRY_INTERVAL_MIN = _CFG.get("retry_interval_minutes", 10)
# Si True, monitoriza partidos de TODOS los clubes (no solo TEAM_NAME).
# Útil cuando el scraper gestiona múltiples clubes a la vez.
WATCH_ALL_CLUBS = _CFG.get("watch_all_clubs", False)

LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Fichero de estado de intentos (persiste entre ejecuciones de CI)
INTENTOS_FILE = SCRIPT_DIR / "intentos_resultados.json"

# ASP.NET dropdown names
DDL_CATEGORIAS = "ctl00$ctl00$contenedor_informacion$contenedor_informacion_con_lateral$DDLCategorias"
DDL_FASES = "ctl00$ctl00$contenedor_informacion$contenedor_informacion_con_lateral$DDLFases"
DDL_GRUPOS = "ctl00$ctl00$contenedor_informacion$contenedor_informacion_con_lateral$DDLGrupos"

SEL_CAT = f"select[name='{DDL_CATEGORIAS}']"
SEL_FASE = f"select[name='{DDL_FASES}']"
SEL_GRUPO = f"select[name='{DDL_GRUPOS}']"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scraper_resultados.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ─── Estado de intentos (respaldado en Supabase scraper_retries) ─────────────

def cargar_intentos() -> dict:
    """
    Carga el estado de reintentos desde Supabase (tabla scraper_retries).
    Fallback a JSON local si Supabase no está disponible.
    Formato devuelto: { "match_key": { "intentos": N, "ultimo": "ISO" } }
    """
    sb = get_supabase()
    if sb:
        try:
            cutoff = (datetime.now() - timedelta(hours=48)).isoformat()
            res = sb.table("scraper_retries").select("match_key, intento, ultimo").gt("ultimo", cutoff).execute()
            return {
                r["match_key"]: {"intentos": r["intento"], "ultimo": r["ultimo"]}
                for r in (res.data or [])
            }
        except Exception as e:
            logger.warning(f"  ⚠️ No se pudo leer scraper_retries de DB: {e}. Usando JSON local.")

    # Fallback: JSON local
    if INTENTOS_FILE.exists():
        try:
            data = json.loads(INTENTOS_FILE.read_text(encoding="utf-8"))
            ahora = datetime.now()
            return {
                pid: info for pid, info in data.items()
                if (ahora - datetime.fromisoformat(info.get("ultimo", "2000-01-01"))).total_seconds() < 48 * 3600
            }
        except Exception:
            return {}
    return {}


def guardar_intentos(intentos: dict):
    """
    Persiste el estado de reintentos en Supabase (scraper_retries).
    Fallback a JSON local si Supabase no está disponible.
    """
    sb = get_supabase()
    if sb and intentos:
        rows = [
            {"match_key": mk, "intento": info.get("intentos", 1), "ultimo": info.get("ultimo")}
            for mk, info in intentos.items()
        ]
        try:
            for i in range(0, len(rows), 100):
                sb.table("scraper_retries").upsert(rows[i:i + 100], on_conflict="match_key").execute()
            return
        except Exception as e:
            logger.warning(f"  ⚠️ No se pudo guardar scraper_retries en DB: {e}. Usando JSON local.")

    INTENTOS_FILE.write_text(json.dumps(intentos, ensure_ascii=False, indent=2), encoding="utf-8")


def resetear_intentos():
    """Limpia todos los reintentos (DB y JSON local)."""
    sb = get_supabase()
    if sb:
        try:
            sb.table("scraper_retries").delete().neq("match_key", "").execute()
            logger.info("  🗄️  scraper_retries reseteado en DB")
        except Exception as e:
            logger.warning(f"  ⚠️ No se pudo resetear DB: {e}")
    if INTENTOS_FILE.exists():
        INTENTOS_FILE.unlink()
    logger.info("Intentos reseteados")


# ─── Utilidades ──────────────────────────────────────────────────────────────

def parse_fecha(fecha_str: str, hora_str: str) -> datetime:
    try:
        d, m, y = fecha_str.split("/")
        dt = datetime(int(y), int(m), int(d))
        if hora_str and ":" in hora_str:
            h, mi = hora_str.split(":")
            dt = dt.replace(hour=int(h), minute=int(mi))
        else:
            dt = dt.replace(hour=12, minute=0)
        return dt
    except Exception:
        return datetime(2000, 1, 1)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text


async def pausa(lo: float = 0.5, hi: float = 1.5):
    await asyncio.sleep(random.uniform(lo, hi))


def normalizar_nombre(nombre: str) -> str:
    """Normaliza un nombre de equipo para comparación robusta.
    Quita acentos, pasa a minúsculas, elimina espacios extra."""
    s = unicodedata.normalize("NFD", nombre)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def nombres_coinciden(nombre_json: str, nombre_web: str) -> bool:
    """Compara nombres de equipos de forma flexible.
    Los patrocinadores rotan (ej: 'ISAVAL CBA' → 'NOATUM LOGISTIC CBA'),
    así que hacemos matching parcial inteligente."""
    a = normalizar_nombre(nombre_json)
    b = normalizar_nombre(nombre_web)

    # Exacto
    if a == b:
        return True

    # Uno contenido en el otro (ej: 'CBA' en 'ISAVAL CBA')
    if a in b or b in a:
        return True

    # Comparar últimas palabras (el "nombre base" del club suele ir al final)
    # Ej: "ISAVAL CBA" → "CBA", "NOATUM LOGISTIC CBA" → "CBA"
    palabras_a = a.split()
    palabras_b = b.split()
    if len(palabras_a) >= 1 and len(palabras_b) >= 1:
        # Última palabra igual (el nombre del club)
        if palabras_a[-1] == palabras_b[-1] and len(palabras_a[-1]) >= 3:
            return True
        # Últimas 2 palabras iguales
        if len(palabras_a) >= 2 and len(palabras_b) >= 2:
            if palabras_a[-2:] == palabras_b[-2:]:
                return True

    # Primeras palabras iguales (ej: "SD CANDRAY ..." → "SD CANDRAY")
    if len(palabras_a) >= 2 and len(palabras_b) >= 2:
        if palabras_a[:2] == palabras_b[:2]:
            return True

    # Ratio de similitud con SequenceMatcher
    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.6:
        return True

    return False


# ─── Detectar partidos pendientes ────────────────────────────────────────────

def buscar_partidos_pendientes() -> list[dict]:
    """
    Consulta Supabase para encontrar partidos del equipo sin resultado
    cuyo tiempo estimado de finalización ya pasó.
    """
    sb = get_supabase()
    if not sb:
        logger.error("Supabase no configurado. No se pueden buscar partidos pendientes.")
        return []

    ahora = datetime.now()
    intentos = cargar_intentos()
    pendientes = []
    descartados = 0
    team_first = TEAM_NAME.split()[0]  # "ADESA" de "ADESA 80"
    # Estados terminales — filtrados directamente en la query para reducir filas transferidas
    excluded_estados = {"cancelado", "aplazado", "esperando_resultado", "finalizado", "sin_resultado"}

    try:
        query = sb.table("matches").select(
            "match_key, fecha, hora, local, visitante, estado, group_id, "
            "comp_groups!inner(phase, name, "
            "comp_categories!inner(slug, "
            "competitions!inner(slug, url)))"
        ).eq("es_resultado", False).not_.in_("estado", list(excluded_estados))
        if not WATCH_ALL_CLUBS:
            query = query.or_(f"local.ilike.%{team_first}%,visitante.ilike.%{team_first}%")
        res = query.execute()
    except Exception as e:
        logger.error(f"Error consultando Supabase: {e}")
        return []

    total = len(res.data or [])
    scope = "todos los clubes" if WATCH_ALL_CLUBS else TEAM_NAME
    logger.info(f"Supabase: {total} partidos de {scope} sin resultado (estado pendiente)")

    for m in (res.data or []):
        fecha_str = m.get("fecha", "")
        hora_str = m.get("hora", "")
        if not fecha_str:
            continue

        dt_inicio = parse_fecha(fecha_str, hora_str)
        dt_fin_estimado = dt_inicio + timedelta(hours=DURACION_PARTIDO_HORAS)

        if dt_fin_estimado >= ahora:
            continue

        pid = m.get("match_key", "")
        if not pid:
            continue

        info = intentos.get(pid, {})
        n_intentos = info.get("intentos", 0)
        if n_intentos >= MAX_INTENTOS:
            descartados += 1
            continue

        cg = m.get("comp_groups", {}) or {}
        cc = cg.get("comp_categories", {}) or {}
        comp = cc.get("competitions", {}) or {}

        pendientes.append({
            "local": m.get("local", ""),
            "visitante": m.get("visitante", ""),
            "fecha": fecha_str,
            "hora": hora_str,
            "estado": m.get("estado", "proximo"),
            "comp_url": comp.get("url", ""),
            "comp_slug": comp.get("slug", ""),
            "cat_carpeta": cc.get("slug", ""),
            "fase_carpeta": cg.get("phase", ""),
            "grupo_carpeta": cg.get("name", ""),
            "group_id": m.get("group_id", ""),
            "dt_inicio": dt_inicio,
            "pid": pid,
            "intento": n_intentos + 1,
        })

    if descartados:
        logger.info(f"  {descartados} descartado(s) (max {MAX_INTENTOS} intentos)")
    return pendientes


def agrupar_por_grupo(pendientes: list[dict]) -> dict[str, list[dict]]:
    grupos = {}
    for p in pendientes:
        key = f"{p['comp_slug']}|{p['cat_carpeta']}|{p['grupo_carpeta']}|{p['fase_carpeta']}"
        grupos.setdefault(key, []).append(p)
    return grupos


# ─── Browser helpers ─────────────────────────────────────────────────────────

async def crear_browser(headless: bool = False):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-blink-features=AutomationControlled",
              "--disable-dev-shm-usage"],
    )
    stealth = Stealth()
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1366, "height": 768},
        locale="es-ES",
        timezone_id="Europe/Madrid",
        extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"},
    )
    await stealth.apply_stealth_async(context)
    page = await context.new_page()
    return pw, browser, context, page


async def esperar_pagina(page, timeout: int = 60000) -> bool:
    try:
        await page.wait_for_selector(SEL_CAT, timeout=timeout, state="visible")
        await asyncio.sleep(0.5)  # Pequeña pausa adicional para asegurar estabilidad
        return True
    except Exception:
        try:
            title = await page.title()
            if "moment" in title.lower() or "momento" in title.lower():
                logger.info("  CF challenge, esperando...")
                try:
                    await page.wait_for_selector(SEL_CAT, timeout=120000, state="visible")
                    await asyncio.sleep(1.0)  # Pausa adicional tras resolver CF
                    return True
                except Exception:
                    return False
        except Exception:
            pass
        return False


async def esperar_dropdown_con_opciones(page, selector: str, timeout: int = 15000) -> bool:
    """Espera a que un dropdown tenga al menos una opción con valor cargada."""
    try:
        selector_escaped = selector.replace("'", "\\'")
        await page.wait_for_function(
            f"""() => {{
                const sel = document.querySelector('{selector_escaped}');
                return sel && Array.from(sel.options).some(o => o.value && o.value.trim() !== '');
            }}""",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


async def obtener_opciones(page, selector: str) -> list[dict]:
    return await page.eval_on_selector_all(
        selector + " option",
        "opts => opts.map(o => ({value: o.value, text: o.textContent.trim().replace(/\\s+/g, ' ')}))",
    )


async def seleccionar_dropdown(page, selector: str, ddl_name: str, value: str, max_retries: int = 3):
    for intento in range(max_retries):
        try:
            await page.evaluate("() => { window.__cFRLUnblockHandlers = true; }")
            
            # Esperar a que el selector esté disponible
            await page.wait_for_selector(selector, timeout=10000, state="visible")
            
            # Intentar selección con navegación
            try:
                async with page.expect_navigation(wait_until="load", timeout=90000):
                    await page.select_option(selector, value)
                await asyncio.sleep(0.3)
            except Exception as nav_error:
                # La navegación puede fallar si es muy rápida o no hay cambio real
                logger.debug(f"  Navigation event timeout (puede ser normal): {nav_error}")
                await asyncio.sleep(0.8)

            # Verificar que la página esté lista
            ok = await esperar_pagina(page, timeout=90000)
            if ok:
                await pausa(0.5, 1.2)
                return True

            if intento < max_retries - 1:
                logger.warning(f"  Intento {intento + 1}/{max_retries} falló para {ddl_name}, reintentando...")
                await asyncio.sleep(2.0)

        except Exception as e:
            if intento < max_retries - 1:
                logger.warning(f"  Error en intento {intento + 1}/{max_retries}: {e}")
                await asyncio.sleep(2.0)
            else:
                logger.error(f"  Error crítico tras {max_retries} intentos: {e}")
    
    return False


async def extraer_partidos_pagina(page) -> list[dict]:
    return await page.evaluate("""
        () => {
            const resultados = [];
            const cal = document.getElementById('calendario');
            if (!cal) return resultados;

            cal.querySelectorAll('header.nombre_tabla').forEach(header => {
                const h5 = header.querySelector('h5');
                const jornada = h5 ? h5.textContent.trim().replace(/\\s+/g, ' ') : '';

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
                        jornada,
                    });
                });
            });
            return resultados;
        }
    """)


# ─── Match dropdown to folder ───────────────────────────────────────────────

def match_opcion_a_carpeta(opciones: list[dict], carpeta: str) -> Optional[str]:
    """Busca la opción del dropdown que mejor coincide con el nombre de carpeta.
    Prioriza exacto > exacto sin acentos > mejor substring."""
    def strip_accents(s):
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii").lower().strip()

    carpeta_norm = carpeta.replace("-", " ").lower().strip()
    carpeta_ascii = strip_accents(carpeta.replace("-", " "))

    # Paso 1: Match exacto (con y sin acentos)
    for opt in opciones:
        opt_norm = opt["text"].lower().strip()
        if opt_norm == carpeta_norm:
            return opt["value"]
    for opt in opciones:
        if strip_accents(opt["text"]) == carpeta_ascii:
            return opt["value"]

    # Paso 2: Substring match - buscar el MEJOR match (más largo overlap)
    best_match = None
    best_score = 0
    for opt in opciones:
        opt_ascii = strip_accents(opt["text"])
        # Solo match si la carpeta está contenida en la opción
        # (no al revés, para evitar 'grupo a' matcheando 'grupo ascenso')
        if carpeta_ascii == opt_ascii:
            return opt["value"]
        # Substring: carpeta contenida en opción, y la longitud es similar
        if carpeta_ascii in opt_ascii:
            score = len(carpeta_ascii) / max(len(opt_ascii), 1)
            if score > best_score:
                best_score = score
                best_match = opt["value"]
        elif opt_ascii in carpeta_ascii:
            score = len(opt_ascii) / max(len(carpeta_ascii), 1)
            if score > best_score:
                best_score = score
                best_match = opt["value"]

    if best_match and best_score >= 0.5:
        return best_match

    # Paso 3: Match por números extraídos (ej: "JORNADAS-5ª-10ª" → "5 10",
    # útil cuando la web cambia la literalidad pero conserva el rango numérico)
    nums_carpeta = re.findall(r"\d+", carpeta_ascii)
    if len(nums_carpeta) >= 2:
        for opt in opciones:
            nums_opt = re.findall(r"\d+", strip_accents(opt["text"]))
            if nums_carpeta == nums_opt:
                logger.debug(f"  Fase matcheada por números {nums_carpeta}: '{carpeta}' → '{opt['text']}'")
                return opt["value"]

    return None


# ─── Scraping dirigido de un grupo ──────────────────────────────────────────

async def scrape_grupo(page, comp_url, cat_carpeta, fase_carpeta, grupo_carpeta) -> list[dict]:
    page_url = page.url or ""
    # Extraer el ID de competición (delegacion-competicion-XXXX) para comparar
    comp_id = ""
    if "/delegacion-competicion" in comp_url:
        parts = comp_url.split("/delegacion-competicion")
        comp_id = "/delegacion-competicion" + parts[1].split("/")[0]

    if not comp_id or comp_id not in page_url:
        logger.info(f"  Navegando a: {comp_url}")
        await page.goto(comp_url, wait_until="domcontentloaded", timeout=60000)
        if not await esperar_pagina(page, timeout=60000):
            logger.error("  No se pudo cargar la pagina")
            return []
        await esperar_dropdown_con_opciones(page, SEL_CAT)
        await pausa(0.5, 1.0)

    # Categoria
    cats = await obtener_opciones(page, SEL_CAT)
    cats = [c for c in cats if c["value"]]
    cat_value = match_opcion_a_carpeta(cats, cat_carpeta)
    if not cat_value:
        logger.error(f"  Categoria '{cat_carpeta}' no encontrada")
        return []

    logger.info(f"  Categoria: {cat_carpeta}")
    if not await seleccionar_dropdown(page, SEL_CAT, DDL_CATEGORIAS, cat_value, max_retries=3):
        logger.error(f"  No se pudo cambiar a categoría {cat_carpeta} tras múltiples intentos")
        return []
    await esperar_dropdown_con_opciones(page, SEL_FASE)

    # Fase
    fases = await obtener_opciones(page, SEL_FASE)
    fases = [f for f in fases if f["value"]]
    fase_value = match_opcion_a_carpeta(fases, fase_carpeta)
    if not fase_value:
        fase_value = fases[0]["value"] if len(fases) == 1 else None
    if not fase_value:
        opciones_disp = [f["text"] for f in fases]
        logger.error(f"  Fase '{fase_carpeta}' no encontrada. Opciones disponibles: {opciones_disp}")
        return []

    logger.info(f"  Fase: {fase_carpeta}")
    if not await seleccionar_dropdown(page, SEL_FASE, DDL_FASES, fase_value, max_retries=2):
        logger.warning(f"  No se pudo cambiar a fase {fase_carpeta}")
        return []
    await esperar_dropdown_con_opciones(page, SEL_GRUPO)

    # Grupo
    grupos = await obtener_opciones(page, SEL_GRUPO)
    grupos = [g for g in grupos if g["value"]]
    grupo_value = match_opcion_a_carpeta(grupos, grupo_carpeta)
    if not grupo_value:
        grupo_value = grupos[0]["value"] if len(grupos) == 1 else None
    if not grupo_value:
        logger.error(f"  Grupo '{grupo_carpeta}' no encontrado")
        return []

    logger.info(f"  Grupo: {grupo_carpeta}")
    if not await seleccionar_dropdown(page, SEL_GRUPO, DDL_GRUPOS, grupo_value, max_retries=2):
        logger.warning(f"  No se pudo cambiar a grupo {grupo_carpeta}")
        return []

    # Tab calendario
    try:
        cal_tab = page.locator("#calendario-tab")
        if await cal_tab.count() > 0:
            aria = await cal_tab.get_attribute("aria-selected")
            if aria != "true":
                await cal_tab.click()
                await pausa(0.5, 1.0)
    except Exception:
        pass

    partidos = await extraer_partidos_pagina(page)
    logger.info(f"  Extraidos {len(partidos)} partidos del grupo")
    return partidos





# ─── Actualizar resultados en Supabase ───────────────────────────────────────

def actualizar_supabase_resultados(partidos_web: list[dict], group_id: str = None) -> set[str]:
    """
    Actualiza resultados de partidos en Supabase a partir de los datos web.
    Devuelve el set de match_keys actualizados.
    Si se pasa group_id, filtra por ese grupo para evitar contaminación entre categorías.

    Implementación batch: 1 SELECT para obtener todos los matches del grupo +
    1 upsert masivo. Evita el patrón N×(SELECT LIKE + UPDATE) del código anterior.
    """
    sb = get_supabase()
    if not sb:
        return set()

    con_resultado = [pw for pw in partidos_web if pw.get("es_resultado")]
    if not con_resultado:
        return set()

    # 1. Obtener todos los matches del grupo en una sola consulta
    try:
        query = sb.table("matches").select("id, match_key, fecha, local, visitante, es_resultado")
        if group_id:
            query = query.eq("group_id", group_id)
        existing = query.execute()
    except Exception as e:
        logger.error(f"  Supabase select error: {e}")
        return set()

    db_rows = existing.data or []
    if not db_rows:
        logger.warning(f"  ⚠️  DB devolvió 0 filas para group_id={group_id}. "
                       "Puede que el calendario no esté en Supabase para este grupo.")
        return set()

    # 2. Construir lookup: fecha_sin_barras + equipos_ordenados → match de BD
    #    Normalizamos las fechas a DDMMYYYY con ceros (ej: "25/4/2026" → "25042026")
    def _fecha_clean(f: str) -> str:
        parts = f.split("/")
        if len(parts) == 3:
            return f"{parts[0].zfill(2)}{parts[1].zfill(2)}{parts[2]}"
        return f.replace("/", "")

    db_lookup: dict[str, dict] = {}
    for m in db_rows:
        fc = _fecha_clean(m.get("fecha", ""))
        eq_sorted = "_".join(sorted([slugify(m.get("local", "")), slugify(m.get("visitante", ""))]))
        db_lookup[f"{fc}_{eq_sorted}"] = m

    logger.info(f"  🗄️  Supabase: {len(db_rows)} matches en el grupo para cruzar")

    # 3. Cruzar resultados web con matches de BD
    updates: list[dict] = []
    actualizados: set[str] = set()
    ts = datetime.now().isoformat()

    def _buscar_db_match(fecha_web: str, loc_web: str, vis_web: str) -> Optional[dict]:
        """Primero match exacto por clave; si falla, intento fuzzy por nombre de equipo."""
        fc = _fecha_clean(fecha_web)
        key = f"{fc}_{'_'.join(sorted([slugify(loc_web), slugify(vis_web)]))}"
        m = db_lookup.get(key)
        if m:
            return m
        # Fallback fuzzy: misma fecha normalizada, nombres similares
        for db_key, db_m in db_lookup.items():
            if not db_key.startswith(fc + "_"):
                continue
            db_loc = db_m.get("local", "")
            db_vis = db_m.get("visitante", "")
            if (nombres_coinciden(loc_web, db_loc) and nombres_coinciden(vis_web, db_vis)) or \
               (nombres_coinciden(loc_web, db_vis) and nombres_coinciden(vis_web, db_loc)):
                logger.debug(f"  Fuzzy match: '{loc_web}' vs '{db_loc}' / '{vis_web}' vs '{db_vis}'")
                return db_m
        return None

    for pw in con_resultado:
        loc = pw.get("local", "").strip()
        vis = pw.get("visitante", "").strip()
        if not loc or not vis:
            continue
        db_match = _buscar_db_match(pw.get("fecha", ""), loc, vis)
        if not db_match:
            logger.debug(f"  Sin match DB: {pw.get('fecha','')} {loc} vs {vis}")
            continue
        if db_match.get("es_resultado"):
            # Ya tiene resultado — registrar como actualizado sin re-escribir
            actualizados.add(db_match["match_key"])
            continue
        updates.append({
            "match_key": db_match["match_key"],
            "marcador_local": pw["marcador_local"],
            "marcador_visitante": pw["marcador_visitante"],
            "es_resultado": True,
            "estado": "finalizado",
            "hora": pw.get("hora", ""),
            "pabellon": pw.get("pabellon", ""),
            "updated_at": ts,
        })
        logger.info(f"  RESULTADO: {loc} {pw['marcador_local']}-{pw['marcador_visitante']} {vis}")
        actualizados.add(db_match["match_key"])

    # 4. Upsert en bloque (on_conflict=match_key)
    if updates:
        try:
            for i in range(0, len(updates), 100):
                sb.table("matches").upsert(updates[i:i + 100], on_conflict="match_key").execute()
            logger.info(f"  🗄️  Supabase: {len(updates)} resultado(s) actualizados")
        except Exception as e:
            logger.error(f"  Supabase batch update error: {e}")

    if actualizados:
        logger.info(f"  🗄️  Supabase: {len(actualizados)} resultado(s) encontrados")
    return actualizados


def marcar_supabase_estado(match_key: str, estado: str):
    """Marca estado de un partido en Supabase (aplazado/esperando_resultado)."""
    sb = get_supabase()
    if not sb:
        return
    try:
        sb.table("matches").update({
            "estado": estado,
            "updated_at": datetime.now().isoformat(),
        }).eq("match_key", match_key).execute()
    except Exception as e:
        logger.error(f"  Supabase marca estado error: {e}")


# ─── scraper_runs: registro de ejecuciones ───────────────────────────────────

def _run_comenzar() -> Optional[str]:
    """Registra el inicio de una ejecución en scraper_runs. Devuelve el run_id."""
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = sb.table("scraper_runs").insert({
            "run_type": "results",
            "status": "running",
            "club_ids": [],
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        logger.warning(f"  ⚠️ No se pudo registrar scraper_run: {e}")
        return None


def _run_finalizar(run_id: Optional[str], status: str, grupos_scrapeados: int,
                   matches_actualizados: int, errors: list):
    """Actualiza el registro de ejecución al finalizar."""
    if not run_id:
        return
    sb = get_supabase()
    if not sb:
        return
    try:
        sb.table("scraper_runs").update({
            "status": status,
            "groups_scraped": grupos_scrapeados,
            "matches_upserted": matches_actualizados,
            "errors": errors,
            "finished_at": datetime.now().isoformat(),
        }).eq("id", run_id).execute()
    except Exception as e:
        logger.warning(f"  ⚠️ No se pudo actualizar scraper_run: {e}")


# ─── Pipeline principal ──────────────────────────────────────────────────────

async def actualizar_resultados(headless: bool = False, check_only: bool = False) -> int:
    logger.info("=" * 60)
    logger.info(f"SCRAPER RAPIDO DE RESULTADOS -- {TEAM_NAME}")
    logger.info(f"{datetime.now().strftime('%d/%m/%Y %H:%M')}")
    logger.info("=" * 60)

    pendientes = buscar_partidos_pendientes()

    if not pendientes:
        logger.info("No hay partidos pendientes de resultado.")
        return 0

    logger.info(f"\n{len(pendientes)} partido(s) pendiente(s):")
    for p in pendientes:
        logger.info(f"  [{p['intento']}/{MAX_INTENTOS}] {p.get('local','?')} vs {p.get('visitante','?')} "
                     f"({p.get('fecha','?')} {p.get('hora','?')}) -- {p['cat_carpeta']}")

    if check_only:
        logger.info("\n(modo --check: no se realiza scraping)")
        return len(pendientes)

    grupos = agrupar_por_grupo(pendientes)
    logger.info(f"\n{len(grupos)} grupo(s) a scrapear")

    run_id = _run_comenzar()
    pw_inst, browser, context, page = await crear_browser(headless=headless)
    total_actualizados = 0
    grupos_scrapeados = 0
    run_errors: list = []
    intentos = cargar_intentos()
    run_status = "completed"

    try:
        for key, partidos_grupo in grupos.items():
            comp_slug, cat_carpeta, grupo_carpeta, fase_carpeta = key.split("|")

            comp_url = partidos_grupo[0]["comp_url"]
            if not comp_url:
                logger.warning(f"  Competicion '{comp_slug}' sin URL en Supabase. Saltando.")
                continue

            logger.info(f"\n{'─' * 50}")
            logger.info(f"  {comp_slug} / {cat_carpeta} / {grupo_carpeta}")

            try:
                partidos_web = await scrape_grupo(
                    page, comp_url, cat_carpeta, fase_carpeta, grupo_carpeta
                )
                grupos_scrapeados += 1
            except Exception as e:
                logger.error(f"  Error scraping: {e}")
                run_errors.append({"grupo": key, "error": str(e)})
                try:
                    await page.goto("about:blank")
                    await pausa(0.5, 1.0)
                except Exception:
                    pass
                for p in partidos_grupo:
                    info = intentos.get(p["pid"], {"intentos": 0})
                    info["intentos"] = info.get("intentos", 0) + 1
                    info["ultimo"] = datetime.now().isoformat()
                    intentos[p["pid"]] = info
                continue

            if not partidos_web:
                for p in partidos_grupo:
                    info = intentos.get(p["pid"], {"intentos": 0})
                    info["intentos"] = info.get("intentos", 0) + 1
                    info["ultimo"] = datetime.now().isoformat()
                    intentos[p["pid"]] = info
                continue

            con_resultado = [pw for pw in partidos_web if pw["es_resultado"]]
            logger.info(f"  Web: {len(partidos_web)} partidos ({len(con_resultado)} con resultado)")

            # Actualizar resultados en Supabase y obtener qué match_keys se actualizaron
            # Pasar group_id para evitar contaminar partidos de otras categorías
            group_id = partidos_grupo[0].get("group_id") if partidos_grupo else None
            pids_actualizados = actualizar_supabase_resultados(partidos_web, group_id=group_id)
            total_actualizados += len(pids_actualizados)

            for p in partidos_grupo:
                pid = p["pid"]
                if pid in pids_actualizados:
                    intentos.pop(pid, None)
                else:
                    info = intentos.get(pid, {"intentos": 0})
                    info["intentos"] = info.get("intentos", 0) + 1
                    info["ultimo"] = datetime.now().isoformat()
                    intentos[pid] = info
                    n = info["intentos"]
                    if n >= MAX_INTENTOS:
                        logger.info(f"  RENDIDO ({n}/{MAX_INTENTOS}): {p['local']} vs {p['visitante']}")
                        misma_fecha = [pw for pw in partidos_web if pw.get("fecha") == p["fecha"]]
                        con_res = [pw for pw in misma_fecha if pw.get("es_resultado")]
                        if len(misma_fecha) > 0 and len(con_res) >= len(misma_fecha) * 0.5:
                            estado_new = "aplazado"
                            logger.info(f"  APLAZADO ({len(con_res)}/{len(misma_fecha)} partidos con resultado)")
                        else:
                            estado_new = "esperando_resultado"
                            logger.info(f"  ESPERANDO RESULTADO ({len(con_res)}/{len(misma_fecha)} partidos)")
                        marcar_supabase_estado(pid, estado_new)
                    else:
                        logger.info(f"  Sin resultado ({n}/{MAX_INTENTOS}). Se reintentara en ~{RETRY_INTERVAL_MIN}min.")

            await pausa(0.5, 1.0)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        run_errors.append({"error": str(e)})
        run_status = "failed"
    finally:
        await browser.close()
        await pw_inst.stop()
        guardar_intentos(intentos)
        _run_finalizar(run_id, run_status, grupos_scrapeados, total_actualizados, run_errors)

    logger.info(f"\n{'=' * 60}")
    logger.info(f"RESUMEN: {total_actualizados} resultado(s) de {len(pendientes)} pendiente(s)")
    logger.info(f"{'=' * 60}")
    return total_actualizados

# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=f"Scraper rapido de resultados {TEAM_NAME} (max {MAX_INTENTOS} intentos)"
    )
    parser.add_argument("--check", action="store_true",
                        help="Solo mostrar pendientes (sin scraping)")
    parser.add_argument("--headless", action="store_true",
                        help="Modo headless")
    parser.add_argument("--reset", action="store_true",
                        help="Limpiar estado de intentos")
    args = parser.parse_args()

    if args.reset:
        resetear_intentos()
        return

    asyncio.run(actualizar_resultados(headless=args.headless, check_only=args.check))


if __name__ == "__main__":
    main()
