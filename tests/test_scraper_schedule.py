import unittest
from datetime import datetime, timedelta, timezone

from scraper_schedule import (
    MADRID,
    kickoff_at,
    parse_timestamp,
    pending_matches,
    retry_due_at,
    utc_now,
)


UTC = timezone.utc


class ScraperScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "team_name": "ADESA 80",
            "match_duration_hours": 1,
            "max_retry_attempts": 11,
            "retry_intervals_minutes": [10, 10, 10, 10, 20, 30, 60, 120, 240],
            "retry_window_hours": 24,
        }
        self.match = {
            "match_key": "partido-1",
            "fecha": "16/10/2026",
            "hora": "18:00",
            "estado": "proximo",
            "es_resultado": False,
            "local": "ADESA 80",
            "visitante": "CB Cadiz",
            "group_id": "grupo-1",
        }
        # 18:00 Madrid in October is 16:00 UTC, estimated end 17:00 UTC.
        self.first_due = datetime(2026, 10, 16, 17, tzinfo=UTC)

    def test_first_query_waits_until_estimated_end_including_equality(self):
        self.assertEqual(retry_due_at(self.match, {}, self.cfg), self.first_due)
        self.assertEqual(
            pending_matches([self.match], {}, self.cfg, self.first_due - timedelta(seconds=1)),
            [],
        )
        pending = pending_matches([self.match], {}, self.cfg, self.first_due)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["intento"], 1)
        self.assertEqual(pending[0]["pid"], "partido-1")
        self.assertEqual(pending[0]["next_check_at"], self.first_due)
        self.assertEqual(pending[0]["dt_inicio"], self.first_due - timedelta(hours=1))
        self.assertEqual(pending[0]["group_id"], "grupo-1")
        self.assertNotIn("pid", self.match)

    def test_delayed_runs_wait_from_last_attempt_without_catchup(self):
        last = self.first_due + timedelta(hours=2)
        retry = {"intentos": 1, "ultimo": last.isoformat()}
        due = last + timedelta(minutes=10)
        self.assertEqual(retry_due_at(self.match, retry, self.cfg), due)
        self.assertEqual(
            pending_matches([self.match], {"partido-1": retry}, self.cfg, due - timedelta(seconds=1)),
            [],
        )
        self.assertEqual(
            pending_matches([self.match], {"partido-1": retry}, self.cfg, due)[0]["intento"],
            2,
        )

    def test_poll_grace_never_advances_the_first_query(self):
        cfg = {**self.cfg, "retry_poll_grace_seconds": 180}
        self.assertEqual(retry_due_at(self.match, {}, cfg), self.first_due)
        self.assertEqual(
            pending_matches([self.match], {}, cfg, self.first_due - timedelta(seconds=1)),
            [],
        )
        self.assertEqual(len(pending_matches([self.match], {}, cfg, self.first_due)), 1)

    def test_poll_grace_allows_next_tick_after_ci_setup_without_immediate_retry(self):
        cfg = {**self.cfg, "retry_poll_grace_seconds": 180}
        # A 17:17 tick read the source at 17:19 after installing dependencies.
        last = self.first_due + timedelta(minutes=19)
        retry = {"intentos": 1, "ultimo": last.isoformat()}
        self.assertEqual(retry_due_at(self.match, retry, cfg), self.first_due + timedelta(minutes=26))
        self.assertEqual(
            pending_matches([self.match], {"partido-1": retry}, cfg, self.first_due + timedelta(minutes=25)),
            [],
        )
        due_at_next_tick = pending_matches(
            [self.match], {"partido-1": retry}, cfg, self.first_due + timedelta(minutes=27)
        )
        self.assertEqual(len(due_at_next_tick), 1)
        self.assertEqual(due_at_next_tick[0]["intento"], 2)

    def test_poll_grace_is_capped_at_three_minutes(self):
        cfg = {**self.cfg, "retry_poll_grace_seconds": 3600}
        retry = {"intentos": 1, "ultimo": self.first_due.isoformat()}
        self.assertEqual(retry_due_at(self.match, retry, cfg), self.first_due + timedelta(minutes=7))

    def test_poll_grace_preserves_intervals_of_five_minutes_or_less(self):
        retry = {"intentos": 1, "ultimo": self.first_due.isoformat()}
        for minutes in (1, 2, 5):
            with self.subTest(minutes=minutes):
                cfg = {**self.cfg, "retry_poll_grace_seconds": 180, "retry_intervals_minutes": [minutes]}
                self.assertEqual(
                    retry_due_at(self.match, retry, cfg), self.first_due + timedelta(minutes=minutes)
                )

    def test_backoff_uses_attempt_count_and_caps_at_last_interval(self):
        for count, minutes in [(1, 10), (4, 10), (5, 20), (6, 30), (7, 60), (8, 120), (9, 240), (10, 240)]:
            with self.subTest(count=count):
                retry = {"intentos": count, "ultimo": self.first_due.isoformat()}
                self.assertEqual(
                    retry_due_at(self.match, retry, self.cfg),
                    self.first_due + timedelta(minutes=minutes),
                )

    def test_legacy_fixed_interval_configuration_is_respected(self):
        cfg = {**self.cfg, "retry_interval_minutes": 17}
        del cfg["retry_intervals_minutes"]
        retry = {"intentos": 2, "ultimo": self.first_due.isoformat()}
        self.assertEqual(retry_due_at(self.match, retry, cfg), self.first_due + timedelta(minutes=17))

    def test_technical_failure_has_cooldown_without_consuming_result_budget(self):
        retry = {"intentos": 0, "ultimo": self.first_due.isoformat()}
        due = self.first_due + timedelta(minutes=10)
        self.assertEqual(retry_due_at(self.match, retry, self.cfg), due)
        self.assertEqual(pending_matches([self.match], {"partido-1": retry}, self.cfg, self.first_due), [])
        self.assertEqual(
            pending_matches([self.match], {"partido-1": retry}, self.cfg, due)[0]["intento"],
            1,
        )

    def test_budget_and_window_stop_targeted_queries_without_changing_match(self):
        exhausted = {"intentos": 11, "ultimo": self.first_due.isoformat()}
        self.assertIsNone(retry_due_at(self.match, exhausted, self.cfg))
        self.assertEqual(
            pending_matches([self.match], {"partido-1": exhausted}, self.cfg, self.first_due + timedelta(hours=1)),
            [],
        )
        deadline = self.first_due + timedelta(hours=24)
        self.assertEqual(len(pending_matches([self.match], {}, self.cfg, deadline)), 1)
        self.assertEqual(pending_matches([self.match], {}, self.cfg, deadline + timedelta(seconds=1)), [])
        near_deadline = {"intentos": 9, "ultimo": (deadline - timedelta(minutes=1)).isoformat()}
        self.assertIsNone(retry_due_at(self.match, near_deadline, self.cfg))
        self.assertEqual(self.match["estado"], "proximo")

    def test_changed_kickoff_resets_attempts_from_before_new_estimated_end(self):
        moved = {**self.match, "hora": "20:00"}
        retry = {"intentos": 11, "ultimo": self.first_due.isoformat()}
        new_due = self.first_due + timedelta(hours=2)
        self.assertEqual(retry_due_at(moved, retry, self.cfg), new_due)
        self.assertEqual(
            pending_matches([moved], {"partido-1": retry}, self.cfg, new_due)[0]["intento"],
            1,
        )

    def test_invalid_retry_timestamps_do_not_permanently_exhaust_matches(self):
        for timestamp in [None, "", "invalid"]:
            with self.subTest(timestamp=timestamp):
                retry = {"intentos": 11, "ultimo": timestamp}
                self.assertEqual(retry_due_at(self.match, retry, self.cfg), self.first_due)
                self.assertEqual(
                    pending_matches([self.match], {"partido-1": retry}, self.cfg, self.first_due)[0]["intento"],
                    1,
                )

    def test_only_selected_team_and_eligible_states_are_queried(self):
        for state in ["proximo", "en_curso", "esperando_resultado"]:
            with self.subTest(state=state):
                self.assertEqual(len(pending_matches([{**self.match, "estado": state}], {}, self.cfg, self.first_due)), 1)
        for state in ["aplazado", "cancelado", "finalizado", "sin_resultado", "", None]:
            with self.subTest(state=state):
                self.assertEqual(pending_matches([{**self.match, "estado": state}], {}, self.cfg, self.first_due), [])
        for change in [{"es_resultado": True}, {"match_key": ""}, {"local": "CB Jerez"}]:
            with self.subTest(change=change):
                self.assertEqual(pending_matches([{**self.match, **change}], {}, self.cfg, self.first_due), [])
        other_team = {**self.match, "local": "CB Jerez"}
        self.assertEqual(
            len(pending_matches([other_team], {}, {**self.cfg, "watch_all_clubs": True}, self.first_due)),
            1,
        )
        visiting_team = {**self.match, "local": "CB Jerez", "visitante": "adesa 80 Junior"}
        self.assertEqual(len(pending_matches([visiting_team], {}, self.cfg, self.first_due)), 1)

    def test_unknown_and_invalid_kickoffs_are_left_for_general_scan(self):
        for date, hour in [
            ("", "18:00"), ("31/2/2026", "18:00"), ("2026-10-16", "18:00"),
            ("16/10/2026", ""), ("16/10/2026", "24:00"), ("16/10/2026", "18:60"),
            ("16/10/2026", "18:00 garbage"), ("16/10/2026", "18:0"),
            ("16/10/2026", "18:00:61"), (None, None),
        ]:
            with self.subTest(date=date, hour=hour):
                match = {**self.match, "fecha": date, "hora": hour}
                self.assertIsNone(kickoff_at(match))
                self.assertEqual(pending_matches([match], {}, self.cfg, self.first_due), [])

    def test_midnight_rollover_and_unpadded_date_with_seconds(self):
        match = {**self.match, "fecha": "5/4/2026", "hora": "23:30:15"}
        due = datetime(2026, 4, 5, 22, 30, 15, tzinfo=UTC)
        self.assertEqual(retry_due_at(match, {}, self.cfg), due)
        self.assertEqual(due.astimezone(MADRID).day, 6)
        self.assertEqual(len(pending_matches([match], {}, self.cfg, due)), 1)

    def test_dst_elapsed_duration_and_ambiguous_or_nonexistent_local_time(self):
        before_spring_change = {**self.match, "fecha": "29/3/2026", "hora": "1:30"}
        self.assertEqual(kickoff_at(before_spring_change), datetime(2026, 3, 29, 0, 30, tzinfo=UTC))
        self.assertEqual(retry_due_at(before_spring_change, {}, self.cfg), datetime(2026, 3, 29, 1, 30, tzinfo=UTC))
        self.assertIsNone(kickoff_at({**self.match, "fecha": "29/3/2026", "hora": "2:30"}))
        autumn_ambiguous = {**self.match, "fecha": "25/10/2026", "hora": "2:30"}
        self.assertEqual(kickoff_at(autumn_ambiguous), datetime(2026, 10, 25, 0, 30, tzinfo=UTC))

    def test_timestamps_normalize_utc_offsets_and_legacy_madrid(self):
        expected = datetime(2026, 10, 16, 17, tzinfo=UTC)
        for value in ["2026-10-16T17:00:00Z", "2026-10-16T19:00:00+02:00", "2026-10-16T19:00:00", expected]:
            with self.subTest(value=value):
                self.assertEqual(parse_timestamp(value), expected)
        self.assertEqual(parse_timestamp("2026-01-16T19:00:00"), datetime(2026, 1, 16, 18, tzinfo=UTC))
        self.assertIsNone(parse_timestamp("2026-03-29T02:30:00"))
        for value in [None, "", "invalid", 123]:
            self.assertIsNone(parse_timestamp(value))
        self.assertEqual(utc_now().tzinfo, UTC)


if __name__ == "__main__":
    unittest.main()
