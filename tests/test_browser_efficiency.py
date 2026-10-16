import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import scraper_competicion


class DropdownEfficiencyTests(unittest.IsolatedAsyncioTestCase):
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
