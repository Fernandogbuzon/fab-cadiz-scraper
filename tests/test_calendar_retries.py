import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import scraper_competicion as calendar
from scraper_common import seasonal_match_key


class CalendarQuery:
    def __init__(self, database, table):
        self.database = database
        self.table = table
        self.columns = None
        self.deleted = False
        self.keys = []
        self.filters = []
        self.limit_value = None

    @property
    def not_(self):
        return self

    def select(self, columns):
        self.columns = columns
        return self

    def delete(self):
        self.deleted = True
        return self

    def in_(self, column, values):
        self.keys = list(values)
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def upsert(self, rows, **kwargs):
        return self

    def execute(self):
        if self.table == "content_hashes":
            return SimpleNamespace(data=[{"content_hash": self.database.stored_hash}] if self.database.stored_hash else [])
        if self.table == "scraper_retries" and self.deleted:
            self.database.events.append(("retry-delete", self.keys))
            if self.database.fail_delete:
                raise RuntimeError("retry storage unavailable")
            self.database.deleted_batches.append(self.keys)
            return SimpleNamespace(data=[])
        if self.columns == "match_key, estado, hora":
            return SimpleNamespace(data=[row for row in self.database.previous if row["match_key"] in self.keys])
        if self.table == "matches" and self.columns == "match_key" and self.database.fail_cleanup:
            raise RuntimeError("match cleanup unavailable")
        if self.table == "standings":
            self.database.events.append(("standings", None))
            if self.database.fail_standings:
                raise RuntimeError("standings storage unavailable")
        return SimpleNamespace(data=[{"match_key": key} for key in self.database.current_keys])


class CalendarRpc:
    def __init__(self, database, params):
        self.database = database
        self.params = params

    def execute(self):
        self.database.events.append(("hash-stamp", None))
        if self.database.fail_hash:
            raise RuntimeError("hash storage unavailable")
        self.database.stored_hash = self.params["p_new_hash"]
        return SimpleNamespace(data=True)


class CalendarDatabase:
    def __init__(self, previous):
        self.previous = previous
        self.current_keys = []
        self.written = []
        self.events = []
        self.deleted_batches = []
        self.fail_delete = False
        self.fail_upsert = False
        self.fail_cleanup = False
        self.fail_standings = False
        self.fail_hash = False
        self.stored_hash = None
        self.queries = []
        self.rpc_calls = []

    def table(self, name):
        query = CalendarQuery(self, name)
        self.queries.append(query)
        return query

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return CalendarRpc(self, params)

    def write_matches(self, database, rows):
        self.events.append(("upsert", None))
        if self.fail_upsert:
            raise RuntimeError("match storage unavailable")
        self.written = rows
        self.current_keys = [row["match_key"] for row in rows]
        return True


