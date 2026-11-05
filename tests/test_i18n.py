import os
import unittest
from unittest import mock

from netserve import i18n


class I18nTest(unittest.TestCase):
    def test_every_catalog_has_the_english_keys(self):
        expected = set(i18n.CATALOG["en"])
        for lang in ("it", "fr", "de", "es"):
            self.assertEqual(set(i18n.CATALOG[lang]), expected, lang)

    def test_language_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"LANGUAGE": "", "LC_ALL": "", "LANG": "it_IT.UTF-8"}, clear=False):
            self.assertEqual(i18n.current_language(), "it")

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(i18n.translate("app_title", "ja"), i18n.translate("app_title", "en"))

    def test_unknown_key_returns_the_key(self):
        self.assertEqual(i18n.translate("no_such_key", "en"), "no_such_key")

    def test_fields_are_substituted(self):
        self.assertIn("69", i18n.translate("port_busy", "en", port=69))


if __name__ == "__main__":
    unittest.main()
