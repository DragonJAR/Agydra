"""Integration: agydra launches agy with the overlay; the generic store is never touched."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agydra import account, locks, platforms, runner  # noqa: E402
from agydra.store import Store  # noqa: E402

from conftest import BaseCase  # noqa: E402


class TestIntegration(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def _run_cli(self, *args):
        """Run agydra's CLI in a subprocess (execvpe replaces the process)."""
        return subprocess.run(
            [sys.executable, "-m", "agydra", *args],
            capture_output=True, text=True, timeout=60,
        )

    def test_launch_redirects_agy_to_profile_store(self):
        real_gemini = self.fake_home / ".gemini"
        marker = real_gemini / "generic-marker"
        marker.write_text("do-not-touch", encoding="utf-8")
        before = marker.read_text(encoding="utf-8")

        result = self._run_cli("-p", "work")
        self.assertEqual(result.returncode, 0, result.stderr)

        # The fake agy wrote into the PROFILE store, not the generic one.
        wrote = self.store.profile_data_dir("work") / "fake-agy-wrote"
        self.assertTrue(wrote.exists(), "agy did not write to the profile store")
        # The generic store is byte-identical: R2 invariant.
        self.assertEqual(marker.read_text(encoding="utf-8"), before)
        self.assertIn("PROFILE=work", result.stdout)
        self.assertIn(str((self.store.root / "overlays" / "work")), result.stdout)

    def test_two_profiles_full_isolation(self):
        self.store.create("lab")
        r1 = self._run_cli("-p", "work")
        r2 = self._run_cli("-p", "lab")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        work_wrote = self.store.profile_data_dir("work") / "fake-agy-wrote"
        lab_wrote = self.store.profile_data_dir("lab") / "fake-agy-wrote"
        self.assertTrue(work_wrote.exists() and lab_wrote.exists())
        # generic marker untouched by both
        self.assertTrue((self.fake_home / ".gemini" / "oauth_creds.json").exists())

    def test_dry_run_prints_plan_and_executes_nothing(self):
        result = self._run_cli("-p", "work", "--dry-run", "anything")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Plan: profile, binary, argv, overlay, HOME redirect, sandbox
        self.assertIn("profile : work", result.stdout)
        self.assertIn("binary", result.stdout)
        self.assertIn("overlay :", result.stdout)
        # env line: HOME=<overlay> on POSIX, USERPROFILE=<overlay> on Windows
        var = "USERPROFILE" if sys.platform.startswith("win") else "HOME"
        self.assertIn(f"env     : {var}=", result.stdout)
        self.assertIn("sandbox : off", result.stdout)
        wrote = self.store.profile_data_dir("work") / "fake-agy-wrote"
        self.assertFalse(wrote.exists(), "dry-run must not execute agy")

    def test_last_used_is_updated_after_launch(self):
        result = self._run_cli("-p", "work")
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = self.store.get("work")
        self.assertIsNotNone(profile.last_used)

    def test_number_reference_resolves(self):
        self.store.create("second")
        result = self._run_cli("-p", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PROFILE=second", result.stdout)

    def test_launch_propagates_child_exit_code(self):
        # POSIX: launch() exec-replaces; Windows: subprocess.run returns the
        # child's code. Either way a failing child must surface as the CLI's
        # exit code, never as a traceback.
        self.store.create("err")
        if platforms.is_windows():
            bad = self.bin_dir / "agy-fail.cmd"
            bad.write_text("@echo off\r\nexit /b 7\r\n", encoding="utf-8")
        else:
            bad = self.bin_dir / "agy-fail"
            bad.write_text("#!/usr/bin/env python3\nraise SystemExit(7)\n", encoding="utf-8")
            bad.chmod(bad.stat().st_mode | 0o111)
        result = self._run_cli("-p", "err", "--binary", str(bad))
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_create_second_profile_copies_settings(self):
        # Regression: _share_config referenced an undefined target_dir
        # (NameError), breaking `create` from the second profile on.
        (self.store.profile_data_dir("work") / "settings.json").write_text(
            '{"theme": "dark"}', encoding="utf-8"
        )
        result = self._run_cli("create", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        copied = self.store.profile_data_dir("fresh") / "settings.json"
        self.assertEqual(copied.read_text(encoding="utf-8"), '{"theme": "dark"}')

    def test_share_config_copies_only_allowed_files(self):
        self.store.create("lab")
        src = self.store.profile_data_dir("work")
        (src / "settings.json").write_text('{"a": 1}', encoding="utf-8")
        (src / "mcp.json").write_text('{"b": 2}', encoding="utf-8")
        (src / "oauth_creds.json").write_text('{"c": 3}', encoding="utf-8")
        result = self._run_cli("share-config", "work", "lab")
        self.assertEqual(result.returncode, 0, result.stderr)
        lab = self.store.profile_data_dir("lab")
        self.assertTrue((lab / "settings.json").exists())
        self.assertTrue((lab / "mcp.json").exists())
        self.assertFalse(
            (lab / "oauth_creds.json").exists(), "credentials must never be shared"
        )

    def test_readonly_commands_create_nothing(self):
        # status/--dry-run must have zero filesystem side effects; Store() is
        # lazy, so even a fresh AGYDRA_HOME stays untouched.
        empty_root = self._tmp / "empty-store"
        os.environ["AGYDRA_HOME"] = str(empty_root)
        try:
            for args in (["status"], ["-p", "ghost", "--dry-run"]):
                result = self._run_cli(*args)
                self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(empty_root.exists(), "read-only commands created the store")
        finally:
            os.environ["AGYDRA_HOME"] = str(self.store_root)

    def test_management_create_and_list(self):
        result = self._run_cli("create", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._run_cli("list")
        self.assertIn("work", result.stdout)
        self.assertIn("fresh", result.stdout)

    def test_account_state_and_email(self):
        data = self.store.profile_data_dir("work")
        cli_dir = data / account.AGY_CLI_DIR
        cli_dir.mkdir(parents=True, exist_ok=True)
        (cli_dir / account.TOKEN_FILE).write_text(
            '{"auth_method": "consumer", "token": {'
            '"access_token": "tok", "refresh_token": "r"}}',
            encoding="utf-8",
        )
        self.assertEqual(
            account.auth_state(data, self.store, "work"), "authenticated"
        )
        self.assertEqual(account.sync_profile_email(self.store, "work"), None)
        self.assertEqual(self.store.get("work").email, None)


class TestRandomProfileSelection(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")
        self.store.create("lab")
        # Both authenticated so -r has a free pool.
        for name in ("work", "lab"):
            (self.store.profile_data_dir(name) / "oauth_creds.json").write_text(
                '{"access_token": "tok"}', encoding="utf-8"
            )

    def _run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "agydra", *args],
            capture_output=True, text=True, timeout=60,
        )

    def test_r_picks_a_free_authenticated_profile(self):
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = result.stdout.split("PROFILE=", 1)[1].splitlines()[0].strip()
        self.assertIn(profile, ("work", "lab"))

    def test_short_flag_forms(self):
        for flags in (["-r"], ["--random"], ["-r", "hello"]):
            result = self._run_cli(*flags)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_r_and_p_are_mutually_exclusive(self):
        result = self._run_cli("-r", "-p", "work")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr)

    def test_r_skips_authenticated_only(self):
        # lab is unauthenticated; -r must fall back to work.
        (self.store.profile_data_dir("lab") / "oauth_creds.json").unlink()
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PROFILE=work", result.stdout)

    def test_r_errors_when_none_authenticated(self):
        for name in ("work", "lab"):
            (self.store.profile_data_dir(name) / "oauth_creds.json").unlink()
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 1)
        self.assertIn("authenticate one with", result.stderr)

    def test_r_errors_with_single_profile(self):
        self.store.delete("lab")
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 1)
        self.assertIn("at least 2 profiles", result.stderr)

    def test_r_errors_with_no_profiles(self):
        self.store.delete("work")
        self.store.delete("lab")
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 1)
        self.assertIn("at least 2 profiles", result.stderr)

    def test_r_respects_live_session(self):
        # Hold a live session on 'work'; -r must pick 'lab'.
        gate = self._tmp / "gate"
        env = dict(os.environ)
        env["FAKE_AGY_GATE"] = str(gate)
        subprocess.Popen(
            [sys.executable, "-m", "agydra", "-p", "work", "--hold"],
            env=env,
        )
        try:
            # Wait until the lock is held.
            import time

            for _ in range(100):
                if locks.is_locked(self.store, "work"):
                    break
                time.sleep(0.05)
            result = self._run_cli("-r")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PROFILE=lab", result.stdout)
        finally:
            gate.touch()
            import time

            time.sleep(0.3)


