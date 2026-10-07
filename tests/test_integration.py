"""Integration: agydra launches agy with the overlay; the generic store is never touched."""
import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import locks
import platforms
from store import Store

from conftest import (
    CLI_ENTRY,
    REPO_ROOT,
    BaseCase,
    _FAKE_AGY_SOURCE,
    _make_jwt,
    cli_environment,
    held_cli_session,
    run_cli,
    spawn_cli,
    write_fake_agy,
)


class TestFakeAgyFixture(unittest.TestCase):
    def test_windows_fake_binary_creates_missing_parent(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "missing" / "agy"
            with mock.patch("conftest.sys.platform", "win32"):
                created = write_fake_agy(binary)
            self.assertEqual(created, binary.with_suffix(".cmd"))
            self.assertTrue(created.is_file())

    def test_windows_launcher_runs_the_same_python_source_and_forwards_exit_code(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "bin" / "agy"
            with mock.patch("conftest.sys.platform", "win32"):
                launcher = write_fake_agy(binary)
            program = binary.with_name("agy-fake.py")
            self.assertEqual(program.read_text(encoding="utf-8"), _FAKE_AGY_SOURCE)
            command = launcher.read_text(encoding="utf-8")
            self.assertIn(f'"{sys.executable}" "{program}" %*', command)
            self.assertIn("exit /b %ERRORLEVEL%", command)
            self.assertTrue((binary.parent / "heartbeat_child.py").is_file())

    def test_fake_source_is_deterministic_and_stdlib_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "agy-fake.py"
            program.write_text(_FAKE_AGY_SOURCE, encoding="utf-8")
            home = Path(directory) / "home"
            home.mkdir()
            env = dict(os.environ, AGYDRA_PROFILE="demo", HOME=str(home), USERPROFILE=str(home))
            outputs = [
                subprocess.run(
                    [sys.executable, str(program)], capture_output=True, text=True, env=env
                )
                for _ in range(2)
            ]
            version = subprocess.run(
                [sys.executable, str(program), "--version"], capture_output=True, text=True
            )
            self.assertEqual(outputs[0].stdout, outputs[1].stdout)
            self.assertEqual(outputs[0].stdout, f"PROFILE=demo\nHOME={home}\n")
            self.assertEqual(version.stdout.strip(), "fake-agy 1.0")
            self.assertEqual((home / ".gemini" / "fake-agy-wrote").read_text(), "wrote")


class TestCliHelperIndependence(BaseCase):
    def test_environment_prepends_repo_root_and_keeps_existing_pythonpath(self):
        os.environ["PYTHONPATH"] = "/opt/elsewhere"
        env = cli_environment({"EXTRA": "1"})
        self.assertEqual(
            env["PYTHONPATH"], os.pathsep.join([str(REPO_ROOT), "/opt/elsewhere"])
        )
        self.assertEqual(env["EXTRA"], "1")
        os.environ.pop("PYTHONPATH")
        self.assertEqual(cli_environment()["PYTHONPATH"], str(REPO_ROOT))

    def test_cli_runs_from_a_directory_outside_the_repository(self):
        outside = self._tmp / "unrelated-cwd"
        outside.mkdir()
        self.assertFalse(str(outside).startswith(str(REPO_ROOT)))
        os.environ.pop("PYTHONPATH", None)
        result = run_cli("ls", cwd=outside)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no profiles", result.stdout)

    def test_cli_never_goes_through_bootstrap_or_the_repo_venv(self):
        import shutil

        installation = self._tmp / "checkout-without-venv"
        installation.mkdir()
        for module in REPO_ROOT.glob("*.py"):
            shutil.copy2(module, installation / module.name)
        self.assertFalse((installation / ".venv").exists())
        outside = self._tmp / "bootstrap-cwd"
        outside.mkdir()
        result = run_cli(
            "ls", cwd=outside, extra_env={"PYTHONPATH": str(installation)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no profiles", result.stdout)
        self.assertNotIn("running setup", result.stdout)
        self.assertFalse((installation / ".venv").exists())
        self.assertFalse((self.fake_home / ".local" / "bin" / "agydra").exists())

    def test_cli_entry_is_the_console_script_body(self):
        self.assertEqual(CLI_ENTRY, "import sys; from cli import main; sys.exit(main())")

    def test_spawned_cli_does_not_depend_on_the_parent_cwd(self):
        outside = self._tmp / "spawn-cwd"
        outside.mkdir()
        process = spawn_cli(
            "ls", cwd=outside, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        out, err = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("no profiles", out)


class TestFixtureEnvironment(BaseCase):
    def test_host_data_paths_are_sandboxed(self):
        root = self._tmp.resolve()
        for name in ("HOME", "LOCALAPPDATA", "XDG_DATA_HOME"):
            with self.subTest(name=name):
                data_path = Path(os.environ[name]).resolve()
                self.assertTrue(data_path.is_relative_to(root))


class TestIntegration(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_launch_redirects_agy_to_profile_store(self):
        real_gemini = self.fake_home / ".gemini"
        marker = real_gemini / "generic-marker"
        marker.write_text("do-not-touch", encoding="utf-8")
        before = marker.read_text(encoding="utf-8")

        result = self._run_cli("-p", "work")
        self.assertEqual(result.returncode, 0, result.stderr)

        wrote = self.store.profile_data_dir("work") / "fake-agy-wrote"
        self.assertTrue(wrote.exists(), "agy did not write to the profile store")
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
        self.assertTrue(
            (self.fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists()
        )

    def test_dry_run_prints_plan_and_executes_nothing(self):
        result = self._run_cli("-p", "work", "--dry-run", "anything")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile : work", result.stdout)
        self.assertIn("binary", result.stdout)
        self.assertIn("overlay :", result.stdout)
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
        (src / "antigravity-cli").mkdir(parents=True, exist_ok=True)
        (src / "antigravity-cli" / "antigravity-oauth-token").write_text(
            '{"c": 3}', encoding="utf-8"
        )
        result = self._run_cli("share-config", "work", "lab")
        self.assertEqual(result.returncode, 0, result.stderr)
        lab = self.store.profile_data_dir("lab")
        self.assertTrue((lab / "settings.json").exists())
        self.assertTrue((lab / "mcp.json").exists())
        self.assertFalse(
            (lab / "antigravity-cli" / "antigravity-oauth-token").exists(),
            "credentials must never be shared",
        )

    def test_readonly_commands_create_nothing(self):
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

    def test_list_shows_email_for_keychain_only_profile(self):
        """A profile created through the macOS keychain bridge (e.g. after
        `agydra login`) can have NO on-disk token file at all -- its email
        must still show up in `agydra list`, sourced from the private
        keychain slot backup (`<store>/keychain/<name>.secret`).

        The CLI runs with the bridge enabled but with a forbidden
        ``security`` shim first on PATH: any shell-out to the real keychain
        tool is recorded and fails the test, so the host keychain is never
        touched. The profile is created before the bridge is enabled."""
        if not platforms.is_macos():
            self.skipTest("macOS-only keychain bridge")
        import keychain

        self.store.create("kc")
        data_dir = self.store.profile_data_dir("kc")
        self.assertFalse((data_dir / account.AGY_CLI_DIR).exists())

        jwt = _make_jwt({"email": "kc@example.com"})
        token_json = json.dumps({
            "token": {"access_token": "a", "refresh_token": "r"},
            "auth_method": "consumer",
            "id_token": jwt,
        }).encode("utf-8")
        secret = b"go-keyring-base64:" + base64.b64encode(token_json)
        keychain.save_profile_slot(self.store, "kc", secret)

        shim_dir = self._tmp / "forbidden-bin"
        shim_dir.mkdir()
        calls = self._tmp / "security-calls.log"
        shim = shim_dir / "security"
        shim.write_text(
            '#!/bin/sh\necho "$@" >> "$FORBIDDEN_SECURITY_LOG"\nexit 1\n', encoding="utf-8"
        )
        shim.chmod(0o755)

        result = self._run_cli_with_env(
            "list",
            PATH=str(shim_dir) + os.pathsep + os.environ["PATH"],
            AGYDRA_NO_KEYCHAIN="",
            FORBIDDEN_SECURITY_LOG=str(calls),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kc@example.com", result.stdout)
        self.assertEqual(self.store.get("kc").email, "kc@example.com")
        self.assertFalse(
            calls.exists(), f"list shelled out to security: {calls.read_text() if calls.exists() else ''}"
        )

    def _run_cli_with_env(self, *args, **env):
        return run_cli(*args, cwd=self._tmp, extra_env=env)


class TestRandomProfileSelection(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")
        self.store.create("lab")
        for name in ("work", "lab"):
            cli_dir = self.store.profile_data_dir(name) / "antigravity-cli"
            cli_dir.mkdir(parents=True, exist_ok=True)
            (cli_dir / "antigravity-oauth-token").write_text(
                '{"access_token": "tok"}', encoding="utf-8"
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
        (
            self.store.profile_data_dir("lab") / "antigravity-cli" / "antigravity-oauth-token"
        ).unlink()
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PROFILE=work", result.stdout)

    def test_r_errors_when_none_authenticated(self):
        for name in ("work", "lab"):
            (
                self.store.profile_data_dir(name) / "antigravity-cli" / "antigravity-oauth-token"
            ).unlink()
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 1)
        self.assertIn("authenticate one with", result.stderr)

    def test_r_works_with_a_single_profile(self):
        self.store.delete("lab")
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PROFILE=work", result.stdout)

    def test_r_reuses_the_only_profile_while_it_is_busy(self):
        self.store.delete("lab")
        with held_cli_session("-p", "work", cwd=self._tmp):
            result = self._run_cli("-r")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PROFILE=work", result.stdout)

    def test_r_errors_with_no_profiles(self):
        self.store.delete("work")
        self.store.delete("lab")
        result = self._run_cli("-r")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no profiles exist yet", result.stderr)

    def test_r_respects_live_session(self):
        with held_cli_session("-p", "work", cwd=self._tmp):
            result = self._run_cli("-r")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PROFILE=lab", result.stdout)


class TestBusyGuards(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_delete_refuses_busy_profile(self):
        handle = locks.try_lock(self.store, "work")
        try:
            result = self._run_cli("delete", "work", "-f")
            self.assertEqual(result.returncode, 1)
            self.assertIn("holder state could not be verified", result.stderr)
        finally:
            handle.release()

    def test_delete_refuses_busy_profile_names_holder_pid(self):
        """A legacy raw PID recorded by ``LockHandle.record_holder_pid()``
        is not a verified lease holder: the busy message must say the
        holder state could not be verified and must never suggest
        ``kill`` for it."""
        if sys.platform.startswith("win"):
            self.skipTest("PID recording is POSIX-only by design")
        handle = locks.try_lock(self.store, "work")
        handle.record_holder_pid()
        try:
            result = self._run_cli("delete", "work", "-f")
            self.assertEqual(result.returncode, 1)
            self.assertIn("holder state could not be verified", result.stderr)
            self.assertNotIn("kill", result.stderr)
            self.assertNotIn(f"PID {os.getpid()}", result.stderr)
        finally:
            handle.release()

    def test_delete_busy_message_falls_back_without_pid_on_bad_lock_content(self):
        """If the lock file's content doesn't parse as a PID (corrupted,
        truncated, written by an older agydra version), the message must
        fall back to the generic wording instead of printing garbage."""
        handle = locks.try_lock(self.store, "work")
        try:
            with open(locks.lock_path(self.store, "work"), "wb") as fh:
                fh.write(b"not-a-pid")
            result = self._run_cli("delete", "work", "-f")
            self.assertEqual(result.returncode, 1)
            self.assertIn("holder state could not be verified", result.stderr)
            self.assertNotIn("PID", result.stderr)
        finally:
            handle.release()

    def test_delete_allowed_when_free(self):
        result = self._run_cli("delete", "work", "-f", "--no-backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.store.exists("work"))

    def test_dashed_rm_alias_force_skips_confirmation_like_delete(self):
        """``-rm`` is delete's ``rm`` alias resolved via the single-dash
        subcommand dispatch form (``_resolve_subcommand``) -- INTENTIONALLY
        a subcommand, not a launcher-mode bundle, since ``rm`` only
        partially overlaps the launcher's short-flag letters (see
        ``test_partial_letter_overlap_alias_dispatches_as_subcommand`` in
        test_vocab_and_bundles.py). ``agydra -rm work -f`` must skip the
        confirmation prompt exactly like ``agydra delete work -f`` does,
        since the trailing ``-f`` is parsed as delete's own --force."""
        result = self._run_cli("-rm", "work", "-f", "--no-backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.store.exists("work"))

    def test_rename_refuses_busy_profile(self):
        handle = locks.try_lock(self.store, "work")
        try:
            result = self._run_cli("rename", "work", "new")
            self.assertEqual(result.returncode, 1)
            self.assertIn("holder state could not be verified", result.stderr)
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


class TestDeleteRecoversCorruptProfile(BaseCase):
    """A directory-only/corrupt profile used to be stuck: resolve_ref said
    'unknown profile ... create it', and create then said 'already exists'.
    cmd_delete must accept the literal name straight from
    store.unreadable_profiles() so the user has a way out."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.profile_dir("broken").mkdir(parents=True)

    def test_delete_removes_directory_only_profile(self):
        result = self._run_cli("delete", "broken", "-f", "--no-backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.store.profile_dir("broken").exists())

    def test_delete_refuses_busy_corrupt_profile(self):
        handle = locks.try_lock(self.store, "broken")
        try:
            result = self._run_cli("delete", "broken", "-f")
            self.assertEqual(result.returncode, 1)
            self.assertIn("holder state could not be verified", result.stderr)
            self.assertTrue(self.store.profile_dir("broken").exists())
        finally:
            handle.release()

    def test_create_recovers_after_deleting_corrupt_profile(self):
        """The original stuck-user scenario end to end: delete the corrupt
        directory, then create a fresh profile with the same name."""
        result = self._run_cli("delete", "broken", "-f", "--no-backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._run_cli("create", "broken")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.store.exists("broken"))


class TestImportFlow(BaseCase):
    """`import` takes the TARGET profile name; the generic ~/.gemini source
    is auto-detected. These lock in the corrective UX for path-as-ref."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_path_as_ref_gets_corrective_error(self):
        result = self._run_cli("import", str(self.fake_home / ".gemini"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("TARGET profile name, not a path", result.stderr)
        self.assertIn("agydra import <profile-name>", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_import_copies_generic_into_profile(self):
        result = self._run_cli("import", "work")
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = (
            self.store.profile_data_dir("work") / "antigravity-cli" / "antigravity-oauth-token"
        )
        self.assertTrue(imported.exists(), "generic data must land in the profile")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            (self.fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists()
        )

    def test_import_custom_source_flag(self):
        custom = self._tmp / "custom-src"
        custom.mkdir()
        (custom / "settings.json").write_text('{"x": 1}', encoding="utf-8")
        result = self._run_cli("import", "work", "--source", str(custom))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.store.profile_data_dir("work") / "settings.json").exists()
        )

    def test_import_missing_source_reports_path(self):
        result = self._run_cli(
            "import", "work", "--source", str(self._tmp / "nope")
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("source directory not found", result.stderr)

    def test_import_requires_existing_profile(self):
        result = self._run_cli("import", "ghost")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown profile", result.stderr)


class TestAliasesAndFlagTable(BaseCase):
    def test_all_canonical_aliases_resolve(self):
        from cli import _CANONICAL, _SUBCOMMAND_ALIASES

        for canonical, aliases in _SUBCOMMAND_ALIASES.items():
            self.assertEqual(_CANONICAL[canonical], canonical)
            for alias in aliases:
                self.assertEqual(_CANONICAL[alias], canonical)

    def test_alias_dispatch_list(self):
        from cli import _CANONICAL

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
        from cli import _CANONICAL

        self.assertIsNone(_CANONICAL.get("totally-not-a-subcommand"))
        self.assertIsNone(_CANONICAL.get(""))

    def test_help_documents_every_subcommand(self):
        import cli
        from cli import _SUBCOMMAND_ALIASES, _SUBCOMMAND_HELP

        self.assertEqual(set(_SUBCOMMAND_HELP), set(_SUBCOMMAND_ALIASES))
        handlers = {
            name[len("cmd_"):].replace("_", "-")
            for name in dir(cli)
            if name.startswith("cmd_")
        }
        expected = set(_SUBCOMMAND_ALIASES) - {"version", "help"}
        self.assertEqual(handlers, expected)
        order = list(_SUBCOMMAND_ALIASES)
        self.assertLess(order.index("create"), order.index("list"))
        self.assertLess(order.index("list"), order.index("rename"))

    def test_management_help_includes_examples_section(self):
        import io
        from contextlib import redirect_stdout

        from cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["help"]), 0)
        out = buf.getvalue()
        for token in ("management:", "examples:", "create", "login",
                      "import main", "agydra -p"):
            self.assertIn(token, out)

    def test_help_is_plain_in_captured_pipes(self):
        import io
        from contextlib import redirect_stdout

        from cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["help"])
        self.assertNotIn("\x1b[", buf.getvalue())

    def test_help_is_colored_under_force_color(self):
        import io
        import os
        from contextlib import redirect_stdout

        from cli import main

        old = os.environ.get("FORCE_COLOR")
        old_no = os.environ.get("NO_COLOR")

        def capture_help():
            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["help"])
            return buf.getvalue()

        try:
            os.environ["FORCE_COLOR"] = "0"
            os.environ["NO_COLOR"] = ""
            plain = capture_help()
            os.environ["FORCE_COLOR"] = "1"
            os.environ.pop("NO_COLOR", None)
            out = capture_help()
        finally:
            if old is None:
                os.environ.pop("FORCE_COLOR", None)
            else:
                os.environ["FORCE_COLOR"] = old
            if old_no is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = old_no
        self.assertIn("\x1b[", out)
        from ui import strip_ansi
        self.assertEqual(strip_ansi(out).split(), plain.split())
        self.assertIn("management:", strip_ansi(out))

    def test_no_color_wins_over_force_color(self):
        import os

        import ui

        old_f = os.environ.get("FORCE_COLOR")
        old_n = os.environ.get("NO_COLOR")
        os.environ["FORCE_COLOR"] = "1"
        os.environ["NO_COLOR"] = "1"
        try:
            self.assertFalse(ui.color_enabled())
        finally:
            for key, old in (("FORCE_COLOR", old_f), ("NO_COLOR", old_n)):
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

    def test_paint_unknown_style_raises(self):
        import os

        import ui

        old = os.environ.get("FORCE_COLOR")
        os.environ["FORCE_COLOR"] = "1"
        try:
            with self.assertRaises(KeyError):
                ui.paint("x", "sparkly-rainbow")
        finally:
            if old is None:
                os.environ.pop("FORCE_COLOR", None)
            else:
                os.environ["FORCE_COLOR"] = old

    def test_flag_table_shape_and_extractor(self):
        from cli import _LAUNCH_FLAGS, _consume_launch_flags

        for key, (short, long_, takes_value, metavar, help_text) in _LAUNCH_FLAGS.items():
            self.assertTrue(short.startswith("-"))
            self.assertTrue(long_.startswith("--"))
            self.assertTrue(help_text)
            if takes_value:
                self.assertIsNotNone(metavar)
            else:
                self.assertIsNone(metavar)

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

        values, rest = _consume_launch_flags(["chat", "-r"])
        self.assertIsNone(values["random"])
        self.assertEqual(rest, ["chat", "-r"])

    def test_alias_subcommand_runs(self):
        result = self._run_cli("ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no profiles", result.stdout)


class TestLauncherFlagExtraction(unittest.TestCase):
    def test_extracts_all_flag_forms(self):
        from cli import _consume_launch_flags

        cases = {
            ("--profile", "work", "--version"):
                ({"profile": "work"}, ["--version"]),
            ("-p", "work", "--version"):
                ({"profile": "work"}, ["--version"]),
            ("-pwork", "--version"):
                ({"profile": "work"}, ["--version"]),
            ("--profile=work", "-p", "text"):
                ({"profile": "work"}, ["-p", "text"]),
            ("-p", "lab", "--binary", "/opt/agy", "--version"):
                ({"profile": "lab", "binary": "/opt/agy"}, ["--version"]),
            ("--binary", "/opt/agy", "--version"):
                ({"profile": None, "binary": "/opt/agy"}, ["--version"]),
            ("-p", "lab", "--binary", "/opt/agy", "-p", "hello"):
                ({"profile": "lab", "binary": "/opt/agy"}, ["-p", "hello"]),
            ("--dry-run", "--version"):
                ({"profile": None, "dry-run": True}, ["--version"]),
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
        from cli import _consume_launch_flags

        values, rest = _consume_launch_flags(
            ["-p", "lab", "--binary", "/opt/agy", "-p", "hello"]
        )
        self.assertEqual(values["profile"], "lab")
        self.assertEqual(values["binary"], "/opt/agy")
        self.assertEqual(rest, ["-p", "hello"])

        values, rest = _consume_launch_flags(["--", "-p", "after-separator"])
        self.assertIsNone(values["profile"])
        self.assertEqual(rest, ["-p", "after-separator"])

        values, rest = _consume_launch_flags(["chat", "-p", "work"])
        self.assertIsNone(values["profile"])
        self.assertEqual(rest, ["chat", "-p", "work"])


class TestLateProfileFlagWarning(unittest.TestCase):
    """Bug guard: agydra must warn when a value flag slipped past the
    extractor (e.g. ``agydra "chat" -pwork``) — agy itself uses ``-p``."""

    def _capture(self, raw, consumed=None):
        import io
        from contextlib import redirect_stderr

        from cli import _consume_launch_flags, _warn_late_flags

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
        out = self._capture(["chat", "--", "-p", "work"])
        self.assertEqual(out, "")

    def test_no_warning_when_profile_already_consumed(self):
        out = self._capture(["chat", "-p", "x"], consumed=["-p", "work"])
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
