"""Unit tests for the i18n module and language CLI features."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import i18n
from conftest import BaseCase
from models import DEFAULT_SETTINGS
from store import Store


class TestI18nModule(unittest.TestCase):
    def setUp(self):
        self.orig_active = i18n._get_active()
        self.orig_env = os.environ.get("AGYDRA_LANG")

    def tearDown(self):
        i18n._set_active(self.orig_active)
        if self.orig_env is not None:
            os.environ["AGYDRA_LANG"] = self.orig_env
        else:
            os.environ.pop("AGYDRA_LANG", None)

    def test_default_settings_has_lang_auto(self):
        self.assertIn("lang", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["lang"], "auto")

    def test_translation_en_and_es(self):
        i18n._set_active("en")
        self.assertEqual(i18n.t("cmd.create.ok", name="test"), "Profile 'test' created.")
        i18n._set_active("es")
        self.assertEqual(i18n.t("cmd.create.ok", name="test"), "Perfil 'test' creado.")

    def test_catalogs_have_identical_keys_and_placeholders(self):
        import string

        def fields(template):
            return {
                field for _, field, _, _ in string.Formatter().parse(template) if field is not None
            }

        english, spanish = i18n._STRINGS["en"], i18n._STRINGS["es"]
        self.assertEqual(set(english), set(spanish))
        mismatched = {
            key: (fields(english[key]), fields(spanish[key]))
            for key in english
            if fields(english[key]) != fields(spanish[key])
        }
        self.assertEqual(mismatched, {})

    def test_fallback_cascade(self):
        i18n._set_active("es")
        # Clave inexistente en es pero sí en default
        self.assertEqual(i18n.t("non.existent.key", default="fallback"), "fallback")
        # Clave inexistente en todos lados devuelve la clave
        self.assertEqual(i18n.t("non.existent.key"), "non.existent.key")

    def test_affirmative_answers(self):
        for ans in ("y", "yes", "s", "si", "sí", "Y", "YES", "S", "SI", "Sí", " yes ", "  s  "):
            with self.subTest(ans=ans):
                self.assertTrue(i18n.is_affirmative(ans))
        for ans in ("n", "no", "N", "NO", "", "  ", "maybe", "cancel", "0", "1"):
            with self.subTest(ans=ans):
                self.assertFalse(i18n.is_affirmative(ans))

    def test_resolve_language_cascade(self):
        # 1. flag_lang
        self.assertEqual(i18n.resolve_language(flag_lang="es"), "es")
        self.assertEqual(i18n._get_active(), "es")

        # 2. env var
        os.environ["AGYDRA_LANG"] = "en"
        self.assertEqual(i18n.resolve_language(), "en")
        self.assertEqual(i18n._get_active(), "en")

        os.environ.pop("AGYDRA_LANG", None)

    def _locale_with(self, **env):
        from unittest import mock

        with mock.patch.dict(os.environ, env, clear=False):
            for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
                if var not in env:
                    os.environ.pop(var, None)
            return i18n._locale_lang()

    def test_locale_first_non_empty_variable_decides(self):
        self.assertEqual(self._locale_with(LC_ALL="C", LANG="es_AR.UTF-8"), "en")
        self.assertEqual(self._locale_with(LC_ALL="POSIX", LC_MESSAGES="es_ES"), "en")
        self.assertEqual(self._locale_with(LC_ALL="fr_FR.UTF-8", LANG="es_ES"), "en")
        self.assertEqual(self._locale_with(LC_ALL="", LC_MESSAGES="es_CO", LANG="en_US"), "es")
        self.assertEqual(self._locale_with(LC_ALL="  ", LANG="es_AR.UTF-8"), "es")
        self.assertEqual(self._locale_with(LC_ALL="ES_mx.UTF-8"), "es")

    def test_locale_falls_back_to_getlocale_only_when_unset(self):
        from unittest import mock

        with mock.patch.object(i18n.locale, "getlocale", return_value=("es_CO", "UTF-8")):
            self.assertEqual(self._locale_with(), "es")
            self.assertEqual(self._locale_with(LC_ALL="C"), "en")
        with mock.patch.object(i18n.locale, "getlocale", side_effect=ValueError("bad")):
            self.assertEqual(self._locale_with(), "en")

    def test_set_language_validation(self):
        with self.assertRaises(ValueError):
            i18n.set_language(None, "invalid_lang")


class TestI18nCli(BaseCase):
    def test_global_flag_lang_persists(self):
        res = self._run_cli("--lang", "es")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Idioma cambiado a Español y guardado.", res.stdout)
        cfg = Store(self.store_root).load_config()
        self.assertEqual(cfg.settings.get("lang"), "es")

    def test_global_flag_lang_with_equals(self):
        res = self._run_cli("--lang=en")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Language set to English and saved.", res.stdout)
        cfg = Store(self.store_root).load_config()
        self.assertEqual(cfg.settings.get("lang"), "en")

    def test_subcommand_language_set_and_query(self):
        res = self._run_cli("language", "es")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Idioma establecido en 'es' y guardado.", res.stdout)

        res_query = self._run_cli("language")
        self.assertEqual(res_query.returncode, 0)
        self.assertIn("Idioma actual: es", res_query.stdout)

    def test_subcommand_aliases(self):
        res = self._run_cli("lang", "en")
        self.assertEqual(res.returncode, 0)
        res_idioma = self._run_cli("idioma")
        self.assertEqual(res_idioma.returncode, 0)
        self.assertIn("Current language: en", res_idioma.stdout)


if __name__ == "__main__":
    unittest.main()