class CalendarRetryTests(unittest.TestCase):
    def make_match(self, visitor="CB Cadiz", hour="16:00"):
        return {
            "fecha": "16/10/2026", "hora": hour, "local": "ADESA 80", "visitante": visitor,
            "es_resultado": False, "marcador_local": None, "marcador_visitante": None,
        }

    def previous(self, match, hour="18:00", state="esperando_resultado"):
        return {
            "match_key": seasonal_match_key(calendar.SEASON, "copa", match["fecha"], match["local"], match["visitante"], "Junior"),
            "hora": hour,
            "estado": state,
        }

    def save(self, database, matches, unchanged=False, standings=None):
        with patch.object(calendar, "get_supabase", return_value=database), \
             patch.object(calendar, "_upsert_competition", return_value="competition-id"), \
             patch.object(calendar, "_upsert_category", return_value="category-id"), \
             patch.object(calendar, "_upsert_group", return_value="group-id"), \
             patch.object(calendar, "_grupo_sin_cambios", return_value=unchanged), \
             patch.object(calendar, "logger"), \
             patch.object(calendar, "_upsert_matches_batch", side_effect=database.write_matches):
            return calendar.guardar_supabase(matches, standings or {}, "Junior", "Grupo A", "Regular", "Copa", "copa", "https://example.test/copa")

    def test_changed_hour_resets_budget_in_both_directions_and_reopens_waiting_matches(self):
        earlier = self.make_match("CB Earlier", "16:00")
        later = self.make_match("CB Later", "20:00")
        database = CalendarDatabase([self.previous(earlier), self.previous(later, state="aplazado")])
        self.assertTrue(self.save(database, [earlier, later]))
        self.assertEqual({row["estado"] for row in database.written}, {"proximo"})
        self.assertEqual(set(database.deleted_batches[0]), {row["match_key"] for row in database.previous})
        self.assertEqual([event[0] for event in database.events], ["retry-delete", "upsert", "hash-stamp"])

    def test_unchanged_hour_preserves_status_and_budget_and_new_matches_have_no_retry_to_clear(self):
        unchanged = self.make_match(hour="18:00")
        new = self.make_match("CB New")
        database = CalendarDatabase([self.previous(unchanged, state="aplazado")])
        self.save(database, [unchanged, new])
        self.assertEqual(database.written[0]["estado"], "aplazado")
        self.assertEqual(database.written[1]["estado"], "proximo")
        self.assertEqual(database.deleted_batches, [])

    def test_cancelled_status_survives_changed_hour(self):
        match = self.make_match()
        database = CalendarDatabase([self.previous(match, state="cancelado")])
        self.save(database, [match])
        self.assertEqual(database.written[0]["estado"], "cancelado")

    def test_retry_deletion_is_bounded_to_changed_match_keys_and_batches_of_fifty(self):
        matches = [self.make_match(f"CB {index}") for index in range(51)]
        previous = [self.previous(match) for match in matches]
        unrelated = {"match_key": "unrelated-match", "hora": "18:00", "estado": "esperando_resultado"}
        database = CalendarDatabase(previous + [unrelated])
        self.save(database, matches)
        self.assertEqual([len(batch) for batch in database.deleted_batches], [50, 1])
        self.assertEqual({key for batch in database.deleted_batches for key in batch}, {row["match_key"] for row in previous})

    def test_failed_match_upsert_resets_only_changed_retry_and_never_stamps_hash(self):
        match = self.make_match()
        database = CalendarDatabase([self.previous(match)])
        database.fail_upsert = True
        with self.assertRaises(RuntimeError):
            self.save(database, [match])
        self.assertEqual(database.deleted_batches, [[database.previous[0]["match_key"]]])
        self.assertEqual(database.rpc_calls, [])
        self.assertEqual(database.stored_hash, None)

    def test_retry_deletion_failure_is_reported(self):
        match = self.make_match()
        database = CalendarDatabase([self.previous(match)])
        database.fail_delete = True
        with self.assertRaises(RuntimeError):
            self.save(database, [match])
        self.assertEqual(database.written, [])
        self.assertEqual(database.rpc_calls, [])

    def test_unchanged_group_hash_skips_match_and_retry_writes(self):
        match = self.make_match()
        database = CalendarDatabase([self.previous(match)])
        self.assertFalse(self.save(database, [match], unchanged=True))
        self.assertEqual(database.events, [])

    def test_hash_comparison_is_read_only_and_scoped_to_the_group(self):
        database = CalendarDatabase([])
        database.stored_hash = "previous-hash"
        self.assertTrue(calendar._grupo_sin_cambios(database, "group-id", "previous-hash"))
        self.assertFalse(calendar._grupo_sin_cambios(database, "group-id", "new-hash"))
        self.assertEqual(database.rpc_calls, [])
        self.assertEqual(database.stored_hash, "previous-hash")
        query = database.queries[-1]
        self.assertEqual(query.columns, "content_hash")
        self.assertEqual(query.filters, [("entity_type", "group"), ("entity_id", "group-id")])
        self.assertEqual(query.limit_value, 1)

    def test_hash_stamps_after_standings_and_all_other_writes(self):
        match = self.make_match()
        database = CalendarDatabase([self.previous(match)])
        self.save(database, [match], standings={"clasificacion": [{"equipo": "ADESA 80", "posicion": 1}]})
        self.assertEqual([event[0] for event in database.events], ["retry-delete", "upsert", "standings", "standings", "hash-stamp"])
        self.assertEqual(database.stored_hash, calendar._hash_partidos([match]))

    def test_cleanup_or_standings_failure_never_stamps_hash(self):
        for failure in ["fail_cleanup", "fail_standings"]:
            with self.subTest(failure=failure):
                match = self.make_match()
                database = CalendarDatabase([self.previous(match)])
                setattr(database, failure, True)
                with self.assertRaises(RuntimeError):
                    self.save(database, [match], standings={"clasificacion": [{"equipo": "ADESA 80"}]})
                self.assertEqual(database.rpc_calls, [])

    def test_hash_stamp_failure_keeps_data_and_allows_safe_repetition(self):
        match = self.make_match()
        database = CalendarDatabase([self.previous(match)])
        database.fail_hash = True
        self.assertTrue(self.save(database, [match]))
        self.assertEqual(len(database.written), 1)
        self.assertIsNone(database.stored_hash)


class CalendarFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_is_visible_while_other_competitions_finish(self):
        urls = [f"https://example.test/competition-{index}" for index in range(3)]
        page = SimpleNamespace(goto=AsyncMock())
        browser = SimpleNamespace(close=AsyncMock())
        playwright = SimpleNamespace(stop=AsyncMock())
        with patch.object(calendar, "COMPETITIONS_FROM_DB", False), \
             patch.object(calendar, "COMPETICIONES", urls), \
             patch.object(calendar, "logger"), \
             patch.object(calendar, "_run_comenzar", return_value="run-id"), \
             patch.object(calendar, "_run_finalizar") as finalize, \
             patch.object(calendar, "crear_browser", new=AsyncMock(return_value=(playwright, browser, None, page))), \
             patch.object(calendar, "resolver_urls_actuales", new=AsyncMock(return_value=urls)), \
             patch.object(calendar, "pausa", new=AsyncMock()), \
             patch.object(calendar, "scrape_una_competicion", new=AsyncMock(side_effect=[
                 (2, "first", 1, 0), RuntimeError("second competition failed"), (3, "third", 1, 0),
             ])) as scrape:
            self.assertTrue(await calendar.scrape_todas())
        self.assertEqual(scrape.await_count, 3)
        self.assertEqual([call.args[1] for call in scrape.await_args_list], urls)
        self.assertEqual(finalize.call_args.args, ("run-id", "failed"))
        self.assertEqual(finalize.call_args.kwargs["comps"], 3)
        self.assertEqual(finalize.call_args.kwargs["matches_upserted"], 5)
        self.assertEqual(finalize.call_args.kwargs["groups_scraped"], 2)
        self.assertEqual(finalize.call_args.kwargs["errors"], [{"url": urls[1], "error": "second competition failed"}])
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_initial_page_load_failure_raises_instead_of_empty_success(self):
        page = SimpleNamespace(goto=AsyncMock(), url="https://example.test/competition")
        with patch.object(calendar, "logger"), \
             patch.object(calendar, "esperar_pagina", new=AsyncMock(return_value=False)):
            with self.assertRaisesRegex(RuntimeError, "No se pudo cargar"):
                await calendar.scrape_una_competicion(page, page.url)

    async def test_missing_categories_raises_instead_of_empty_success(self):
        page = SimpleNamespace(goto=AsyncMock(), url="https://example.test/competition")
        with patch.object(calendar, "logger"), \
             patch.object(calendar, "esperar_pagina", new=AsyncMock(return_value=True)), \
             patch.object(calendar, "esperar_dropdown_con_opciones", new=AsyncMock()), \
             patch.object(calendar, "pausa", new=AsyncMock()), \
             patch.object(calendar, "obtener_nombre_competicion", new=AsyncMock(return_value="Copa")), \
             patch.object(calendar, "obtener_opciones", new=AsyncMock(return_value=[{"value": "", "text": "Seleccionar"}])):
            with self.assertRaisesRegex(RuntimeError, "no tiene categorías"):
                await calendar.scrape_una_competicion(page, page.url)


if __name__ == "__main__":
    unittest.main()
