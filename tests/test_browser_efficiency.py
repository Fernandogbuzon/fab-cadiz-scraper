import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import scraper_competicion


class DropdownEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_index_discovers_new_provincial_competitions_and_renews_ids(self):
        base = scraper_competicion.PROVINCE_BASE_URL
        page = AsyncMock()
        page.eval_on_selector_all.return_value = [
            "/cadiz/delegacion-competicion-2/copa", "/cadiz/delegacion-competicion-3/premini",
            "/cadiz/delegacion-competicion-3/premini#calendario",
            "/malaga/delegacion-competicion-4/otra", "https://otro.test/cadiz/delegacion-competicion-5/otra",
        ]
        with patch.dict(scraper_competicion._CFG, discover_club_competitions=True):
            urls = await scraper_competicion.resolver_urls_actuales(page, [base + "/delegacion-competicion-1/copa"])
        self.assertEqual(urls, [base + "/delegacion-competicion-2/copa", base + "/delegacion-competicion-3/premini"])

    def test_discovery_preserves_both_squads_and_excludes_unrelated_names_and_old_dates(self):
        partidos = [
            {"fecha": "21/11/2026", "local": "ADESA 80 VERDE", "visitante": "ADESA80 BLANCO"},
            {"fecha": "21/11/2025", "local": "ADESA 80 ROJO", "visitante": "CB Cádiz"},
            {"fecha": "21/11/2026", "local": "ADESA 800", "visitante": "OTRADESA 80"},
        ]
        self.assertEqual(scraper_competicion.nombres_club_del_grupo(partidos, True), {"ADESA 80 VERDE", "ADESA80 BLANCO"})

    def test_alias_registration_preserves_primary_alias_and_is_retryable(self):
        sb = MagicMock()
        clubs, aliases = MagicMock(), MagicMock()
        sb.table.side_effect = {"clubs": clubs, "club_team_aliases": aliases}.__getitem__
        clubs.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = {"id": "club"}
        known = set()
        execute = aliases.upsert.return_value.execute
        execute.side_effect = [RuntimeError("offline"), None]
        with patch.object(scraper_competicion, "get_supabase", return_value=sb):
            with self.assertRaises(RuntimeError):
                scraper_competicion.registrar_aliases_club({"ADESA 80 A", "ADESA 80 B"}, known)
            self.assertEqual(known, set())
            scraper_competicion.registrar_aliases_club({"ADESA 80 A", "ADESA 80 B"}, known)
            scraper_competicion.registrar_aliases_club({"ADESA 80 B"}, known)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(aliases.upsert.call_args.args[0], [
            {"club_id": "club", "team_name": "ADESA 80 A"}, {"club_id": "club", "team_name": "ADESA 80 B"},
        ])
        self.assertTrue(aliases.upsert.call_args.kwargs["ignore_duplicates"])

    def make_page(self, selected):
        page = MagicMock()
        page.evaluate = AsyncMock()
        page.wait_for_selector = AsyncMock()
        page.select_option = AsyncMock()
        page.locator.return_value.input_value = AsyncMock(return_value=selected)
        return page

    async def test_current_selection_does_not_wait_for_a_navigation(self):
        # One-option categories and the first phase/group already have this value.
        page = self.make_page("selected")
        with (
            patch.object(scraper_competicion, "esperar_pagina", AsyncMock(return_value=True)),
            patch.object(scraper_competicion, "pausa", AsyncMock()),
            patch.object(scraper_competicion.asyncio, "sleep", AsyncMock()),
        ):
            result = await scraper_competicion.seleccionar_dropdown(
                page, "select", "categories", "selected"
            )

        self.assertTrue(result)
        page.expect_navigation.assert_not_called()
        page.select_option.assert_not_awaited()

    async def test_changed_selection_still_waits_for_aspnet_postback(self):
        page = self.make_page("old")
        page.locator.return_value.input_value.side_effect = ["old", "new"]
        with (
            patch.object(scraper_competicion, "esperar_pagina", AsyncMock(return_value=True)),
            patch.object(scraper_competicion, "pausa", AsyncMock()),
            patch.object(scraper_competicion.asyncio, "sleep", AsyncMock()),
        ):
            result = await scraper_competicion.seleccionar_dropdown(
                page, "select", "categories", "new"
            )

        self.assertTrue(result)
        page.expect_navigation.assert_called_once()
        page.select_option.assert_awaited_once_with("select", "new")


if __name__ == "__main__":
    unittest.main()
