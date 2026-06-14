# HANDOFF → sesión adesa80-web

Cambios aplicados por la sesión del **scraper** sobre la Supabase **Central** compartida
(`fab-cadiz-central`, ref `dzpkmkqmtdzwmrynzgus`). Este doc es para que la sesión que
trabaja en `adesa80-web` sepa exactamente qué cambió y qué tiene que hacer.

Fecha: 2026-06-14. Consumidor afectado: `adesa80-web` (lee Central vía
`src/lib/supabaseCentral.ts` + `server/routes/partidos.js`, con service key).

---

## 1. Cambios de ESQUEMA ya aplicados en Central (en vivo)

Todos additive + reversibles, registrados en el historial nativo de migraciones de Supabase.

| Migración | Cambio |
|-----------|--------|
| `matches_normalized_fields` | `matches` += `fecha_iso DATE`, `local_slug TEXT`, `visitante_slug TEXT`, `stable_match_key TEXT` (+ índices). Antes NO estaban aplicados. `fecha_iso` backfilled 3558/3582 (24 con fecha no parseable). `local_slug/visitante_slug/stable_match_key` quedan NULL hasta el próximo scrape completo (el scraper los rellena con su `slugify`). |
| `estado_add_cancelado` | `matches.estado` CHECK ahora admite `'cancelado'` (set: proximo, en_curso, finalizado, aplazado, sin_resultado, esperando_resultado, cancelado). |
| `club_team_aliases_priority` | `club_team_aliases` += `priority INT NOT NULL DEFAULT 0` (+ índice). Mayor prioridad gana cuando varios alias hacen match. |
| `004_seed_clubs_logos` | **Logos en BBDD** (ver §3). 38 clubs con `logo_url`, 60 alias. Bucket Storage `team-logos`. |
| `005_logo_alias_fixes` | Alias extra `ciudad de cádiz`/`ciudad de cadiz` → `cbciudaddecadiz` (gap que logoMap.js TODAVÍA tiene). |
| `central_security_hardening` | Seguridad (ver §5). |

**Contrato intacto** (no se renombró/borró nada que la web lee): `matches.{local,visitante,
fecha,hora,marcador_local,marcador_visitante,es_resultado,estado,jornada,pabellon,match_key,
competition_id,group_id}`, `comp_groups.{phase,name}`, `comp_categories.{name,slug}`,
`competitions.{name,slug,season}`. La web usa `matches.select('*')` → las columnas nuevas
llegan solas y se ignoran si no se usan.

---

## 2. Estado actual (conteos en vivo)

- `clubs` = 38 (1 ADESA gestionado `active=true` + 37 rivales `active=false`).
- `club_team_aliases` = 62. Todas con `priority`.
- `clubs.logo_url` poblado en los 38.
- Cobertura logo: **202/255** nombres distintos de equipo resuelven. Los 53 restantes
  son placeholders (`GANADOR n`, `- EQUIPO POR DETERMINAR -`, `FAB CÁDIZ G-A/B`),
  selecciones (ESPAÑA/FRANCIA/...), veteranos (`+35/+40/...`) o equipos sin asset de logo.

---

## 3. LOGOS EN BBDD — lo importante para la web

Hoy la web resuelve logos en cliente con `src/lib/logoMap.js` (patrones substring ordenados
→ `/logos/<file>`). Eso ahora está **portado a la BBDD** reutilizando `clubs` + `club_team_aliases`.

**Modelo:**
- `clubs(id, slug, name, logo_url, team_pattern, active)` — `logo_url` apunta al bucket público.
- `club_team_aliases(club_id, team_name, priority, is_primary)` — `team_name` es un PATRÓN
  substring (igual semántica que `get_club_matches` que ya usa ILIKE `%team_name%`). `priority`
  reproduce el ORDEN de logoMap (antes = mayor prioridad = gana en solapes).

**Algoritmo de resolución (sustituye a `resolveLogoFile`)** — dado un nombre de equipo:
```sql
SELECT c.logo_url, c.slug, c.name
FROM club_team_aliases a
JOIN clubs c ON c.id = a.club_id
WHERE lower($1) LIKE '%' || lower(a.team_name) || '%'
ORDER BY a.priority DESC
LIMIT 1;
```
Probado OK en casos difíciles: `CD UDEA ALGECIRAS`→udea (no algeciras), `GYMNÁSTICA
PORTUENSE`→gymnastica (no portuense), `MAXCOLCHON CB SAN FERNANDO`→cbsanfernando,
`UB JEREZ BCTO`→ubjerez, `ADESA 80 ROJO`→adesa-80, `CB CIUDAD DE CÁDIZ`→cbciudaddecadiz.

**Bucket Storage (público):** `team-logos`. URL pública:
`https://dzpkmkqmtdzwmrynzgus.supabase.co/storage/v1/object/public/team-logos/<archivo>`
(`clubs.logo_url` ya contiene esta URL completa).

