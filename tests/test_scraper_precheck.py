import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import scraper_precheck as precheck


NOW = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)
CONFIG = {
    "season": "2026-2027",
    "team_name": "ADESA 80",
    "match_duration_hours": 1,
    "retry_window_hours": 24,
}


class Response(io.BytesIO):
    def __init__(self, rows, content_range=""):
        super().__init__(json.dumps(rows).encode("utf-8"))
        self.headers = {"Content-Range": content_range}


class RestRowsTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "CENTRAL_SUPABASE_URL": "https://example.invalid/",
            "CENTRAL_SUPABASE_SERVICE_KEY": "test-service-key",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_reads_all_rows_when_server_page_cap_is_below_requested_limit(self):
        pages = [
            Response([{"match_key": "a"}, {"match_key": "b"}], "0-1/3"),
            Response([{"match_key": "c"}], "2-2/3"),
        ]
        with patch.object(precheck, "urlopen", side_effect=pages) as request:
            rows = precheck.rest_rows("matches", {"select": "match_key", "es_resultado": "eq.false"})

        self.assertEqual([row["match_key"] for row in rows], ["a", "b", "c"])
        self.assertEqual(request.call_count, 2)
        queries = [parse_qs(urlparse(call.args[0].full_url).query) for call in request.call_args_list]
        self.assertEqual([query["offset"] for query in queries], [["0"], ["2"]])
        for call, query in zip(request.call_args_list, queries):
            self.assertEqual(query["es_resultado"], ["eq.false"])
            self.assertEqual(query["order"], ["match_key.asc"])
            self.assertEqual(call.args[0].get_header("Prefer"), "count=exact")
            self.assertEqual(call.kwargs["timeout"], 20)

    def test_exact_total_avoids_an_extra_empty_request(self):
        rows = [{"match_key": str(index)} for index in range(precheck.PAGE_SIZE)]
        with patch.object(precheck, "urlopen", return_value=Response(rows, "0-499/500")) as request:
            self.assertEqual(precheck.rest_rows("matches", {}), rows)
        request.assert_called_once()

    def test_missing_total_reads_until_empty_page_even_with_short_pages(self):
        pages = [Response([{"match_key": "a"}], "0-0/*"), Response([])]
        with patch.object(precheck, "urlopen", side_effect=pages) as request:
            self.assertEqual(precheck.rest_rows("matches", {}), [{"match_key": "a"}])
        self.assertEqual(request.call_count, 2)
        last_query = parse_qs(urlparse(request.call_args.args[0].full_url).query)
        self.assertEqual(last_query["offset"], ["1"])

    def test_network_failure_propagates_instead_of_becoming_no_pending(self):
        with patch.object(precheck, "urlopen", side_effect=URLError("unavailable")):
            with self.assertRaises(URLError):
                precheck.rest_rows("matches", {})

    def test_unexpected_response_fails(self):
        with patch.object(precheck, "urlopen", return_value=Response({"message": "failure"})):
            with self.assertRaisesRegex(RuntimeError, "Respuesta inesperada"):
                precheck.rest_rows("matches", {})

    def test_missing_configuration_fails_without_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(precheck, "urlopen") as request:
            with self.assertRaisesRegex(RuntimeError, "no configurado"):
                precheck.rest_rows("matches", {})
        request.assert_not_called()


