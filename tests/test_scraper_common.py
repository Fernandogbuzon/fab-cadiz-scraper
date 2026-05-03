import unittest

from scraper_common import (
    build_lookup_key,
    build_match_key,
    fecha_iso,
    fecha_key,
    legacy_fecha_key,
    parse_fecha_hora,
    slugify,
)


class ScraperCommonTests(unittest.TestCase):
    def test_slugify_removes_accents_and_noise(self):
        self.assertEqual(slugify("Copa Andalucia B"), "copa-andalucia-b")
        self.assertEqual(slugify("1 Ano / Cadiz"), "1-ano-cadiz")

    def test_fecha_keys_keep_compatibility_and_canonical_lookup(self):
        self.assertEqual(legacy_fecha_key("5/4/2026"), "542026")
        self.assertEqual(fecha_key("5/4/2026"), "05042026")
        self.assertEqual(fecha_key("05/04/2026"), "05042026")

    def test_fecha_iso(self):
        self.assertEqual(fecha_iso("5/4/2026"), "2026-04-05")
        self.assertEqual(fecha_iso("05/04/2026"), "2026-04-05")
        self.assertEqual(fecha_iso("no-date"), "")

    def test_match_key_preserves_existing_contract(self):
        key = build_match_key("05/04/2026", "ADESA 80", "CB Cadiz", "Junior Masculino")
        self.assertEqual(key, "05042026_adesa-80_cb-cadiz_junior-masculino")

    def test_lookup_key_normalizes_date_padding(self):
        a = build_lookup_key("5/4/2026", "ADESA 80", "CB Cadiz")
        b = build_lookup_key("05/04/2026", "CB Cadiz", "ADESA 80")
        self.assertEqual(a, b)

    def test_parse_fecha_hora(self):
        dt = parse_fecha_hora("5/4/2026", "18:30")
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute), (2026, 4, 5, 18, 30))
        self.assertEqual(parse_fecha_hora("", "").year, 2000)


if __name__ == "__main__":
    unittest.main()
