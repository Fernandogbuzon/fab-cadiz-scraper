import copy
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import scraper_precheck
import scraper_resultados as scraper
import scraper_schedule


NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)
PREVIOUS = "2026-10-16T17:30:00+00:00"


class MemorySupabase:
    """Small query double that applies filters, so result-count tests are meaningful."""

    def __init__(self, tables=None, fail=None, strict_matches=False):
        self.tables = copy.deepcopy(tables or {})
        self.fail = fail
        self.strict_matches = strict_matches
        self.operations = []

    def table(self, name):
        return MemoryQuery(self, name)


class MemoryQuery:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.operation = None
        self.filters = []
        self.rows = []
        self.columns = []

    def select(self, columns):
        self.operation = "select"
        self.columns = [field.strip() for field in columns.split(",")]
        return self

    def eq(self, field, value):
        self.filters.append((field, [value]))
        return self

    def in_(self, field, values):
        self.filters.append((field, values))
        return self

    def upsert(self, rows, **_kwargs):
        self.operation = "upsert"
        self.rows = copy.deepcopy(rows)
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def execute(self):
        self.client.operations.append((self.table, self.operation, self.filters, self.rows))
        if self.client.fail == (self.table, self.operation):
            raise RuntimeError("simulated DB failure")
        table = self.client.tables.setdefault(self.table, [])

        def matches(row):
            return all(row.get(field) in values for field, values in self.filters)

        if self.operation == "select":
            rows = [{field: row[field] for field in self.columns if field in row} for row in table if matches(row)]
            return SimpleNamespace(data=copy.deepcopy(rows))
        if self.operation == "delete":
            self.client.tables[self.table] = [row for row in table if not matches(row)]
        elif self.operation == "upsert":
            if self.table == "matches" and self.client.strict_matches:
                # PostgreSQL validates required INSERT fields before ON CONFLICT.
                required = ("match_key", "group_id", "competition_id", "fecha", "local", "visitante")
                for row in self.rows:
                    if any(row.get(field) is None for field in required):
                        raise RuntimeError("null value in matches column violates not-null constraint")
            for row in self.rows:
                existing = next((stored for stored in table if stored["match_key"] == row["match_key"]), None)
                if existing is None:
                    table.append(copy.deepcopy(row))
                else:
                    existing.update(copy.deepcopy(row))
        return SimpleNamespace(data=[])


def due_match(attempt=3):
    return {
        "pid": "match-a", "match_key": "match-a", "intento": attempt,
        "fecha": "16/10/2026", "hora": "18:00", "local": "ADESA 80", "visitante": "CB Cádiz",
        "estado": "proximo", "es_resultado": False,
        "comp_url": "https://example.test/competition", "comp_slug": "copa",
        "cat_carpeta": "Junior", "fase_carpeta": "Regular", "grupo_carpeta": "A",
        "group_id": "group-a", "competition_id": "comp-a",
    }


def web_match(result=False):
    return {
        "fecha": "16/10/2026", "hora": "18:00", "local": "ADESA 80", "visitante": "CB Cádiz",
        "es_resultado": result, "marcador_local": 70 if result else None,
        "marcador_visitante": 65 if result else None, "pabellon": "Municipal",
    }


class RetryPersistenceTests(unittest.TestCase):
    def test_empty_map_deletes_resolved_retry_from_database(self):
        db = MemorySupabase({"scraper_retries": [{"match_key": "match-a", "intento": 2, "ultimo": PREVIOUS}]})
        with (
            patch.object(scraper, "get_supabase", return_value=db),
            patch.object(scraper, "_intentos_iniciales", {"match-a": {"intentos": 2, "ultimo": PREVIOUS}}),
        ):
            scraper.guardar_intentos({})
        self.assertEqual(db.tables["scraper_retries"], [])
        self.assertEqual([op[1] for op in db.operations], ["delete"])

    def test_confirmed_result_deletes_retry_even_when_not_in_initial_snapshot(self):
        db = MemorySupabase({"scraper_retries": [{"match_key": "match-a", "intento": 1}]})
        with (
            patch.object(scraper, "get_supabase", return_value=db),
            patch.object(scraper, "_intentos_iniciales", {}),
        ):
            scraper.guardar_intentos({}, {"match-a"})
        self.assertEqual(db.tables["scraper_retries"], [])

    def test_unchanged_retry_state_has_no_database_writes(self):
        state = {"match-a": {"intentos": 2, "ultimo": PREVIOUS}}
        db = MemorySupabase()
        with (
            patch.object(scraper, "get_supabase", return_value=db),
            patch.object(scraper, "_intentos_iniciales", copy.deepcopy(state)),
        ):
            scraper.guardar_intentos(state)
        self.assertEqual(db.operations, [])

    def test_ci_database_failure_cannot_fall_back_to_ephemeral_json(self):
        db = MemorySupabase(fail=("scraper_retries", "upsert"))
        local_file = MagicMock()
        with (
            patch.object(scraper, "get_supabase", return_value=db),
            patch.object(scraper, "_intentos_iniciales", {}),
            patch.object(scraper, "INTENTOS_FILE", local_file),
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            self.assertRaisesRegex(RuntimeError, "persistir"),
        ):
            scraper.guardar_intentos({"match-a": {"intentos": 1, "ultimo": PREVIOUS}})
        local_file.write_text.assert_not_called()

    def test_ci_missing_database_fails_persistence(self):
        with (
            patch.object(scraper, "get_supabase", return_value=None),
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            self.assertRaisesRegex(RuntimeError, "Supabase"),
        ):
            scraper.guardar_intentos({})


