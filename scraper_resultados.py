#!/usr/bin/env python3
"""
Scraper Rápido de Resultados (reutilizable)
==========================================
Detecta partidos del equipo configurado en team_config.json que deberían
haber terminado (hora inicio + duración) pero aún no tienen resultado,
y scrapea SOLO esos grupos específicos.

Primera consulta: hora programada + duración. Reintentos rápidos al principio
y espaciados después, compartidos con scraper_precheck.py. Los fallos técnicos
no consumen intentos y agotar el presupuesto nunca implica un aplazamiento.
Estado persistente en Supabase; JSON solo para ejecuciones locales sin CI.

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
from scraper_common import build_lookup_key, fecha_key, parse_fecha_hora, slugify as _shared_slugify
from scraper_schedule import MADRID, pending_matches, utc_now
from scraper_precheck import load_context

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
MAX_INTENTOS = _CFG.get("max_retry_attempts", 11)
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
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

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

_intentos_iniciales: dict = {}


def guardar_intentos(intentos: dict, resueltos: set[str] | None = None):
    """Write changed retries and remove resolved ones, including an empty state.

    A CI persistence failure must fail the run: an ephemeral JSON fallback would
    silently reset the budget on the next runner.
    """
    sb = get_supabase()
    eliminados = (set(_intentos_iniciales) - set(intentos)) | (resueltos or set())
    if sb:
        rows = [
            {"match_key": mk, "intento": info.get("intentos", 1), "ultimo": info.get("ultimo")}
            for mk, info in intentos.items()
            if info != _intentos_iniciales.get(mk) and mk not in eliminados
        ]
        try:
            for i in range(0, len(rows), 100):
                sb.table("scraper_retries").upsert(rows[i:i + 100], on_conflict="match_key").execute()
            keys = sorted(eliminados)
            for i in range(0, len(keys), 50):
                sb.table("scraper_retries").delete().in_("match_key", keys[i:i + 50]).execute()
            return
        except Exception as e:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError("No se pudo persistir el estado de reintentos") from e
            logger.warning(f"  ⚠️ No se pudo guardar scraper_retries en DB: {e}. Usando JSON local.")
    elif os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("Supabase no disponible para persistir reintentos")
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
    return parse_fecha_hora(fecha_str, hora_str)


def slugify(text: str) -> str:
    return _shared_slugify(text)


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
    """Use the exact same query, state and policy as the lightweight precheck."""
    global _intentos_iniciales
    matches, intentos = load_context(_CFG)
    if os.environ.get("GITHUB_ACTIONS") != "true" and INTENTOS_FILE.exists():
        # Local recovery after a failed DB write; DB remains authoritative in CI.
        local = json.loads(INTENTOS_FILE.read_text(encoding="utf-8"))
        from scraper_schedule import parse_timestamp
        for key, info in local.items():
            local_time = parse_timestamp(info.get("ultimo"))
            db_time = parse_timestamp(intentos.get(key, {}).get("ultimo"))
            if local_time and (not db_time or local_time > db_time):
                intentos[key] = info
    _intentos_iniciales = {key: dict(info) for key, info in intentos.items()}
    pendientes = pending_matches(matches, intentos, _CFG)
    for m in pendientes:
        cg = m.get("comp_groups", {}) or {}
        cc = cg.get("comp_categories", {}) or {}
        comp = cc.get("competitions", {}) or {}
        m.update({
            "comp_url": comp.get("url", ""),
            "comp_slug": comp.get("slug", ""),
            "cat_carpeta": cc.get("slug", ""),
            "fase_carpeta": cg.get("phase", ""),
            "grupo_carpeta": cg.get("name", ""),
        })
    logger.info(f"Supabase: {len(matches)} candidatos recientes; {len(pendientes)} consultas vencidas")
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
            if await page.locator(selector).input_value() == value:
                return True
            
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
    best_text = ""
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
                best_text = opt["text"]
        elif opt_ascii in carpeta_ascii:
            score = len(opt_ascii) / max(len(carpeta_ascii), 1)
            if score > best_score:
                best_score = score
                best_match = opt["value"]
                best_text = opt["text"]

    if best_match and best_score >= 0.5:
        # Match no exacto: registrar para que los mis-bindings sean visibles en logs CI
        logger.info(f"  Match aproximado (substring, score={best_score:.2f}): '{carpeta}' → '{best_text}'")
        return best_match

    # Paso 3: Match por números extraídos (ej: "JORNADAS-5ª-10ª" → "5 10",
    # útil cuando la web cambia la literalidad pero conserva el rango numérico)
    nums_carpeta = re.findall(r"\d+", carpeta_ascii)
    if len(nums_carpeta) >= 2:
        for opt in opciones:
            nums_opt = re.findall(r"\d+", strip_accents(opt["text"]))
            if nums_carpeta == nums_opt:
                logger.info(f"  Fase matcheada por números {nums_carpeta}: '{carpeta}' → '{opt['text']}'")
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


async def scrape_otros_grupos_en_fase(
    page, comp_url: str, cat_carpeta: str, fase_carpeta: str, grupo_excluir: str,
    cache: dict | None = None,
) -> list[dict]:
    """
    Escanea TODOS los grupos de una fase/categoría y devuelve la lista combinada de partidos.
    Útil cuando el group_id en DB no coincide con el grupo web real del partido.

    `cache` (dict por ejecución, clave=(comp_url,cat,fase)): escanea la fase UNA sola vez por
    ejecución. Evita el blow-up N×G de postbacks cuando varios partidos sin resultado caen en
    la misma fase (cada postback puede tardar ~90s y reventar el límite de 12 min del job CI).
    `grupo_excluir` se conserva por compatibilidad de firma; ya no se excluye ningún grupo para
    que la caché cubra la fase completa (correcto para cualquier partido de la fase).
    """
    cache_key = (comp_url, cat_carpeta, fase_carpeta)
    if cache is not None and cache_key in cache:
        logger.info(f"  ♻️  Fase '{fase_carpeta}' ya escaneada en esta ejecución (caché)")
        return cache[cache_key]
    all_partidos: list[dict] = []
    comp_id = ""
    if "/delegacion-competicion" in comp_url:
        parts = comp_url.split("/delegacion-competicion")
        comp_id = "/delegacion-competicion" + parts[1].split("/")[0]

    if not comp_id or comp_id not in (page.url or ""):
        await page.goto(comp_url, wait_until="domcontentloaded", timeout=60000)
        if not await esperar_pagina(page, timeout=60000):
            raise RuntimeError("No se pudo leer la fase para buscar grupos alternativos")
        await esperar_dropdown_con_opciones(page, SEL_CAT)
        await pausa(0.5, 1.0)

    cats = await obtener_opciones(page, SEL_CAT)
    cats = [c for c in cats if c["value"]]
    cat_value = match_opcion_a_carpeta(cats, cat_carpeta)
    if not cat_value:
        raise RuntimeError("No se pudo leer la fase para buscar grupos alternativos")
    if not await seleccionar_dropdown(page, SEL_CAT, DDL_CATEGORIAS, cat_value, max_retries=2):
        raise RuntimeError("No se pudo leer la fase para buscar grupos alternativos")
    await esperar_dropdown_con_opciones(page, SEL_FASE)

    fases = await obtener_opciones(page, SEL_FASE)
    fases = [f for f in fases if f["value"]]
    fase_value = match_opcion_a_carpeta(fases, fase_carpeta)
    if not fase_value:
        fase_value = fases[0]["value"] if len(fases) == 1 else None
    if not fase_value:
        raise RuntimeError("No se pudo leer la fase para buscar grupos alternativos")
    if not await seleccionar_dropdown(page, SEL_FASE, DDL_FASES, fase_value, max_retries=2):
        raise RuntimeError("No se pudo leer la fase para buscar grupos alternativos")
    await esperar_dropdown_con_opciones(page, SEL_GRUPO)

    grupos = await obtener_opciones(page, SEL_GRUPO)
    grupos = [g for g in grupos if g["value"]]

    if not grupos:
        raise RuntimeError("La fase no contiene grupos legibles")

    for grupo_opt in grupos:
        logger.info(f"  🔍 Grupo de la fase: '{grupo_opt['text']}'")
        if not await seleccionar_dropdown(page, SEL_GRUPO, DDL_GRUPOS, grupo_opt["value"], max_retries=2):
            raise RuntimeError("No se pudo seleccionar un grupo alternativo")
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
        logger.info(f"    {len(partidos)} partidos en '{grupo_opt['text']}'")
        all_partidos.extend(partidos)

    if cache is not None:
        cache[cache_key] = all_partidos
    return all_partidos



def actualizar_supabase_resultados(partidos_web: list[dict], group_id: str = None,
                                   competition_id: str = None) -> set[str]:
    """
    Actualiza resultados de partidos en Supabase a partir de los datos web.
    Devuelve el set de match_keys actualizados.
    Si se pasa group_id, filtra por ese grupo para evitar contaminación entre categorías.
    Si NO hay group_id (búsqueda en grupos alternativos) pero sí competition_id, acota a
    esa competición: evita escanear toda la tabla matches y, sobre todo, evita cruzar un
    resultado con un partido de OTRA competición (mismo nombre de equipo, fecha similar).

    Implementación batch: 1 SELECT para obtener todos los matches del grupo +
    1 upsert masivo. Evita el patrón N×(SELECT LIKE + UPDATE) del código anterior.
    """
    sb = get_supabase()
    if not sb:
        raise RuntimeError("Supabase no disponible para guardar resultados")
    if not group_id and not competition_id:
        raise ValueError("Los resultados deben acotarse a un grupo o competición")

    con_resultado = [pw for pw in partidos_web if pw.get("es_resultado")]
    if not con_resultado:
        return set()

    # 1. Obtener todos los matches del grupo en una sola consulta
    try:
        query = sb.table("matches").select(
            "match_key,fecha,local,visitante,es_resultado,group_id,competition_id,hora,pabellon"
        ).eq("es_resultado", False)
        if group_id:
            query = query.eq("group_id", group_id)
        elif competition_id:
            query = query.eq("competition_id", competition_id)
        existing = query.execute()
    except Exception as e:
        raise RuntimeError("No se pudieron leer los partidos para guardar resultados") from e

    db_rows = existing.data or []
    if not db_rows:
        logger.warning(f"  ⚠️  DB devolvió 0 filas para group_id={group_id}. "
                       "Puede que el calendario no esté en Supabase para este grupo.")
        return set()

    # 2. Construir lookup: fecha_sin_barras + equipos_ordenados → match de BD
    #    Normalizamos las fechas a DDMMYYYY con ceros (ej: "25/4/2026" → "25042026")
    db_lookup: dict[str, dict] = {}
    for m in db_rows:
        db_lookup[build_lookup_key(m.get("fecha", ""), m.get("local", ""), m.get("visitante", ""))] = m

    logger.info(f"  🗄️  Supabase: {len(db_rows)} matches en el grupo para cruzar")

    # 3. Cruzar resultados web con matches de BD
    updates: list[dict] = []
    actualizados: set[str] = set()
    ts = utc_now().isoformat()

    def _buscar_db_match(fecha_web: str, loc_web: str, vis_web: str) -> Optional[dict]:
        """Primero match exacto por clave; si falla, intento fuzzy por nombre de equipo.
        Si sigue fallando, prueba ±1 día (la web y la BD a veces difieren en 1 día)."""
        fc = fecha_key(fecha_web)
        key = build_lookup_key(fecha_web, loc_web, vis_web)
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
                logger.info(f"  Fuzzy match: '{loc_web}' → '{db_loc}' / '{vis_web}' → '{db_vis}'")
                return db_m
        # Fallback ±1 día: a veces la federación registra el partido con 1 día de diferencia
        try:
            d, mo, y = fecha_web.split("/")
            dt_base = datetime(int(y), int(mo), int(d))
            for delta in (-1, 1):
                fc_adj = fecha_key((dt_base + timedelta(days=delta)).strftime("%d/%m/%Y"))
                for db_key, db_m in db_lookup.items():
                    if not db_key.startswith(fc_adj + "_"):
                        continue
                    db_loc = db_m.get("local", "")
                    db_vis = db_m.get("visitante", "")
                    if (nombres_coinciden(loc_web, db_loc) and nombres_coinciden(vis_web, db_vis)) or \
                       (nombres_coinciden(loc_web, db_vis) and nombres_coinciden(vis_web, db_loc)):
                        logger.info(
                            f"  Match ±1 día: web={fecha_web} → DB={db_m.get('fecha','')} "
                            f"'{db_loc}' vs '{db_vis}'"
                        )
                        return db_m
        except Exception:
            pass
        return None

    sin_match: list[str] = []
    seen_updates: set[str] = set()
    for pw in con_resultado:
        loc = pw.get("local", "").strip()
        vis = pw.get("visitante", "").strip()
        if not loc or not vis:
            continue
        db_match = _buscar_db_match(pw.get("fecha", ""), loc, vis)
        if not db_match:
            sin_match.append(f"{pw.get('fecha','')} {loc} vs {vis}")
            continue
        if db_match.get("es_resultado"):
            # No contar resultados históricos como nuevas escrituras.
            continue
        if db_match["match_key"] in seen_updates:
            continue
        seen_updates.add(db_match["match_key"])
        direct = nombres_coinciden(loc, db_match["local"]) and nombres_coinciden(vis, db_match["visitante"])
        reverse_exact = slugify(loc) == slugify(db_match["visitante"]) and slugify(vis) == slugify(db_match["local"])
        reverse = reverse_exact or (not direct and nombres_coinciden(loc, db_match["visitante"])
                                    and nombres_coinciden(vis, db_match["local"]))
        ml, mv = pw["marcador_local"], pw["marcador_visitante"]
        if reverse:
            ml, mv = mv, ml
        updates.append({
            "match_key": db_match["match_key"],
            # PostgreSQL validates NOT NULL before resolving ON CONFLICT. A
            # partial upsert with only scores cannot update this schema.
            "group_id": db_match["group_id"],
            "competition_id": db_match["competition_id"],
            "fecha": db_match["fecha"],
            "local": db_match["local"],
            "visitante": db_match["visitante"],
            "marcador_local": ml,
            "marcador_visitante": mv,
            "es_resultado": True,
            "estado": "finalizado",
            "hora": pw.get("hora") or db_match.get("hora", ""),
            "pabellon": pw.get("pabellon") or db_match.get("pabellon", ""),
            "updated_at": ts,
        })

    # 4. Upsert en bloque (on_conflict=match_key)
    if updates:
        try:
            for i in range(0, len(updates), 100):
                batch = updates[i:i + 100]
                sb.table("matches").upsert(batch, on_conflict="match_key").execute()
                actualizados.update(row["match_key"] for row in batch)
            logger.info(f"  🗄️  Supabase: {len(updates)} resultado(s) actualizados")
        except Exception as e:
            raise RuntimeError("No se pudieron persistir los resultados") from e

    if sin_match:
        logger.warning(f"  ⚠️  {len(sin_match)} resultado(s) web sin match en DB: {sin_match}")
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
            "updated_at": utc_now().isoformat(),
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
            "finished_at": utc_now().isoformat(),
        }).eq("id", run_id).execute()
    except Exception as e:
        logger.warning(f"  ⚠️ No se pudo actualizar scraper_run: {e}")


# ─── Pipeline principal ──────────────────────────────────────────────────────

async def actualizar_resultados(headless: bool = False, check_only: bool = False) -> int:
    logger.info(f"SCRAPER RESULTADOS -- {TEAM_NAME} -- {utc_now().astimezone(MADRID):%d/%m/%Y %H:%M}")
    pendientes = buscar_partidos_pendientes()
    if not pendientes:
        logger.info("No hay consultas de resultados vencidas.")
        return 0
    if check_only:
        for match in pendientes:
            logger.info(f"  [{match['intento']}/{MAX_INTENTOS}] {match['local']} - {match['visitante']}")
        return 0

    # Each group is fetched once, ordered to reuse the current competition/category.
    grupos = agrupar_por_grupo(pendientes)
    intentos = {key: dict(info) for key, info in _intentos_iniciales.items()}
    resueltos: set[str] = set()
    run_errors: list[dict] = []
    grupos_scrapeados = 0
    otros_grupos_cache: dict = {}
    run_id = _run_comenzar()
    pw_inst = browser = None

    def registrar_consulta(match: dict, *, comprobado: bool):
        # Only a successful source read consumes the result-attempt budget.
        # Technical failures still persist a cooldown so the next run cannot spin.
        intentos[match['pid']] = {
            "intentos": match['intento'] if comprobado else match['intento'] - 1,
            "ultimo": utc_now().isoformat(),
        }

    try:
        pw_inst, browser, context, page = await crear_browser(headless=headless)
        for key, partidos_grupo in sorted(grupos.items()):
            first = partidos_grupo[0]
            comp_url = first['comp_url']
            logger.info(f"Grupo: {key}; {len(partidos_grupo)} consulta(s)")
            try:
                if not comp_url:
                    raise RuntimeError("Competición sin URL en Supabase")
                partidos_web = await scrape_grupo(
                    page, comp_url, first['cat_carpeta'], first['fase_carpeta'], first['grupo_carpeta']
                )
                if not partidos_web:
                    raise RuntimeError("El grupo no se pudo leer o no contiene partidos")
                grupos_scrapeados += 1
                confirmed = actualizar_supabase_resultados(partidos_web, group_id=first.get('group_id'),
                                                          competition_id=first.get('competition_id'))
                resueltos.update(confirmed)
                for match in partidos_grupo:
                    pid = match['pid']
                    if pid in resueltos:
                        intentos.pop(pid, None)
                        continue
                    # Search alternate groups only at the beginning and end of the
                    # budget, reusing a phase cache rather than scanning each retry.
                    web_match = [row for row in partidos_web
                                 if build_lookup_key(row.get('fecha', ''), row.get('local', ''), row.get('visitante', ''))
                                 == build_lookup_key(match['fecha'], match['local'], match['visitante'])]
                    if not web_match and match['intento'] in (1, MAX_INTENTOS):
                        alternatives = await scrape_otros_grupos_en_fase(
                            page, comp_url, match['cat_carpeta'], match['fase_carpeta'],
                            match['grupo_carpeta'], cache=otros_grupos_cache,
                        )
                        # Keep the date, not just team names: the same teams may
                        # meet repeatedly in one season.
                        candidates = [row for row in alternatives
                                      if fecha_key(row.get('fecha', '')) == fecha_key(match['fecha'])
                                      and nombres_coinciden(row.get('local', ''), match['local'])
                                      and nombres_coinciden(row.get('visitante', ''), match['visitante'])]
                        confirmed = actualizar_supabase_resultados(
                            candidates, competition_id=match.get('competition_id')
                        )
                        resueltos.update(confirmed)
                        if pid in confirmed:
                            intentos.pop(pid, None)
                            continue
                    registrar_consulta(match, comprobado=True)
                    if match['intento'] >= MAX_INTENTOS:
                        # A missing score says nothing about cancellation or postponement.
                        marcar_supabase_estado(pid, 'esperando_resultado')
                        logger.info(f"  Presupuesto agotado: {pid}; continúa en los dos scrapeos generales diarios")
                    else:
                        logger.info(f"  Sin resultado: {pid}; intento {match['intento']}/{MAX_INTENTOS}")
            except Exception as exc:
                logger.error(f"Error al consultar {key}: {exc}")
                run_errors.append({"grupo": key, "error": str(exc)})
                for match in partidos_grupo:
                    if match['pid'] not in resueltos:
                        registrar_consulta(match, comprobado=False)
                try:
                    await page.goto('about:blank')
                except Exception:
                    pass
    except Exception as exc:
        run_errors.append({"error": str(exc)})
        for match in pendientes:
            if match['pid'] not in resueltos:
                registrar_consulta(match, comprobado=False)
    finally:
        try:
            guardar_intentos(intentos, resueltos)
        except Exception as exc:
            run_errors.append({"error": str(exc)})
        try:
            if browser:
                await browser.close()
            if pw_inst:
                await pw_inst.stop()
        finally:
            _run_finalizar(run_id, 'failed' if run_errors else 'completed',
                           grupos_scrapeados, len(resueltos), run_errors)
    if run_errors:
        raise RuntimeError(f"Fallaron {len(run_errors)} operaciones; consulta los logs de la ejecución")
    logger.info(f"RESUMEN: {len(resueltos)} resultado(s) guardados; {len(grupos)} grupo(s)")
    return len(resueltos)

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

    try:
        n = asyncio.run(actualizar_resultados(headless=args.headless, check_only=args.check))
    except Exception as e:
        logger.error(f"Fallo no controlado en scraper_resultados: {e}", exc_info=True)
        print("RESULT_COUNT=0")
        sys.exit(1)
    # Señal legible por CI: nº de resultados actualizados (0 en --check o sin novedades).
    print(f"RESULT_COUNT={n or 0}")
    sys.exit(0)


if __name__ == "__main__":
    main()
