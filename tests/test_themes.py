"""Theme system tests — registry, config round-trip, and headless TUI runs.

The retro themes are registered as real Textual themes, so they appear in the
command palette (Ctrl+P → "Change theme"); these tests drive theme changes
both ways: via the reactive `App.theme` (what the palette sets) and via the
`t` key binding.
"""
import asyncio
import os
import tempfile
import unittest

from printqueue.config import Config, load_config, save_config
from printqueue.db import Database
from printqueue.themes import (
    DEFAULT_MARKUP,
    PQ_VARIABLE_DEFAULTS,
    RETRO_THEMES,
    custom_themes,
    get_markup,
    known_theme_names,
)


class StubClient:
    """Minimal stand-in for BambuCloudClient in headless UI tests."""

    async def poll_async(self):
        from printqueue.api import PrinterStatus
        return PrinterStatus(online=False, source="cloud", state="offline",
                             error="stub")

    def close(self):
        pass


class ThemeRegistryTests(unittest.TestCase):
    def test_custom_themes_cover_retro_names(self):
        themes = custom_themes()
        self.assertEqual([t.name for t in themes], list(RETRO_THEMES))

    def test_known_theme_names_include_builtins_and_customs(self):
        names = known_theme_names()
        for expected in ("c64", "wildcat", "textual-dark", "nord"):
            self.assertIn(expected, names)

    def test_theme_variables_match_defaults_keys(self):
        expected_keys = set(PQ_VARIABLE_DEFAULTS)
        for theme in custom_themes():
            self.assertEqual(set(theme.variables), expected_keys, theme.name)
            for key, value in theme.variables.items():
                self.assertTrue(value.strip(), f"{theme.name}:{key} empty")

    def test_markup_lookup_and_fallback(self):
        self.assertTrue(get_markup("c64").ascii_labels)
        self.assertTrue(get_markup("wildcat").ascii_labels)
        self.assertIs(get_markup("textual-dark"), DEFAULT_MARKUP)
        self.assertIs(get_markup(None), DEFAULT_MARKUP)
        self.assertFalse(DEFAULT_MARKUP.ascii_labels)

    def test_title_markup(self):
        self.assertEqual(DEFAULT_MARKUP.title("X"), "[bold cyan]X[/]")


class ThemeConfigTests(unittest.TestCase):
    def test_theme_round_trips_through_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            save_config(Config(theme="c64"), path)
            self.assertEqual(load_config(path).theme, "c64")
            save_config(Config(), path)
            self.assertIsNone(load_config(path).theme)


class ThemeUITests(unittest.TestCase):
    """Mount the real app headless — proves the CSS parses under every theme
    and that palette-driven theme switches restyle the app."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._db_path = os.path.join(self._tmp.name, "test.db")

    def _make_app(self, theme_name=None):
        from printqueue.ui import PrintQueueApp
        db = Database(self._db_path)
        return PrintQueueApp(db=db, client=StubClient(), cfg=None,
                             theme_name=theme_name)

    def test_retro_themes_registered_in_palette_list(self):
        async def run():
            app = self._make_app()
            async with app.run_test():
                for name in RETRO_THEMES:
                    self.assertIn(name, app.available_themes)
                self.assertEqual(app.theme, "textual-dark")
                self.assertFalse(app.screen.has_class("pq-themed"))

        asyncio.run(run())

    def test_each_retro_theme_mounts_with_colors(self):
        from textual.color import Color

        async def run(theme_name, expected_bg):
            app = self._make_app(theme_name)
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.theme, theme_name)
                self.assertTrue(app.screen.has_class("pq-themed"))
                self.assertEqual(app.screen.styles.background,
                                 Color.parse(expected_bg))

        asyncio.run(run("c64", "#40318D"))
        asyncio.run(run("wildcat", "#000000"))

    def test_palette_style_theme_change_restyles_and_swaps_labels(self):
        """Setting App.theme (what Ctrl+P does) must restyle everything."""
        from textual.color import Color
        from textual.widgets import DataTable

        async def run():
            app = self._make_app()
            app.db.add_job(name="benchy", filament_type="PLA")
            async with app.run_test() as pilot:
                table = app.query_one("#queue-table", DataTable)
                self.assertIn("⏳", table.get_row_at(0)[4])

                app.theme = "c64"          # same code path as the palette
                await pilot.pause()
                self.assertTrue(app.screen.has_class("pq-themed"))
                self.assertEqual(app.screen.styles.background,
                                 Color.parse("#40318D"))
                self.assertNotIn("⏳", table.get_row_at(0)[4])

                app.theme = "nord"         # built-in: retro overrides off
                await pilot.pause()
                self.assertFalse(app.screen.has_class("pq-themed"))
                self.assertIn("⏳", table.get_row_at(0)[4])

        asyncio.run(run())

    def test_cycle_theme_key(self):
        async def run():
            app = self._make_app()
            async with app.run_test() as pilot:
                await pilot.press("t")
                await pilot.pause()
                self.assertEqual(app.theme, "c64")
                await pilot.press("t")
                await pilot.pause()
                self.assertEqual(app.theme, "wildcat")
                await pilot.press("t")
                await pilot.pause()
                self.assertEqual(app.theme, "textual-dark")
                self.assertFalse(app.screen.has_class("pq-themed"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