class ResultWriteTests(unittest.TestCase):
    def pending_db_row(self):
        return {**due_match(), "id": "id-a"}

    def test_failed_result_write_raises_instead_of_reporting_success(self):
        db = MemorySupabase({"matches": [self.pending_db_row()]}, fail=("matches", "upsert"))
        with (
            patch.object(scraper, "get_supabase", return_value=db),
            self.assertRaisesRegex(RuntimeError, "persistir los resultados"),
        ):
            scraper.actualizar_supabase_resultados([web_match(True)], group_id="group-a")
        self.assertFalse(db.tables["matches"][0]["es_resultado"])

    def test_count_includes_only_newly_persisted_results(self):
        old_final = {
            "match_key": "old-final", "fecha": "01/09/2026", "local": "PUEBLO NUEVO", "visitante": "TARIFA",
            "es_resultado": True, "group_id": "group-a", "marcador_local": 50, "marcador_visitante": 40,
        }
        old_web = {**old_final, "marcador_local": 99, "marcador_visitante": 98}
        db = MemorySupabase({"matches": [self.pending_db_row(), old_final]})
        with patch.object(scraper, "get_supabase", return_value=db):
            confirmed = scraper.actualizar_supabase_resultados([web_match(True), old_web], group_id="group-a")

        self.assertEqual(confirmed, {"match-a"})
        self.assertTrue(db.tables["matches"][0]["es_resultado"])
        self.assertEqual(db.tables["matches"][1]["marcador_local"], 50)
        self.assertIn(("es_resultado", [False]), db.operations[0][2])
        self.assertEqual(len(db.operations[1][3]), 1)

    def test_source_without_score_does_not_read_or_write_matches(self):
        db = MemorySupabase()
        with patch.object(scraper, "get_supabase", return_value=db):
            self.assertEqual(scraper.actualizar_supabase_resultados([web_match()], group_id="group-a"), set())
        self.assertEqual(db.operations, [])

    def test_result_upsert_preserves_required_insert_fields_before_on_conflict(self):
        original = self.pending_db_row()
        db = MemorySupabase({"matches": [original]}, strict_matches=True)
        with patch.object(scraper, "get_supabase", return_value=db):
            confirmed = scraper.actualizar_supabase_resultados([web_match(True)], group_id="group-a")
        self.assertEqual(confirmed, {"match-a"})
        payload = db.operations[1][3][0]
        for field in ("match_key", "group_id", "competition_id", "fecha", "local", "visitante"):
            with self.subTest(field=field):
                self.assertEqual(payload[field], original[field])
        self.assertTrue(db.tables["matches"][0]["es_resultado"])

    def test_reversed_source_teams_preserve_database_orientation_and_scores(self):
        original = self.pending_db_row()
        source = {
            **web_match(True), "local": original["visitante"], "visitante": original["local"],
            "marcador_local": 65, "marcador_visitante": 70,
        }
        db = MemorySupabase({"matches": [original]}, strict_matches=True)
        with patch.object(scraper, "get_supabase", return_value=db):
            confirmed = scraper.actualizar_supabase_resultados([source], group_id="group-a")
        self.assertEqual(confirmed, {"match-a"})
        stored = db.tables["matches"][0]
        self.assertEqual((stored["local"], stored["visitante"]), (original["local"], original["visitante"]))
        self.assertEqual((stored["marcador_local"], stored["marcador_visitante"]), (70, 65))

    def test_duplicate_source_rows_do_not_duplicate_match_keys_in_upsert(self):
        db = MemorySupabase({"matches": [self.pending_db_row()]}, strict_matches=True)
        source = web_match(True)
        with patch.object(scraper, "get_supabase", return_value=db):
            confirmed = scraper.actualizar_supabase_resultados([source, dict(source)], group_id="group-a")
        self.assertEqual(confirmed, {"match-a"})
        writes = [op for op in db.operations if op[1] == "upsert"]
        self.assertEqual(len(writes), 1)
        self.assertEqual([row["match_key"] for row in writes[0][3]], ["match-a"])


