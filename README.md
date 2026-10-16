# FAB Cádiz scraper

Scraping de calendarios y resultados de las competiciones de `team_config.json`,
con persistencia en Supabase Central. No necesita servicios nuevos ni cambios de
esquema para la programación adaptativa.

## Plan de mejora aplicado

1. Un único detector y política de horarios para CI y el scraper de resultados.
2. Consulta ligera antes de instalar dependencias o abrir Chromium.
3. Seguimiento frecuente cerca del final previsto y progresivamente más espaciado.
4. Estado de reintentos persistente, limpieza de resueltos y errores visibles.
5. Dos scrapeos completos diarios, con horario de Madrid y ajuste automático DST.
6. Evitar selecciones de categorías/fases/grupos que ya están seleccionados.
7. Pruebas de regresión de horarios, persistencia y fallos; CI en cada PR a `main`.

## Horarios y seguimiento

Los horarios de los workflows usan `timezone: Europe/Madrid`:

| Trabajo | Programación |
| --- | --- |
| Calendario completo | Cada día a las **12:17** y **23:47** |
| Detector ligero | Cada 10 minutos, de **07:07 a 01:57**, todos los días |
| Navegador de resultados | Solo cuando el detector encuentra consultas vencidas |

La primera consulta espera hasta **hora publicada + `match_duration_hours`**
(actualmente una hora). El comienzo del partido se deduce de su horario publicado;
la fuente no proporciona confirmación de inicio real ni de duración efectiva.
Los partidos sin fecha/hora válidas se recogen en los scrapeos generales.

Se filtran la temporada actual, el club configurado y la ventana reciente antes
de descargar filas. `watch_all_clubs` amplía el seguimiento a todos los clubes.
La paginación evita perder partidos cuando la API limita el tamaño de la respuesta.

Tras una lectura correcta sin resultado, los intervalos nominales son:
**10, 10, 10, 10, 20, 30, 60, 120 y 240 minutos**; el último se repite hasta un
máximo de **11 consultas**, dentro de las **24 horas** siguientes al final estimado.
Los intervalos se calculan desde la última consulta, sin acumular intentos perdidos.
Una tolerancia configurable de 180 segundos permite aprovechar el siguiente tick
aunque la instalación o el navegador retrasen la consulta; nunca adelanta la primera
consulta y mantiene al menos cinco minutos entre reintentos normales.

Ejemplo sin retrasos ni tolerancia: para un inicio a las 18:00 y duración de una
hora, las consultas serían a partir de 19:00, 19:10, 19:20, 19:30, 19:40, 20:00,
20:30, 21:30, 23:30, 03:30 y 07:30. Solo se ejecutan dentro del horario del detector;
los scrapeos generales también pueden obtener el resultado y detener el seguimiento.
GitHub Actions puede retrasar ejecuciones: estos horarios son objetivos de programación,
no una garantía de latencia. [Comportamiento oficial de schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Al guardar un resultado se borran sus reintentos. Un fallo de red, navegador, grupo
vacío o escritura en base de datos conserva el presupuesto y aplica una espera.
Agotar el presupuesto deja el partido en `esperando_resultado`; nunca se deduce
`aplazado` por el resultado de otros partidos. Los dos generales diarios siguen
recogiendo resultados tardíos. Cambiar la hora publicada reinicia los reintentos.

## Coste y fiabilidad

- Dos pasadas generales al día frente a las cuatro anteriores: **50 % menos pasadas**.
- Un solo job por detector, sin setup de Python, pip ni Chromium cuando no hay trabajo.
- El navegador reutiliza durante 15 minutos la consulta del precheck; no repite el detector.
- Una lectura por grupo agrupa los partidos pendientes. Las búsquedas en grupos alternativos
  se limitan al primer y último intento, con caché por fase durante la ejecución.
- No hay commits automáticos de estado. Supabase es la fuente de reintentos de CI;
  un fallo de persistencia hace fallar la ejecución, evitando reinicios silenciosos.
- Los workflows comparten una cola serial (`queue: max`) para evitar escrituras simultáneas
  y conservar los scrapeos generales pendientes. [Colas de GitHub Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
- La huella de contenido solo se confirma después de persistir el grupo correctamente.
  La versión nueva de la huella fuerza una primera sincronización para reparar posibles
  escrituras incompletas de la versión anterior.

La frecuencia de comprobaciones ligeras aumenta para reducir la espera de resultados;
el ahorro se concentra en las pasadas completas, navegadores y consultas duplicadas.
El tiempo total depende de los partidos y de cuándo publique la federación el marcador.

## Uso local

Requiere Python 3.12+, dependencias de `requirements.txt`, Chromium de Playwright y
`CENTRAL_SUPABASE_URL` / `CENTRAL_SUPABASE_SERVICE_KEY` (o sus alias `SUPABASE_*`).
Los scrapers cargan `.env`; el precheck ligero lee exclusivamente variables de entorno.

```sh
pip install -r requirements.txt
playwright install --with-deps chromium
python scraper_resultados.py --check
python scraper_resultados.py
python scraper_competicion.py
python -m unittest discover -s tests -v
```

`--check` no abre el navegador ni modifica el estado. `--reset` limpia los reintentos.
`intentos_resultados.json` queda como respaldo local; `.scraper-pending.json` es una
instantánea efímera de CI y no contiene credenciales.