**⚠️ Los BYTES de los logos aún NO están subidos** al bucket (el MCP no tiene `storage:write`).
Hasta que se ejecute `scripts/upload_logos.py` en el repo del scraper, esas URLs dan 404.
Mientras tanto la web puede seguir sirviendo `/logos/<file>` desde `public/`. El nombre de
archivo está en `logo_url` (último segmento de la ruta) si se quiere mantener `/logos/<file>`.

---

## 4. TAREA para la sesión de la web — switch logoMap.js → BBDD

Objetivo: que la fuente de verdad de los logos sea la BBDD, no el array hardcoded.

Opción recomendada (**A, build/SSR-time, bajo riesgo**):
1. En SSR/build, una query con la service key (`getCentralClient`) trae
   `clubs(slug,name,logo_url)` + `club_team_aliases(team_name,priority,club_id)`.
2. Construir el equivalente a `LOGO_MAP` ordenado por `priority DESC`: lista de
   `[pattern, logo_url]` (o `[pattern, slug→file]` si se prefiere mantener `/logos/`).
3. Inyectar vía `define:vars` como hoy (`window.__logoMap`), y `getTeamLogo()` /
   `resolveLogoFile()` resuelven por substring + prioridad (misma lógica, datos de BBDD).
4. Mantener `logoMap.js` como fallback hasta verificar; luego deprecar.

Ficheros candidatos a tocar: `src/lib/logoMap.js` (→ generado/derivado de BBDD o nuevo
`src/lib/logoResolver`), `src/scripts/resultados.ts` (`getTeamLogo`), los Astro/rutas que
inyectan `__logoMap` (`PartidosCarousel.astro`, `index.astro`, `resultados.astro`,
`server/routes/*`), `MainSponsorsStrip.astro` si aplica.

Decisión pendiente: usar `logo_url` (bucket) directamente — requiere subir los bytes (§3) —
o seguir con `/logos/<file>` (ya desplegado) y solo tomar de BBDD el patrón→archivo.
Recomendado: subir bytes y usar `logo_url` (desacopla del deploy).

Verificación web: `npm run typecheck` / `npm run build`; comprobar logos en claro y oscuro.

---

## 5. Seguridad Central endurecida (info; sin impacto en la web)

Advisors: 13 hallazgos → 1 (solo queda `pg_trgm in public`, benigno).
- `v_club_summary` y `v_scraper_history` → `security_invoker = true` (antes DEFINER, ERROR).
- Las 10 RPCs del scraper: `search_path` fijado a `public` + `REVOKE EXECUTE ... FROM
  PUBLIC, anon, authenticated` + `GRANT ... TO service_role`. La web NO llama esas RPCs de
  Central (solo usa `.from(...)` con service key y RPCs `bus_*` de SU propio proyecto), así
  que no afecta. El SQL exacto está en el repo del scraper: `sql/006_central_security_hardening.sql`.

---

## 6. ⚠️ Edité 1 fichero EN EL REPO adesa80-web (sin commit)

`adesa80-web/sql/01_central_schema.sql`: las 2 vistas `v_club_summary` y `v_scraper_history`
ahora llevan `WITH (security_invoker = true)` + un comentario. Es para que el source de la web
refleje el estado real de Central. Nada más tocado en la web.
Nota: para reproducir Central desde cero hay que aplicar también el `sql/006` del scraper
(search_path + grants), que NO está dentro de `01`.

---

## 7. Cambios en el repo del SCRAPER (FYI, no es la web)

Rama worktree, sin desplegar. `scraper_resultados.py` (fix cross-competition, caché por fase,
RESULT_COUNT, logs fuzzy), `scraper_competicion.py` (standings upsert, content_hashes vía RPC),
`.github/workflows/2-disparador.yml` (excluye `sin_resultado`), nuevos `sql/002–006`,
`scripts/gen_clubs_seed.py`, `scripts/upload_logos.py`.

---

## Resumen para pegar en la otra sesión

> Central (`fab-cadiz-central`) ya tiene: columnas nuevas en `matches`
> (`fecha_iso/local_slug/visitante_slug/stable_match_key`), `estado` admite `cancelado`,
> `club_team_aliases.priority`, y LOGOS en BBDD (38 `clubs.logo_url` + 62 alias con prioridad,
> bucket público `team-logos`). Resolución logo = alias substring de mayor `priority` →
> `clubs.logo_url`. Tarea web: sustituir `logoMap.js` por esa resolución desde BBDD (datos en
> SSR/build, misma lógica de substring+prioridad), manteniendo fallback. Bytes de logo aún sin
> subir al bucket → hasta entonces usar `/logos/<file>`. Editado en la web (sin commit):
> `sql/01_central_schema.sql` (2 vistas → `security_invoker=true`).
