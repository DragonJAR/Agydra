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
        self.assertEqual(i18n.t("cmd.create.ok", name="test"), "created profile: test")
        i18n._set_active("es")
        self.assertEqual(i18n.t("cmd.create.ok", name="test"), "perfil creado: test")

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
        self.assertEqual(i18n.t("non.existent.key", default="fallback"), "fallback")
        self.assertEqual(i18n.t("non.existent.key"), "non.existent.key")

    def test_affirmative_answers(self):
        for ans in ("y", "yes", "s", "si", "sí", "Y", "YES", "S", "SI", "Sí", " yes ", "  s  "):
            with self.subTest(ans=ans):
                self.assertTrue(i18n.is_affirmative(ans))
        for ans in ("n", "no", "N", "NO", "", "  ", "maybe", "cancel", "0", "1"):
            with self.subTest(ans=ans):
                self.assertFalse(i18n.is_affirmative(ans))

    def test_resolve_language_cascade(self):
        self.assertEqual(i18n.resolve_language(flag_lang="es"), "es")
        self.assertEqual(i18n._get_active(), "es")

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

    def test_language_query_supported_languages_localized(self):
        self._run_cli("language", "es")
        res_es = self._run_cli("language")
        self.assertIn("soportados: en, es", res_es.stdout)

        self._run_cli("language", "en")
        res_en = self._run_cli("language")
        self.assertIn("supported : en, es", res_en.stdout)

    def test_cmd_list_empty_profiles_localized(self):
        res_en = self._run_cli("--lang=en", "list")
        self.assertEqual(res_en.returncode, 0)
        self.assertIn("no profiles; create one with: agydra create <name>", res_en.stdout)

        res_es = self._run_cli("--lang=es", "list")
        self.assertEqual(res_es.returncode, 0)
        self.assertIn("sin perfiles; crea uno con: agydra create <nombre>", res_es.stdout)

    def test_cmd_list_spanish_table_formatting_and_labels(self):
        import locks

        store = Store(self.store_root)
        store.create("perfil1", engine="agy")
        store.create("perfil2", engine="codex")

        locks.acquire_lease(store, "perfil1")
        try:
            res_es = self._run_cli("--lang=es", "list")
            self.assertEqual(res_es.returncode, 0)
            lines = res_es.stdout.strip().splitlines()
            header_line = lines[0]
            row1_line = lines[1]
            row2_line = lines[2]

            self.assertIn("OCUPADO  MOTOR", header_line)
            self.assertNotIn("OCUPADOMOTOR", header_line)
            self.assertIn("AUTENT.", header_line)
            self.assertIn("PREDET.", header_line)
            self.assertIn("ÚLTIMO USO", header_line)

            self.assertIn("sí", row1_line)
            self.assertIn("no autenticado", row1_line)
            self.assertIn("no autenticado", row2_line)
        finally:
            locks.release_lease(store, "perfil1")

        res_en = self._run_cli("--lang=en", "list")
        self.assertEqual(res_en.returncode, 0)
        en_lines = res_en.stdout.strip().splitlines()
        en_header = en_lines[0]
        en_row1 = en_lines[1]

        self.assertIn("BUSY  ENGINE", en_header)
        self.assertIn("AUTH", en_header)
        self.assertIn("DEFAULT", en_header)
        self.assertIn("LAST USED", en_header)
        self.assertIn("not-authenticated", en_row1)

    def test_cmd_list_unknown_auth_state_localized(self):
        import io
        import contextlib
        from unittest import mock
        import account
        import cli
        store = Store(self.store_root)
        store.create("perfil_unk", engine="claude")
        status = account.ClaudeAuthStatus(state=account.CLAUDE_STATE_UNKNOWN)
        with mock.patch("account.claude_auth_status", return_value=status):
            buf_es = io.StringIO()
            with mock.patch("i18n._get_active", return_value="es"):
                with contextlib.redirect_stdout(buf_es):
                    self.assertEqual(cli.cmd_list(store, None), 0)
            self.assertIn("desconocido", buf_es.getvalue())

            buf_en = io.StringIO()
            with mock.patch("i18n._get_active", return_value="en"):
                with contextlib.redirect_stdout(buf_en):
                    self.assertEqual(cli.cmd_list(store, None), 0)
            self.assertIn("unknown", buf_en.getvalue())

    def test_top_level_help_localized(self):
        res_es = self._run_cli("--lang=es", "--help")
        self.assertEqual(res_es.returncode, 0)
        self.assertIn("Lanzador multiperfil y despachador de carga para CLIs de IA", res_es.stdout)
        self.assertIn("opciones:", res_es.stdout)
        self.assertIn("gestión:", res_es.stdout)
        self.assertIn("ejemplos:", res_es.stdout)
        self.assertIn("autor y mantenedor:", res_es.stdout)
        self.assertIn("muestra este mensaje de ayuda y sale", res_es.stdout)

        res_en = self._run_cli("--lang=en", "--help")
        self.assertEqual(res_en.returncode, 0)
        self.assertIn("Multi-profile launcher and workload dispatcher for AI developer CLIs", res_en.stdout)
        self.assertTrue("options:" in res_en.stdout or "optional arguments:" in res_en.stdout)
        self.assertIn("management:", res_en.stdout)
        self.assertIn("examples:", res_en.stdout)
        self.assertIn("show this help message and exit", res_en.stdout)

    def test_subcommand_help_localized(self):
        os.environ["AGYDRA_LANG"] = "es"
        try:
            res_create_es = self._run_cli("create", "--help")
            self.assertEqual(res_create_es.returncode, 0)
            self.assertIn("uso: agydra create", res_create_es.stdout)
            self.assertIn("opciones:", res_create_es.stdout)
            self.assertIn("argumentos posicionales:", res_create_es.stdout)
            self.assertIn("nombre del perfil: letras minúsculas", res_create_es.stdout)
            self.assertIn("motor para este perfil:", res_create_es.stdout)
            self.assertIn("muestra este mensaje de ayuda y sale", res_create_es.stdout)

            res_delete_es = self._run_cli("delete", "--help")
            self.assertEqual(res_delete_es.returncode, 0)
            self.assertIn("uso: agydra delete", res_delete_es.stdout)
            self.assertIn("nombre de perfil o número base 1", res_delete_es.stdout)
            self.assertIn("omitir el zip de respaldo de seguridad", res_delete_es.stdout)

            res_login_es = self._run_cli("login", "--help")
            self.assertEqual(res_login_es.returncode, 0)
            self.assertIn("uso: agydra login", res_login_es.stdout)
            self.assertIn("nombre o número de perfil (por defecto: perfil resuelto)", res_login_es.stdout)
            self.assertIn("volver a iniciar sesión sin pedir confirmación", res_login_es.stdout)
        finally:
            os.environ.pop("AGYDRA_LANG", None)

        res_create_en = self._run_cli("create", "--help")
        self.assertEqual(res_create_en.returncode, 0)
        self.assertIn("usage: agydra create", res_create_en.stdout)
        self.assertTrue("options:" in res_create_en.stdout or "optional arguments:" in res_create_en.stdout)
        self.assertIn("positional arguments:", res_create_en.stdout)
        self.assertIn("profile name: lowercase, digits", res_create_en.stdout)
        self.assertIn("engine for this profile:", res_create_en.stdout)
        self.assertIn("show this help message and exit", res_create_en.stdout)

    def test_cmd_create_and_delete_localized_output(self):
        os.environ["AGYDRA_LANG"] = "es"
        try:
            res_create = self._run_cli("create", "perfil_es")
            self.assertEqual(res_create.returncode, 0)
            self.assertIn("perfil creado: perfil_es", res_create.stdout)

            res_delete = self._run_cli("delete", "perfil_es", "--force", "--no-backup")
            self.assertEqual(res_delete.returncode, 0)
            self.assertIn("perfil eliminado: perfil_es", res_delete.stdout)
        finally:
            os.environ.pop("AGYDRA_LANG", None)

        res_create_en = self._run_cli("create", "perfil_en")
        self.assertEqual(res_create_en.returncode, 0)
        self.assertIn("created profile: perfil_en", res_create_en.stdout)

        res_delete_en = self._run_cli("delete", "perfil_en", "--force", "--no-backup")
        self.assertEqual(res_delete_en.returncode, 0)
        self.assertIn("deleted profile: perfil_en", res_delete_en.stdout)

    def test_cmd_rename_and_default_localized_output(self):
        self._run_cli("create", "orig")
        os.environ["AGYDRA_LANG"] = "es"
        try:
            res_rename = self._run_cli("rename", "orig", "renombrado")
            self.assertEqual(res_rename.returncode, 0)
            self.assertIn("renombrado 'orig' -> 'renombrado'", res_rename.stdout)

            res_def = self._run_cli("default", "renombrado")
            self.assertEqual(res_def.returncode, 0)
            self.assertIn("perfil predeterminado: renombrado", res_def.stdout)
        finally:
            os.environ.pop("AGYDRA_LANG", None)

        res_def_en = self._run_cli("default", "renombrado")
        self.assertEqual(res_def_en.returncode, 0)
        self.assertIn("default profile: renombrado", res_def_en.stdout)


if __name__ == "__main__":
    unittest.main()
