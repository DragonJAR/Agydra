"""Tests for Codex isolation: overlay construction, .codex symlink, and CODEX_HOME."""
from __future__ import annotations

import errno
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolation
import platforms
import runner
from conftest import BaseCase
from store import Store


class TestCodexIsolation(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_build_overlay_creates_codex_link(self):
        self.store.create("openai-work", engine="codex")
        profile_data = self.store.profile_data_dir("openai-work")
        overlay = isolation.build_overlay("openai-work", profile_data, self.store.root, engine="codex")

        self.assertTrue(overlay.is_dir())
        codex_link = overlay / ".codex"
        self.assertTrue(codex_link.exists() or codex_link.is_symlink())
        self.assertTrue(isolation.link_points_to(codex_link, profile_data))

    def test_isolated_env_injects_codex_home(self):
        self.store.create("openai-work", engine="codex")
        profile_data = self.store.profile_data_dir("openai-work")
        overlay = isolation.build_overlay("openai-work", profile_data, self.store.root, engine="codex")

        env = isolation.isolated_env(overlay, {"AGYDRA_PROFILE": "openai-work"}, engine="codex")
        self.assertEqual(env.get("CODEX_HOME"), str(overlay / ".codex"))
        self.assertEqual(env.get("AGYDRA_PROFILE"), "openai-work")
        self.assertIn("AGYDRA_REAL_HOME", env)

    def test_extra_cannot_override_isolation_environment(self):
        self.store.create("openai-work", engine="codex")
        profile_data = self.store.profile_data_dir("openai-work")
        overlay = isolation.build_overlay(
            "openai-work", profile_data, self.store.root, engine="codex"
        )

        env = isolation.isolated_env(
            overlay,
            {
                "HOME": "/host/home",
                "CODEX_HOME": "/host/.codex",
                "AGYDRA_REAL_HOME": "/spoofed/real-home",
                "AGYDRA_PROFILE": "openai-work",
            },
            engine="codex",
        )

        self.assertEqual(env["HOME"], str(overlay))
        self.assertEqual(env["CODEX_HOME"], str(overlay / ".codex"))
        self.assertEqual(env["AGYDRA_REAL_HOME"], str(self.fake_home))
        self.assertEqual(env["AGYDRA_PROFILE"], "openai-work")

    def test_codex_daemon_setting_ignores_comments_and_other_tables(self):
        self.store.create("openai-work", engine="codex")
        data_dir = self.store.profile_data_dir("openai-work")
        config = data_dir / "config.toml"
        config.write_text(
            "# daemon_auto_start = true\n"
            "[other]\n"
            "daemon_auto_start = true\n"
            "[features]\n",
            encoding="utf-8",
        )

        isolation._disable_codex_daemon_auto_start(data_dir)

        content = config.read_text(encoding="utf-8")
        self.assertIn("[features]\ndaemon_auto_start = false\n", content)
        self.assertIn("[other]\ndaemon_auto_start = true\n", content)

    def test_host_codex_dir_skipped_from_overlay(self):
        real_home = platforms.real_home()
        fake_host_codex = real_home / ".codex"
        fake_host_codex.mkdir(parents=True, exist_ok=True)
        (fake_host_codex / "host-marker.txt").write_text("host", encoding="utf-8")

        self.store.create("openai-work", engine="codex")
        profile_data = self.store.profile_data_dir("openai-work")
        (profile_data / "profile-marker.txt").write_text("profile", encoding="utf-8")

        overlay = isolation.build_overlay("openai-work", profile_data, self.store.root, engine="codex")
        codex_link = overlay / ".codex"

        self.assertTrue((codex_link / "profile-marker.txt").exists())
        self.assertFalse((codex_link / "host-marker.txt").exists())


try:
    import tomllib
except ImportError:
    tomllib = None


class TestCodexDaemonConfigEditing(BaseCase):
    def setUp(self):
        super().setUp()
        self.data_dir = self.fake_home / "codex-data"
        self.data_dir.mkdir()
        self.config = self.data_dir / "config.toml"

    def _edit(self, raw: bytes) -> bytes:
        self.config.write_bytes(raw)
        isolation._disable_codex_daemon_auto_start(self.data_dir)
        return self.config.read_bytes()

    def _assert_daemon_disabled(self, original: bytes, edited: bytes) -> None:
        if tomllib is None:
            return
        parsed = tomllib.loads(edited.decode("utf-8").lstrip("\ufeff"))
        self.assertIs(parsed["features"]["daemon_auto_start"], False)
        before = tomllib.loads(original.decode("utf-8").lstrip("\ufeff"))
        for key, value in before.items():
            if key != "features":
                self.assertEqual(parsed[key], value)
        for key, value in before.get("features", {}).items():
            if key != "daemon_auto_start":
                self.assertEqual(parsed["features"][key], value)

    def test_valid_standard_forms_are_edited_without_corruption(self):
        cases = {
            "inline table without setting": (
                b'features = { web_search = true }\nmodel = "x"\n',
                b'features = { daemon_auto_start = false, web_search = true }\nmodel = "x"\n',
            ),
            "inline table with setting enabled": (
                b'features = { daemon_auto_start = true, a = 1 }\n',
                b'features = { daemon_auto_start = false, a = 1 }\n',
            ),
            "empty inline table": (
                b'features = {}\nmodel = "x"\n',
                b'features = { daemon_auto_start = false }\nmodel = "x"\n',
            ),
            "dotted keys without setting": (
                b'features.web_search = true\nmodel = "x"\n',
                b'features.daemon_auto_start = false\nfeatures.web_search = true\nmodel = "x"\n',
            ),
            "dotted setting enabled with comment": (
                b'features.daemon_auto_start = true # keep note\nmodel = "x"\n',
                b'features.daemon_auto_start = false # keep note\nmodel = "x"\n',
            ),
            "table setting enabled with comment": (
                b'[features]\ndaemon_auto_start = true  # note\nother = 1\n',
                b'[features]\ndaemon_auto_start = false  # note\nother = 1\n',
            ),
            "integer flag": (
                b'[features]\ndaemon_auto_start = 1\n',
                b'[features]\ndaemon_auto_start = false\n',
            ),
            "quoted table header": (
                b'["features"]\nx = 1\n',
                b'["features"]\ndaemon_auto_start = false\nx = 1\n',
            ),
            "only a features subtable": (
                b'[features.sub]\nx = 1\n',
                b'[features.sub]\nx = 1\n\n[features]\ndaemon_auto_start = false\n',
            ),
            "header text inside a multi-line string": (
                b'note = """\n[features]\ndaemon_auto_start = true\n"""\n',
                b'note = """\n[features]\ndaemon_auto_start = true\n"""\n\n[features]\ndaemon_auto_start = false\n',
            ),
            "header text inside a multi-line array": (
                b'paths = [\n  "a", # [features]\n  "b",\n]\n',
                b'paths = [\n  "a", # [features]\n  "b",\n]\n\n[features]\ndaemon_auto_start = false\n',
            ),
            "crlf line endings are preserved": (
                b'[features]\r\nx = 1\r\n',
                b'[features]\r\ndaemon_auto_start = false\r\nx = 1\r\n',
            ),
            "no trailing newline": (
                b'model = "x"',
                b'model = "x"\n\n[features]\ndaemon_auto_start = false\n',
            ),
            "byte order mark with table": (
                b'\xef\xbb\xbf[features]\nfoo = 1\n',
                b'\xef\xbb\xbf[features]\ndaemon_auto_start = false\nfoo = 1\n',
            ),
            "byte order mark without features": (
                b'\xef\xbb\xbfmodel = "x"\n',
                b'\xef\xbb\xbfmodel = "x"\n\n[features]\ndaemon_auto_start = false\n',
            ),
            "unicode escaped header": (
                b'["\\u0066eatures"]\nx = 1\n',
                b'["\\u0066eatures"]\ndaemon_auto_start = false\nx = 1\n',
            ),
            "unicode escaped setting name": (
                b'[features]\n"daemon\\u005fauto_start" = true\n',
                b'[features]\n"daemon\\u005fauto_start" = false\n',
            ),
            "wide unicode escape in a dotted key": (
                b'"\\U00000066eatures".web = true\n',
                b'features.daemon_auto_start = false\n"\\U00000066eatures".web = true\n',
            ),
            "escaped inline table key": (
                b'"f\\u0065atures" = { a = 1 }\n',
                b'"f\\u0065atures" = { daemon_auto_start = false, a = 1 }\n',
            ),
            "literal quoted key": (
                b"'features'.web = true\n",
                b"features.daemon_auto_start = false\n'features'.web = true\n",
            ),
            "escaped quote and backslash in unrelated keys": (
                b'"a\\"b\\\\c" = 1\n',
                b'"a\\"b\\\\c" = 1\n\n[features]\ndaemon_auto_start = false\n',
            ),
            "already disabled stays byte identical": (
                b'[features]\ndaemon_auto_start = false\nx = 1\n',
                b'[features]\ndaemon_auto_start = false\nx = 1\n',
            ),
        }
        for label, (raw, expected) in cases.items():
            with self.subTest(case=label):
                edited = self._edit(raw)
                self.assertEqual(edited, expected)
                self._assert_daemon_disabled(raw, edited)
                self.assertEqual(self._edit(edited), edited)

    def test_missing_config_is_created_with_the_setting(self):
        isolation._disable_codex_daemon_auto_start(self.data_dir)
        self.assertEqual(
            self.config.read_bytes(), b"[features]\ndaemon_auto_start = false\n"
        )

    def test_read_failure_is_reported_without_sensitive_os_error_text(self):
        self.config.write_text("[features]\ndaemon_auto_start = true\n", encoding="utf-8")
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError("secret config detail")
        ):
            with self.assertRaises(isolation.IsolationError) as raised:
                isolation._disable_codex_daemon_auto_start(self.data_dir)

        self.assertIn(str(self.config), str(raised.exception))
        self.assertIn("PermissionError", str(raised.exception))
        self.assertNotIn("secret config detail", str(raised.exception))

    def test_inspection_eio_preserves_existing_config_without_writing(self):
        original = b"[features]\ndaemon_auto_start = true\n"
        self.config.write_bytes(original)
        with mock.patch.object(
            Path,
            "lstat",
            side_effect=OSError(errno.EIO, "secret inspection detail"),
        ), mock.patch.object(isolation.store, "atomic_write_text") as atomic_write:
            with self.assertRaises(isolation.IsolationError) as raised:
                isolation._disable_codex_daemon_auto_start(self.data_dir)

        self.assertIn(str(self.config), str(raised.exception))
        self.assertIn("EIO", str(raised.exception))
        self.assertNotIn("secret inspection detail", str(raised.exception))
        self.assertEqual(self.config.read_bytes(), original)
        atomic_write.assert_not_called()

    def test_update_write_failure_is_reported_without_sensitive_os_error_text(self):
        self.config.write_text("[features]\ndaemon_auto_start = true\n", encoding="utf-8")
        with mock.patch.object(
            isolation.store,
            "atomic_write_text",
            side_effect=PermissionError("secret config detail"),
        ):
            with self.assertRaises(isolation.IsolationError) as raised:
                isolation._disable_codex_daemon_auto_start(self.data_dir)

        self.assertIn(str(self.config), str(raised.exception))
        self.assertIn("PermissionError", str(raised.exception))
        self.assertNotIn("secret config detail", str(raised.exception))

    def test_existing_false_setting_does_not_write(self):
        self.config.write_text("[features]\ndaemon_auto_start = false\n", encoding="utf-8")
        with mock.patch.object(
            isolation.store,
            "atomic_write_text",
            side_effect=AssertionError("already-disabled config must be a no-op"),
        ) as atomic_write:
            isolation._disable_codex_daemon_auto_start(self.data_dir)
        atomic_write.assert_not_called()

    def test_config_creation_failure_aborts_before_codex_launch(self):
        codex_store = Store(self.fake_home / "codex-store")
        codex_store.create("codex-work", engine="codex")
        plan = runner.build_plan(
            codex_store,
            [],
            flag_ref="codex-work",
            binary_override=sys.executable,
        )

        with mock.patch.object(
            isolation.store,
            "atomic_write_text",
            side_effect=PermissionError("secret config detail"),
        ), mock.patch.object(runner.platforms, "launch_argv") as launch, mock.patch.object(
            runner.platforms, "run_wait"
        ) as waited_launch:
            with self.assertRaises(isolation.IsolationError) as raised:
                runner.run(plan, store=codex_store)

        self.assertIn("config.toml", str(raised.exception))
        self.assertNotIn("secret config detail", str(raised.exception))
        launch.assert_not_called()
        waited_launch.assert_not_called()

    def test_unparseable_or_non_table_config_fails_explicitly_and_is_untouched(self):
        cases = {
            "invalid utf-8": b'model = "\xff\xfe"\n',
            "unterminated string": b'model = "abc\n',
            "features is not a table": b"features = true\n",
            "features array of tables": b"[[features]]\nx = 1\n",
            "invalid escape in a quoted key": b'["fe\\qatures"]\nx = 1\n',
            "truncated unicode escape": b'["\\u00"]\nx = 1\n',
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                self.config.write_bytes(raw)
                with self.assertRaises(isolation.IsolationError):
                    isolation._disable_codex_daemon_auto_start(self.data_dir)
                self.assertEqual(self.config.read_bytes(), raw)

    def test_symlinked_config_never_writes_through_to_the_external_file(self):
        external = self.fake_home / "external.toml"
        external.write_bytes(b'features = { a = 1 }\n')
        self.config.symlink_to(external)
        isolation._disable_codex_daemon_auto_start(self.data_dir)
        self.assertEqual(external.read_bytes(), b'features = { a = 1 }\n')
        self.assertFalse(self.config.is_symlink())
        self._assert_daemon_disabled(
            b'features = { a = 1 }\n', self.config.read_bytes()
        )

    def test_build_overlay_surfaces_config_errors(self):
        store = Store()
        store.create("cx", engine="codex")
        data_dir = store.profile_data_dir("cx")
        (data_dir / "config.toml").write_bytes(b'x = "\xff"\n')
        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("cx", data_dir, store.root, engine="codex")


if __name__ == "__main__":
    unittest.main()