class TestBusyGuards(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def _run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "agydra", *args],
            capture_output=True, text=True, timeout=60,
        )

    def test_delete_refuses_busy_profile(self):
        handle = locks.try_lock(self.store, "work")
        try:
            result = self._run_cli("delete", "work", "-f")
            self.assertEqual(result.returncode, 1)
            self.assertIn("live session", result.stderr)
        finally:
            handle.release()

    def test_delete_allowed_when_free(self):
        result = self._run_cli("delete", "work", "-f", "--no-backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.store.exists("work"))

    def test_rename_refuses_busy_profile(self):
        handle = locks.try_lock(self.store, "work")
        try:
            result = self._run_cli("rename", "work", "new")
            self.assertEqual(result.returncode, 1)
            self.assertIn("live session", result.stderr)
        finally:
            handle.release()

    def test_list_shows_busy_column(self):
        handle = locks.try_lock(self.store, "work")
        try:
            result = self._run_cli("list")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BUSY", result.stdout)
            self.assertIn("yes", result.stdout)
        finally:
            handle.release()


class TestAliasesAndFlagTable(BaseCase):
    def test_all_canonical_aliases_resolve(self):
        from agydra.cli import _CANONICAL, _SUBCOMMAND_ALIASES

        for canonical, aliases in _SUBCOMMAND_ALIASES.items():
            self.assertEqual(_CANONICAL[canonical], canonical)
            for alias in aliases:
                self.assertEqual(_CANONICAL[alias], canonical)

    def test_alias_dispatch_list(self):
        # list/ls/l must all reach the list handler (no crash, prints header).
        from agydra.cli import _CANONICAL

        self.assertEqual(_CANONICAL.get("ls"), "list")
        self.assertEqual(_CANONICAL.get("l"), "list")
        self.assertEqual(_CANONICAL.get("st"), "status")
        self.assertEqual(_CANONICAL.get("c"), "create")
        self.assertEqual(_CANONICAL.get("rm"), "delete")
        self.assertEqual(_CANONICAL.get("mv"), "rename")
        self.assertEqual(_CANONICAL.get("u"), "use")
        self.assertEqual(_CANONICAL.get("d"), "default")
        self.assertEqual(_CANONICAL.get("in"), "login")
        self.assertEqual(_CANONICAL.get("doc"), "doctor")
        self.assertEqual(_CANONICAL.get("share"), "share-config")
        self.assertEqual(_CANONICAL.get("imp"), "import")

    def test_unknown_token_is_not_a_subcommand(self):
        from agydra.cli import _CANONICAL

        self.assertIsNone(_CANONICAL.get("totally-not-a-subcommand"))
        self.assertIsNone(_CANONICAL.get(""))

    def test_flag_table_shape_and_extractor(self):
        from agydra.cli import _LAUNCH_FLAGS, _consume_launch_flags

        # Table completeness: every flag has both spellings and a value flag.
        for key, (short, long_, takes_value) in _LAUNCH_FLAGS.items():
            self.assertTrue(short.startswith("-"))
            self.assertTrue(long_.startswith("--"))

        values, rest = _consume_launch_flags(["-p", "work", "-r", "chat"])
        self.assertEqual(values["profile"], "work")
        self.assertTrue(values["random"])
        self.assertEqual(rest, ["chat"])

        values, rest = _consume_launch_flags(["-r", "hello"])
        self.assertTrue(values["random"])
        self.assertEqual(rest, ["hello"])

        values, rest = _consume_launch_flags(["-n", "-pwork", "hi"])
        self.assertTrue(values["dry-run"])
        self.assertEqual(values["profile"], "work")
        self.assertEqual(rest, ["hi"])

        values, rest = _consume_launch_flags(["-b", "/opt/agy", "--version"])
        self.assertEqual(values["binary"], "/opt/agy")
        self.assertEqual(rest, ["--version"])

        # A late -r (agy's own?) must not be consumed after a non-flag token.
        values, rest = _consume_launch_flags(["chat", "-r"])
        self.assertIsNone(values["random"])
        self.assertEqual(rest, ["chat", "-r"])

    def test_alias_subcommand_runs(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "agydra", "ls"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Fresh store: the friendly empty-state message, same handler as list.
        self.assertIn("no profiles", result.stdout)


class TestLauncherFlagExtraction(unittest.TestCase):
    def test_extracts_all_flag_forms(self):
        from agydra.cli import _consume_launch_flags

        # argv -> (expected flag values, expected args forwarded to agy)
        cases = {
            # First -p / --profile wins; later -p is passed to agy.
            ("--profile", "work", "--version"):
                ({"profile": "work"}, ["--version"]),
            ("-p", "work", "--version"):
                ({"profile": "work"}, ["--version"]),
            ("-pwork", "--version"):
                ({"profile": "work"}, ["--version"]),
            ("--profile=work", "-p", "text"):
                ({"profile": "work"}, ["-p", "text"]),
            # -p first, then --binary: both captured, later args go to agy.
            ("-p", "lab", "--binary", "/opt/agy", "--version"):
                ({"profile": "lab", "binary": "/opt/agy"}, ["--version"]),
            # --binary first (no -p before it): only --binary captured; -p after
            # is agy's own flag. This is the documented "agy intercepts -p"
            # behavior and is intentional.
            ("--binary", "/opt/agy", "--version"):
                ({"profile": None, "binary": "/opt/agy"}, ["--version"]),
            # -p FIRST then --binary, then a repeat -p: the repeat is agy's.
            ("-p", "lab", "--binary", "/opt/agy", "-p", "hello"):
                ({"profile": "lab", "binary": "/opt/agy"}, ["-p", "hello"]),
            # --dry-run alone: no profile yet (resolver errors later, that's OK).
            ("--dry-run", "--version"):
                ({"profile": None, "dry-run": True}, ["--version"]),
            # New short forms.
            ("-n", "--version"): ({"dry-run": True}, ["--version"]),
            ("-b/opt/agy", "--version"): ({"binary": "/opt/agy"}, ["--version"]),
            ("--binary=/opt/agy", "--version"):
                ({"binary": "/opt/agy"}, ["--version"]),
        }
        for argv, (expected_flags, expected_rest) in cases.items():
            with self.subTest(argv=argv):
                values, rest = _consume_launch_flags(list(argv))
                self.assertEqual(rest, expected_rest)
                for key, expected in expected_flags.items():
                    self.assertEqual(values[key], expected, key)

    def test_first_occurrence_wins_and_rest_forwarded(self):
        from agydra.cli import _consume_launch_flags

        # A repeated flag ends agydra's section: the repeat and everything
        # after it belongs to agy (agy itself uses -p for --print).
        values, rest = _consume_launch_flags(
            ["-p", "lab", "--binary", "/opt/agy", "-p", "hello"]
        )
        self.assertEqual(values["profile"], "lab")
        self.assertEqual(values["binary"], "/opt/agy")
        self.assertEqual(rest, ["-p", "hello"])

        # "--" stops the parser, everything after is forwarded verbatim.
        values, rest = _consume_launch_flags(["--", "-p", "after-separator"])
        self.assertIsNone(values["profile"])
        self.assertEqual(rest, ["-p", "after-separator"])

        # A non-flag token before any flag ends the scan: everything is agy's.
        values, rest = _consume_launch_flags(["chat", "-p", "work"])
        self.assertIsNone(values["profile"])
        self.assertEqual(rest, ["chat", "-p", "work"])


class TestLateProfileFlagWarning(unittest.TestCase):
    """Bug guard: agydra must warn when a value flag slipped past the
    extractor (e.g. ``agydra "chat" -pwork``) — agy itself uses ``-p``."""

    def _capture(self, raw, consumed=None):
        import io
        from contextlib import redirect_stderr

        from agydra.cli import _consume_launch_flags, _warn_late_flags

        values, _rest = _consume_launch_flags(consumed if consumed is not None else [])
        buf = io.StringIO()
        with redirect_stderr(buf):
            _warn_late_flags(values, raw)
        return buf.getvalue()

    def test_separate_p_warns(self):
        out = self._capture(["chat", "-p", "work"])
        self.assertIn("must come first", out)
        self.assertIn("-p work", out)

    def test_separate_profile_warns(self):
        out = self._capture(["chat", "--profile", "work"])
        self.assertIn("must come first", out)

    def test_attached_p_dash_pwork_warns(self):
        # Regression: attached form was silently forwarded before.
        out = self._capture(["chat", "-pwork"])
        self.assertIn("must come first", out)
        self.assertIn("-pwork", out)

    def test_profile_equals_warns(self):
        out = self._capture(["chat", "--profile=work"])
        self.assertIn("must come first", out)

    def test_late_binary_warns(self):
        out = self._capture(["chat", "-b", "/opt/agy"])
        self.assertIn("must come first", out)
        self.assertIn("-b /opt/agy", out)

    def test_double_dash_terminates_scan(self):
        # Anything after ``--`` belongs to agy; no warning should be raised.
        out = self._capture(["chat", "--", "-p", "work"])
        self.assertEqual(out, "")

    def test_no_warning_when_profile_already_consumed(self):
        out = self._capture(["chat", "-p", "x"], consumed=["-p", "work"])
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