class FetchContextTests(unittest.TestCase):
    def test_spring_dst_lookback_includes_every_still_eligible_match_date(self):
        # The spring change removes one local hour. A real 25-hour window
        # starts on March 28, although local wall-clock subtraction says 29.
        now = datetime(2026, 3, 30, 1, 30, tzinfo=precheck.MADRID)
        with patch.object(precheck, "rest_rows", return_value=[]) as read:
            precheck.fetch_context(CONFIG, now)
        self.assertEqual(
            read.call_args.args[1]["and"],
            "(fecha_iso.gte.2026-03-28,fecha_iso.lte.2026-03-30)",
        )

    def test_filters_season_local_dates_team_and_retry_keys(self):
        matches = [{"match_key": "season-a"}, {"match_key": "season-b"}]
        retry_rows = [{"match_key": "season-b", "intento": 2, "ultimo": NOW.isoformat()}]
        with patch.object(precheck, "rest_rows", side_effect=[matches, retry_rows]) as read:
            found, retries = precheck.fetch_context(CONFIG, NOW)

        self.assertEqual(found, matches)
        self.assertEqual(retries, {"season-b": {"intentos": 2, "ultimo": NOW.isoformat()}})
        table, params = read.call_args_list[0].args
        self.assertEqual(table, "matches")
        self.assertEqual(params["competitions.season"], "eq.2026-2027")
        self.assertEqual(params["es_resultado"], "eq.false")
        self.assertEqual(params["estado"], "in.(proximo,en_curso,esperando_resultado)")
        self.assertEqual(params["and"], "(fecha_iso.gte.2026-10-16,fecha_iso.lte.2026-10-17)")
        self.assertEqual(params["or"], "(local.ilike.*ADESA*,visitante.ilike.*ADESA*)")
        self.assertEqual(read.call_args_list[1].args, ("scraper_retries", {
            "select": "match_key,intento,ultimo",
            "match_key": 'in.("season-a","season-b")',
        }))

    def test_watch_all_clubs_removes_team_filter_and_skips_retries_when_empty(self):
        with patch.object(precheck, "rest_rows", return_value=[]) as read:
            self.assertEqual(precheck.fetch_context({**CONFIG, "watch_all_clubs": True}, NOW), ([], {}))
        read.assert_called_once()
        self.assertNotIn("or", read.call_args.args[1])

    def test_retry_requests_are_bounded_and_deduplicate_match_keys(self):
        keys = [f"match-{index:02d}" for index in range(21)]
        matches = [{"match_key": key} for key in keys + [keys[0]]]
        with patch.object(precheck, "rest_rows", side_effect=[matches, [], []]) as read:
            precheck.fetch_context(CONFIG, NOW)
        self.assertEqual(read.call_count, 3)
        filters = [call.args[1]["match_key"] for call in read.call_args_list[1:]]
        requested_keys = [json.loads("[" + value[4:-1] + "]") for value in filters]
        self.assertEqual([len(batch) for batch in requested_keys], [20, 1])
        self.assertEqual([key for batch in requested_keys for key in batch], keys)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "pending.json"
        self.snapshot = {
            "created_at": (NOW - timedelta(minutes=3)).isoformat(),
            "config": dict(CONFIG),
            "matches": [{"match_key": "match-a"}],
            "retries": {"match-a": {"intentos": 1, "ultimo": NOW.isoformat()}},
        }
        self.environment = patch.dict(os.environ, {"SCRAPER_PENDING_FILE": str(self.path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_snapshot(self, **changes):
        self.path.write_text(json.dumps({**self.snapshot, **changes}), encoding="utf-8")

    def test_fresh_matching_snapshot_avoids_duplicate_database_reads(self):
        self.write_snapshot()
        with patch.object(precheck, "utc_now", return_value=NOW), patch.object(precheck, "fetch_context") as fetch:
            self.assertEqual(precheck.load_context(dict(CONFIG)), (self.snapshot["matches"], self.snapshot["retries"]))
        fetch.assert_not_called()

    def test_stale_future_or_different_config_refetches(self):
        variants = [
            {"created_at": (NOW - timedelta(minutes=15)).isoformat()},
            {"created_at": (NOW + timedelta(seconds=1)).isoformat()},
            {"config": {**CONFIG, "season": "2025-2026"}},
            {"config": {**CONFIG, "watch_all_clubs": True}},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                self.write_snapshot(**changes)
                with patch.object(precheck, "utc_now", return_value=NOW), patch.object(precheck, "fetch_context", return_value=([], {})) as fetch:
                    self.assertEqual(precheck.load_context(CONFIG), ([], {}))
                fetch.assert_called_once_with(CONFIG)

    def test_missing_snapshot_fetches_current_database_state(self):
        with patch.object(precheck, "fetch_context", return_value=([], {})) as fetch:
            self.assertEqual(precheck.load_context(CONFIG), ([], {}))
        fetch.assert_called_once_with(CONFIG)

    def test_main_with_no_due_matches_outputs_false_without_snapshot(self):
        (Path(self.directory.name) / "team_config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        output = Path(self.directory.name) / "output"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
                patch.object(precheck, "SCRIPT_DIR", Path(self.directory.name)), \
                patch.object(precheck, "utc_now", return_value=NOW), \
                patch.object(precheck, "fetch_context", return_value=([], {})), \
                patch("sys.stdout", new_callable=io.StringIO):
            precheck.main()
        self.assertEqual(output.read_text(encoding="utf-8"), "pendientes=false\nnum=0\n")
        self.assertFalse(self.path.exists())

    def test_main_database_failure_does_not_output_successful_empty_state(self):
        (Path(self.directory.name) / "team_config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        output = Path(self.directory.name) / "output"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
                patch.object(precheck, "SCRIPT_DIR", Path(self.directory.name)), \
                patch.object(precheck, "fetch_context", side_effect=URLError("unavailable")):
            with self.assertRaises(URLError):
                precheck.main()
        self.assertFalse(output.exists())
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