class ResultPipelineTests(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def pipeline(self, *, attempt=3, source=None, source_error=None, confirmed=None):
        pending = due_match(attempt)
        state = {"match-a": {"intentos": attempt - 1, "ultimo": PREVIOUS}}
        pw = SimpleNamespace(stop=AsyncMock())
        browser = SimpleNamespace(close=AsyncMock())
        page = SimpleNamespace(goto=AsyncMock())
        mocks = {
            "buscar_partidos_pendientes": MagicMock(return_value=[pending]),
            "_intentos_iniciales": state,
            "crear_browser": AsyncMock(return_value=(pw, browser, MagicMock(), page)),
            "scrape_grupo": AsyncMock(return_value=source, side_effect=source_error),
            "scrape_otros_grupos_en_fase": AsyncMock(return_value=[]),
            "actualizar_supabase_resultados": MagicMock(return_value=confirmed or set()),
            "guardar_intentos": MagicMock(),
            "marcar_supabase_estado": MagicMock(),
            "_run_comenzar": MagicMock(return_value="run-a"),
            "_run_finalizar": MagicMock(),
            "utc_now": MagicMock(return_value=NOW),
        }
        with patch.multiple(scraper, **mocks):
            yield SimpleNamespace(**mocks, browser=browser, pw=pw)

    async def test_technical_source_failures_preserve_budget_and_persist_cooldown(self):
        for failure in ([], RuntimeError("Cloudflare timeout")):
            with self.subTest(failure=failure):
                kwargs = {"source": []} if isinstance(failure, list) else {"source_error": failure}
                with self.pipeline(**kwargs) as mocks:
                    with self.assertRaises(RuntimeError):
                        await scraper.actualizar_resultados()
                    saved = mocks.guardar_intentos.call_args.args[0]["match-a"]
                    self.assertEqual(saved, {"intentos": 2, "ultimo": NOW.isoformat()})
                    mocks.actualizar_supabase_resultados.assert_not_called()
                    mocks.marcar_supabase_estado.assert_not_called()
                    self.assertEqual(mocks._run_finalizar.call_args.args[1], "failed")
                    self.assertEqual(mocks._run_finalizar.call_args.args[3], 0)
                    mocks.browser.close.assert_awaited_once()
                    mocks.pw.stop.assert_awaited_once()

    async def test_successful_source_without_score_consumes_one_attempt(self):
        with self.pipeline(source=[web_match()]) as mocks:
            self.assertEqual(await scraper.actualizar_resultados(), 0)
            saved = mocks.guardar_intentos.call_args.args[0]["match-a"]
            self.assertEqual(saved["intentos"], 3)
            self.assertEqual(saved["ultimo"], NOW.isoformat())
            mocks.marcar_supabase_estado.assert_not_called()
            mocks.scrape_otros_grupos_en_fase.assert_not_awaited()
            self.assertEqual(mocks._run_finalizar.call_args.args[1], "completed")

    async def test_budget_exhaustion_marks_awaiting_result_instead_of_postponed(self):
        with self.pipeline(attempt=scraper.MAX_INTENTOS, source=[web_match()]) as mocks:
            self.assertEqual(await scraper.actualizar_resultados(), 0)
            mocks.marcar_supabase_estado.assert_called_once_with("match-a", "esperando_resultado")
            self.assertEqual(mocks.guardar_intentos.call_args.args[0]["match-a"]["intentos"], scraper.MAX_INTENTOS)

    async def test_successful_result_clears_retry_and_counts_one_result(self):
        with self.pipeline(source=[web_match(True)], confirmed={"match-a"}) as mocks:
            self.assertEqual(await scraper.actualizar_resultados(), 1)
            mocks.guardar_intentos.assert_called_once_with({}, {"match-a"})
            self.assertEqual(mocks._run_finalizar.call_args.args[3], 1)
            mocks.marcar_supabase_estado.assert_not_called()

    async def test_result_write_failure_preserves_retry_and_fails_run(self):
        with self.pipeline(source=[web_match(True)]) as mocks:
            mocks.actualizar_supabase_resultados.side_effect = RuntimeError("write failed")
            with self.assertRaises(RuntimeError):
                await scraper.actualizar_resultados()
            saved, resolved = mocks.guardar_intentos.call_args.args
            self.assertEqual(saved["match-a"]["intentos"], 2)
            self.assertEqual(resolved, set())
            self.assertEqual(mocks._run_finalizar.call_args.args[3], 0)

    async def test_browser_startup_failure_refreshes_existing_retry_cooldown(self):
        with self.pipeline() as mocks:
            mocks.crear_browser.side_effect = RuntimeError("Chromium launch failed")
            with self.assertRaises(RuntimeError):
                await scraper.actualizar_resultados()
            saved = mocks.guardar_intentos.call_args.args[0]["match-a"]
            self.assertEqual(saved, {"intentos": 2, "ultimo": NOW.isoformat()})
            mocks.scrape_grupo.assert_not_awaited()
            self.assertEqual(mocks._run_finalizar.call_args.args[1], "failed")

    async def test_retry_persistence_failure_fails_run_after_browser_cleanup(self):
        with self.pipeline(source=[web_match()]) as mocks:
            mocks.guardar_intentos.side_effect = RuntimeError("retry write failed")
            with self.assertRaises(RuntimeError):
                await scraper.actualizar_resultados()
            mocks.browser.close.assert_awaited_once()
            mocks.pw.stop.assert_awaited_once()
            self.assertEqual(mocks._run_finalizar.call_args.args[1], "failed")


class SnapshotReuseTests(unittest.TestCase):
    def test_valid_precheck_snapshot_avoids_duplicate_database_query(self):
        cfg = {"season": "2026-2027", "team_name": "ADESA 80", "match_duration_hours": 1}
        match = due_match()
        match["comp_groups"] = {
            "phase": "Regular", "name": "A",
            "comp_categories": {"slug": "Junior", "competitions": {"slug": "copa", "url": "https://example.test/competition"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "pending.json"
            snapshot.write_text(json.dumps({
                "created_at": NOW.isoformat(), "config": cfg,
                "matches": [match], "retries": {"match-a": {"intentos": 2, "ultimo": PREVIOUS}},
            }), encoding="utf-8")
            with (
                patch.dict(os.environ, {"SCRAPER_PENDING_FILE": str(snapshot), "GITHUB_ACTIONS": "true"}),
                patch.object(scraper, "_CFG", cfg),
                patch.object(scraper, "_intentos_iniciales", {}),
                patch.object(scraper_precheck, "fetch_context") as fetch,
                patch.object(scraper_precheck, "utc_now", return_value=NOW),
                patch.object(scraper_schedule, "utc_now", return_value=NOW),
            ):
                pending = scraper.buscar_partidos_pendientes()
                fetch.assert_not_called()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["intento"], 3)
                self.assertEqual(pending[0]["comp_url"], "https://example.test/competition")
                self.assertEqual(scraper._intentos_iniciales["match-a"]["intentos"], 2)


class AlternatePhaseReadTests(unittest.IsolatedAsyncioTestCase):
    def page_in_competition(self):
        page = MagicMock()
        page.url = "https://example.test/delegacion-competicion-1/copa"
        page.locator.return_value.count = AsyncMock(return_value=0)
        return page

    async def test_failed_phase_selection_raises_without_caching_empty_result(self):
        page = self.page_in_competition()
        cache = {}
        with (
            patch.object(scraper, "obtener_opciones", AsyncMock(side_effect=[
                [{"value": "cat", "text": "Junior"}],
                [{"value": "phase", "text": "Regular"}],
            ])),
            patch.object(scraper, "seleccionar_dropdown", AsyncMock(side_effect=[True, False])),
            patch.object(scraper, "esperar_dropdown_con_opciones", AsyncMock(return_value=True)),
            self.assertRaises(RuntimeError),
        ):
            await scraper.scrape_otros_grupos_en_fase(page, page.url, "Junior", "Regular", "A", cache=cache)
        self.assertEqual(cache, {})

    async def test_failed_group_selection_cannot_cache_a_partially_read_phase(self):
        page = self.page_in_competition()
        cache = {}
        with (
            patch.object(scraper, "obtener_opciones", AsyncMock(side_effect=[
                [{"value": "cat", "text": "Junior"}],
                [{"value": "phase", "text": "Regular"}],
                [{"value": "group-a", "text": "A"}, {"value": "group-b", "text": "B"}],
            ])),
            patch.object(scraper, "seleccionar_dropdown", AsyncMock(side_effect=[True, True, True, False])),
            patch.object(scraper, "esperar_dropdown_con_opciones", AsyncMock(return_value=True)),
            patch.object(scraper, "extraer_partidos_pagina", AsyncMock(return_value=[web_match()])),
            self.assertRaises(RuntimeError),
        ):
            await scraper.scrape_otros_grupos_en_fase(page, page.url, "Junior", "Regular", "A", cache=cache)
        self.assertEqual(cache, {})


if __name__ == "__main__":
    unittest.main()
