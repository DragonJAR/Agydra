"""Public Claude Code CLI contracts in isolated temporary stores."""
from __future__ import annotations

import contextlib
import base64
import io
import json
import os
import sys
import subprocess
import shlex
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from conftest import BaseCase
import account
import cli
import usage
import usage_snapshot
from store import Store, StoreError


class TestClaudeCli(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        os.environ["AGYDRA_CLAUDE_BIN"] = sys.executable

    def _snapshot(self, name="cc", quality="observed", reset=None):
        result = usage.UsageResult(
            name=name,
            engine="claude",
            ok=True,
            groups=[usage.UsageGroup("Claude Code", [
                usage.UsageBucket("five_hour", "Five hour", "5h", 0.75,
                                  reset or datetime.now(timezone.utc) + timedelta(hours=1)),
            ])],
        )
        result.source = "claude_status_line"
        result.quality = quality
        result.observed_at = datetime.now(timezone.utc)
        result.identity_verified = False
        return result

    def test_create_help_and_no_default_config_inheritance(self):
        default = self.store.create("work")
        self.store.set_default(default.name)
        (self.store.profile_data_dir("work") / "settings.json").write_text('{"private":true}')
        result = self._run_cli("create", "cc", "-e", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = self.store.get("cc")
        self.assertEqual(profile.engine, "claude")
        self.assertFalse((self.store.profile_data_dir("cc", engine="claude") / "settings.json").exists())
        self.assertEqual(self.store.profile_data_dir("cc", engine="claude"),
                         self.store.root / "claude-config" / str(profile.seq))
        self.assertIn("claude", self._run_cli("create", "--help").stdout)
        self.assertIn("Claude Code", self._run_cli("help").stdout)

    def test_login_native_arguments_and_no_codex_flags(self):
        self.store.create("cc", engine="claude")
        original_build_plan = cli.runner.build_plan
        with patch.object(account, "claude_auth_status", side_effect=AssertionError("dry-run must not probe native auth")) as auth_probe, patch.object(cli.runner, "build_plan", wraps=original_build_plan) as build_plan, patch.object(cli.runner, "run", return_value=0) as run:
            self.assertEqual(cli.cmd_login(self.store, SimpleNamespace(ref="cc", dry_run=True, force=False)), 0)
        auth_probe.assert_not_called()
        self.assertTrue(build_plan.call_args.kwargs["read_only"])
        plan = run.call_args.args[0]
        self.assertEqual(plan.args[-2:], ["auth", "login"])
        self.assertNotIn("--no-daemon", plan.args)
        self.assertEqual(plan.env_home_var, "CLAUDE_CONFIG_DIR")
        self.assertEqual(plan.env_home_value, self.store.claude_config_dir("cc"))
        env = cli.runner.isolation.isolated_env(plan.overlay, {}, engine="claude")
        self.assertEqual(env["HOME"], str(self.fake_home))

    def test_login_invalidates_generation_under_lock_before_native_process(self):
        import claude_usage

        self.store.create("cc", engine="claude")
        events = []
        invalidate = claude_usage.invalidate_profile_usage

        def locked_invalidate(store, name):
            self.assertTrue(cli.locks.is_locked(store, name))
            events.append("invalidate")
            return invalidate(store, name)

        def native_login(argv, env):
            events.append("native login")
            self.assertEqual(argv[-2:], ["auth", "login"])
            self.assertEqual(env[claude_usage.GENERATION_ENV], "1")
            self.assertTrue(cli.locks.is_locked(self.store, "cc"))
            return 0

        with patch.object(account, "claude_auth_status", return_value=account.ClaudeAuthStatus("unauthenticated")), patch.object(claude_usage, "invalidate_profile_usage", side_effect=locked_invalidate), patch.object(cli.runner.platforms, "run_wait", side_effect=native_login), patch.object(cli.runner.platforms, "launch_argv", side_effect=native_login), patch.object(cli.runner.platforms, "drain_tty_input"), contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_login(self.store, SimpleNamespace(ref="cc", dry_run=False, force=True))
        self.assertEqual(code, 0)
        self.assertEqual(events, ["invalidate", "native login"])
        self.assertFalse(cli.locks.is_locked(self.store, "cc"))

    def test_rename_and_delete_preserve_identity_and_bypass_antigravity_keychain(self):
        self.store.create("cc", engine="claude")
        seq = self.store.get("cc").seq
        config_dir = self.store.claude_config_dir("cc")
        (config_dir / "settings.json").write_text('{"example":true}')
        with patch.object(cli.keychain, "rename_profile_slot", side_effect=AssertionError("Claude must not swap Antigravity slots")), patch.object(Store, "_guard_claude_supervisor"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_rename(self.store, SimpleNamespace(old="cc", new="renamed")), 0)
        self.assertEqual(self.store.get("renamed").seq, seq)
        self.assertEqual(self.store.claude_config_dir("renamed"), config_dir)
        self.assertTrue((config_dir / "settings.json").is_file())
        with patch.object(cli.keychain, "purge_profile_slot", side_effect=AssertionError("Claude must not purge Antigravity slots")), patch.object(Store, "_guard_claude_supervisor"), patch.object(account, "claude_auth_status", return_value=account.ClaudeAuthStatus("unauthenticated")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_delete(self.store, SimpleNamespace(ref="renamed", force=True, no_backup=False)), 0)
        self.assertFalse(config_dir.exists())
        backups = list(self.store.backups_dir.glob("*.zip"))
        self.assertEqual(len(backups), 1)
        with zipfile.ZipFile(backups[0]) as archive:
            config_files = [name for name in archive.namelist() if name.endswith("settings.json")]
            self.assertEqual(len(config_files), 1)
            self.assertEqual(json.loads(archive.read(config_files[0])), {"example": True})

    def test_status_and_list_probe_native_auth_once(self):
        self.store.create("cc", engine="claude")
        native = account.ClaudeAuthStatus("authenticated", email="cc@example.test")
        for command, args in ((cli.cmd_status, SimpleNamespace(ref="cc", engine=None, dry_run=False)),
                              (cli.cmd_list, SimpleNamespace())):
            with patch.object(account, "claude_auth_status", return_value=native) as probe, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(command(self.store, args), 0)
                self.assertEqual(probe.call_count, 1)
                self.assertIn("cc@example.test", out.getvalue())
                self.assertIn("authenticated", out.getvalue())

    def test_import_and_share_reject_claude_without_copying(self):
        self.store.create("cc", engine="claude")
        self.store.create("work")
        source = self._tmp / "source"
        source.mkdir()
        (source / ".credentials.json").write_text('{"secret":"never-copy"}')
        with self.assertRaisesRegex(StoreError, "Claude Code"):
            cli.cmd_import(self.store, SimpleNamespace(ref="cc", source=str(source)))
        with self.assertRaisesRegex(StoreError, "Claude Code"):
            cli._share_config(self.store, "work", ["cc"])
        self.assertEqual(list(self.store.claude_config_dir("cc").iterdir()), [])

    def _live(self, name, five, seven, reset5=None):
        now = datetime.now(timezone.utc)
        buckets = []
        if five is not None:
            buckets.append(usage.UsageBucket("claude-five-hour", "5 Hours", "5h", five, reset5))
        buckets.append(usage.UsageBucket("claude-seven-day", "7 Days", "weekly", seven, now + timedelta(days=2, hours=9)))
        return usage.UsageResult(
            name=name, engine="claude", ok=True, groups=[usage.UsageGroup("Claude Code", buckets)],
            source="claude_cli_usage", quality="observed", observed_at=now, identity_verified=True,
        )

    def test_compact_claude_section_uses_the_standard_columns_and_detail_keeps_the_prose(self):
        self.store.create("cc", engine="claude")
        result = self._snapshot()
        with patch.object(account, "sync_profile_email", return_value=None), patch.object(usage, "gather_usage_report", return_value=[result]), contextlib.redirect_stdout(io.StringIO()) as compact:
            self.assertEqual(cli.cmd_usage(self.store, SimpleNamespace(ref=None)), 0)
        with patch.object(usage, "query_profile_usage", return_value=result), contextlib.redirect_stdout(io.StringIO()) as detail:
            self.assertEqual(cli.cmd_usage(self.store, SimpleNamespace(ref="cc")), 0)
        table = compact.getvalue()
        self.assertIn("ANTHROPIC CLAUDE CODE", table)
        header = next(line for line in table.splitlines() if "PROFILE" in line and "STATE" in line)
        for column in ("#", "PROFILE", "ACCOUNT", "AVAILABLE", "WK · 5H", "↻", "STATE"):
            self.assertIn(column, header)
        row = next(line for line in table.splitlines() if " cc " in line)
        self.assertIn("snapshot", row)
        for prose in cli._claude_usage_lines(result, 10)[:2]:
            self.assertNotIn(prose, table)
            self.assertIn(prose, detail.getvalue())
        self.assertIn("identity unverified", detail.getvalue())
        self.assertNotIn("source:", table)
        self.assertNotIn("USE NOW", table)
        self.assertNotIn("CLAUDE + GPT", table)

    def test_live_rows_fill_the_shared_columns_and_feed_the_use_now_line(self):
        for name in ("claudio", "claudia", "viejo"):
            self.store.create(name, engine="claude")
        results = [
            self._live("claudio", 0.49, 0.24, datetime.now(timezone.utc) + timedelta(minutes=5)),
            self._live("claudia", 1.0, 0.86),
            usage.UsageResult(name="viejo", engine="claude", ok=True, quality="stale",
                              source="claude_status_line", identity_verified=False,
                              error="live quota unavailable: claude /usage timed out"),
        ]
        with patch.object(account, "sync_profile_email", return_value=None), patch.object(usage, "gather_usage_report", return_value=results), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_usage(self.store, SimpleNamespace(ref=None))
        lines = out.getvalue().splitlines()
        claudio = next(line for line in lines if " claudio " in line)
        claudia = next(line for line in lines if " claudia " in line)
        viejo = next(line for line in lines if " viejo " in line)
        self.assertRegex(claudio, r"24\s+24 ·\s+49\s+\d+[dhm]( \d+[hm])?\s+live")
        self.assertRegex(claudia, r"86\s+86 · 100\s+\d+[dhm]( \d+[hm])?\s+live")
        self.assertRegex(viejo, r"unavailable\s+stale")
        self.assertNotIn("timed out", out.getvalue())
        self.assertIn("Claude Code → claudia 86%", out.getvalue())
        self.assertNotIn("claudio 24%", out.getvalue())

    def test_partial_or_untrusted_readings_never_fill_the_quota_columns(self):
        for name in ("parcial", "ambigua"):
            self.store.create(name, engine="claude")
        partial = self._live("parcial", None, 0.30)
        partial.quality = "unknown"
        ambiguous = self._live("ambigua", 0.5, 0.5)
        ambiguous.quality = "ambiguous"
        with patch.object(account, "sync_profile_email", return_value=None), \
                patch.object(usage, "gather_usage_report", return_value=[partial, ambiguous]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_usage(self.store, SimpleNamespace(ref=None))
        lines = out.getvalue().splitlines()
        for name, state in (("parcial", "unknown"), ("ambigua", "ambiguous")):
            row = next(line for line in lines if f" {name} " in line)
            self.assertRegex(row, rf"unavailable\s+{state}")
            self.assertNotIn("█", row)
        self.assertNotIn("USE NOW", out.getvalue())

    def test_account_column_is_filled_once_for_claude_profiles_only(self):
        self.store.create("cc", engine="claude")
        self.store.create("work")
        known = self.store.create("known", engine="claude")
        known.email = "known@example.com"
        self.store.save(known)
        results = [
            usage.UsageResult(name="work", ok=False, error="not authenticated"),
            self._live("cc", 0.5, 0.5),
            self._live("known", 0.5, 0.5),
        ]
        with patch.object(account, "sync_profile_email", return_value="dev@example.com") as sync, \
                patch.object(usage, "gather_usage_report", return_value=results), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_usage(self.store, SimpleNamespace(ref=None))
        sync.assert_called_once_with(self.store, "cc")
        text = out.getvalue()
        self.assertIn("dev@example.com", next(line for line in text.splitlines() if " cc " in line))
        self.assertIn("known@example.com", next(line for line in text.splitlines() if " known " in line))
        with patch.object(account, "sync_profile_email", side_effect=OSError("boom")), \
                patch.object(usage, "gather_usage_report", return_value=results), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_usage(self.store, SimpleNamespace(ref=None)), 0)

    def test_expired_partial_snapshot_does_not_infer_full_quota(self):
        result = self._snapshot(reset=datetime.now(timezone.utc) - timedelta(seconds=1))
        rendered = "\n".join(cli._claude_usage_lines(result, 10))
        self.assertIn("Five hour: unknown", rendered)
        self.assertNotIn("100", rendered)
        self.assertNotIn("75", rendered)
        self.assertNotIn("Weekly", rendered)
        for quality in ("stale", "unknown", "ambiguous", "corrupt"):
            result.quality = quality
            self.assertIn("Five hour: unknown", "\n".join(cli._claude_usage_lines(result, 10)))

    def test_partial_snapshot_shows_fresh_window_without_total_availability(self):
        result = self._snapshot(quality="unknown")
        rendered = "\n".join(cli._claude_usage_lines(result, 10))
        self.assertIn("75", rendered)
        self.assertNotIn("Weekly", rendered)
        self.assertNotIn("Five hour: unknown", rendered)
        for quality in ("stale", "ambiguous", "corrupt"):
            result.quality = quality
            self.assertNotIn("75", "\n".join(cli._claude_usage_lines(result, 10)))

    def test_engine_matching_and_foreign_credentials_do_not_leak(self):
        self.store.create("cc", engine="claude")
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            os.environ[key] = "foreign"
        plan = cli.runner.build_plan(self.store, ["--print", "hello"], flag_ref="cc", engine="claude")
        env = cli.runner.isolation.isolated_env(plan.overlay, {}, engine="claude")
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            self.assertNotIn(key, env)
        self.assertEqual(plan.args, ["--print", "hello"])
        with self.assertRaises(StoreError):
            cli.runner.build_plan(self.store, [], flag_ref="cc", engine="codex")

    def test_snapshot_never_changes_antigravity_recommendation(self):
        self.store.create("work")
        self.store.create("cc", engine="claude")
        agy = usage.UsageResult("work", True, [usage.UsageGroup("Claude and GPT models", [
            usage.UsageBucket("3p-weekly", "Weekly", "weekly", 0.4,
                              datetime.now(timezone.utc) + timedelta(days=1)),
        ])])
        with patch.object(usage, "gather_usage_report", return_value=[agy, self._snapshot()]), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_usage(self.store, SimpleNamespace(ref=None))
        recommendation = next(line for line in out.getvalue().splitlines() if "USE NOW" in line)
        self.assertIn("Claude/GPT → work 40%", recommendation)
        self.assertNotIn("cc", recommendation)

    def test_settings_json_is_opt_in_readonly_and_command_captures_in_sandbox(self):
        import claude_usage

        self.store = Store(self._tmp / "store spaces & % token $ quote's")
        self.store.create("cc", engine="claude")
        profile = self.store.get("cc")
        env = dict(os.environ)
        env.update(claude_usage.capture_environment(self.store, profile))
        env["CLAUDE_CONFIG_DIR"] = str(self.store.claude_config_dir_for_seq(profile.seq))
        before = {str(path.relative_to(self.store.root)): path.read_bytes()
                  for path in self.store.root.rglob("*") if path.is_file()}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_usage(self.store, SimpleNamespace(ref=None, claude_settings="cc"))
        settings = json.loads(out.getvalue())
        os.environ["AGYDRA_HOME"] = str(self.store.root)
        public = self._run_cli("usage", "--claude-settings", "cc")
        self.assertEqual(public.returncode, 0, public.stderr)
        public_settings = json.loads(public.stdout)
        self.assertEqual(public_settings["statusLine"]["type"], "command")
        if os.name != "nt":
            self.assertEqual(shlex.split(public_settings["statusLine"]["command"])[1:],
                             shlex.split(settings["statusLine"]["command"])[1:])
        self.assertEqual(settings["statusLine"]["type"], "command")
        command = settings["statusLine"]["command"]
        if os.name != "nt":
            arguments = shlex.split(command)
            self.assertEqual(arguments[0], sys.executable)
            self.assertEqual(arguments[1:3], ["-m", "claude_usage"])
            self.assertEqual(arguments[arguments.index("--store") + 1], str(self.store.root))
        after = {str(path.relative_to(self.store.root)): path.read_bytes()
                 for path in self.store.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        payload = {
            "session_id": str(uuid.uuid4()),
            "rate_limits": {
                "five_hour": {"used_percentage": 25, "resets_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())},
                "seven_day": {"used_percentage": 40, "resets_at": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())},
            },
        }
        env["PYTHONPATH"] = str(Path(cli.__file__).parent)
        captured = subprocess.run(public_settings["statusLine"]["command"], shell=True, input=json.dumps(payload), text=True,
                                  capture_output=True, env=env, cwd=self._tmp, timeout=10)
        self.assertEqual(captured.returncode, 0, captured.stderr)
        self.assertTrue(captured.stdout.strip())
        self.assertEqual(usage.query_profile_usage(self.store, "cc").quality, "observed")

    def test_windows_settings_command_uses_encoded_literal_arguments(self):
        self.store = Store(self._tmp / "store spaces & % token $ quote's")
        self.store.create("cc", engine="claude")
        with patch.object(cli.platforms, "is_windows", return_value=True), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_usage(self.store, SimpleNamespace(ref=None, claude_settings="cc"))
        command = json.loads(out.getvalue())["statusLine"]["command"]
        self.assertTrue(command.startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand "))
        script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertIn("store spaces & % token $", script)
        self.assertIn("quote''s", script)
        self.assertIn("'-m' 'claude_usage'", script)
        self.assertIn("[Console]::In.ReadToEnd()", script)
        self.assertNotIn("% token", command)
        self.assertNotIn("quote's", command)

    def test_usage_pending_rename_does_not_recover_or_mutate(self):
        self.store.create("cc", engine="claude")
        journal = self.store.root / "profile-rename.json"
        journal.write_text(json.dumps({"old": "cc", "new": "renamed", "broken": True}))
        before = {str(path.relative_to(self.store.root)): path.read_bytes()
                  for path in self.store.root.rglob("*") if path.is_file()}
        self._run_cli("usage", "cc")
        self._run_cli("usage")
        after = {str(path.relative_to(self.store.root)): path.read_bytes()
                 for path in self.store.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def _store_files(self):
        root = self.store.root
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_usage_command_uses_live_reader_without_auth_probe_or_profile_mutation(self):
        import claude_usage

        self.store.create("cc", engine="claude")
        before = self._store_files()
        synthetic = usage.UsageResult(
            name="cc", ok=True, engine="claude", source="live", quality="live"
        )
        with contextlib.redirect_stdout(io.StringIO()), patch.object(
            account, "claude_auth_status",
            side_effect=AssertionError("usage must not probe auth"),
        ), patch.object(
            claude_usage, "query_claude_usage_live", return_value=synthetic
        ) as live:
            cli.cmd_usage(self.store, SimpleNamespace(ref="cc"))
        after = self._store_files()

        live.assert_called_once()
        self.assertEqual(live.call_args.args[1], "cc")
        changed = {
            name
            for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
        }
        self.assertEqual(changed, {usage_snapshot.SNAPSHOT_FILENAME})


class TestClaudeCaptureEnable(BaseCase):
    """``usage --claude-settings PROFILE --apply`` writes the opt-in
    statusLine into the profile settings.json — explicit user action with
    merge guards: atomic write, never clobber an existing statusLine,
    never touch an unparseable file."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        os.environ["AGYDRA_CLAUDE_BIN"] = sys.executable

    def _settings_path(self, name="cc"):
        profile = self.store.get_readonly(name)
        return self.store.claude_config_dir_for_seq(profile.seq) / "settings.json"

    def _run_apply(self, name="cc"):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_usage(
                self.store, SimpleNamespace(
                    ref=None, claude_settings=name, claude_settings_apply=True,
                )
            )
        return rc, out.getvalue()

    def test_apply_writes_statusline_when_settings_absent(self):
        self.store.create("cc", engine="claude")
        rc, out = self._run_apply()
        self.assertEqual(rc, 0)
        settings = json.loads(self._settings_path().read_text(encoding="utf-8"))
        self.assertEqual(settings["statusLine"]["type"], "command")
        command = settings["statusLine"]["command"]
        if command.startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand "):
            command = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertIn("claude_usage", command)
        self.assertIn("capture enabled", out)

    def test_apply_merges_preserving_existing_keys(self):
        self.store.create("cc", engine="claude")
        path = self._settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": "opus", "verbose": True}), encoding="utf-8")
        rc, _ = self._run_apply()
        self.assertEqual(rc, 0)
        settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "opus")
        self.assertTrue(settings["verbose"])
        self.assertEqual(settings["statusLine"]["type"], "command")

    def test_apply_is_idempotent_when_statusline_matches(self):
        self.store.create("cc", engine="claude")
        self._run_apply()
        rc, out = self._run_apply()
        self.assertEqual(rc, 0)
        self.assertIn("already enabled", out)

    def test_apply_refuses_to_overwrite_foreign_statusline(self):
        self.store.create("cc", engine="claude")
        path = self._settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"statusLine": {"type": "command", "command": "my-own"}}),
            encoding="utf-8",
        )
        with self.assertRaises(StoreError) as ctx:
            self._run_apply()
        self.assertIn("already defines a statusLine", str(ctx.exception))
        settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settings["statusLine"]["command"], "my-own")

    def test_apply_refuses_unparseable_settings(self):
        self.store.create("cc", engine="claude")
        path = self._settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            self._run_apply()
        self.assertIn("not valid JSON", str(ctx.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "not json")

    def test_apply_requires_claude_profile(self):
        self.store.create("wk")
        with self.assertRaises(StoreError):
            self._run_apply("wk")

    def test_apply_validates_physical_config_directory(self):
        self.store.create("cc", engine="claude")
        config_dir = self.store.claude_config_dir("cc")
        outside = self._tmp / "outside-claude-config"
        outside.mkdir()
        config_dir.rmdir()
        config_dir.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(cli.isolation.IsolationError):
            self._run_apply()

        self.assertEqual(list(outside.iterdir()), [])

    def test_apply_refuses_a_busy_profile(self):
        self.store.create("cc", engine="claude")
        handle = cli.locks.try_lock(self.store, "cc")
        try:
            with self.assertRaises(cli.ProfileBusyError):
                self._run_apply()
        finally:
            handle.release()

    def test_apply_holds_mutation_lock_through_recheck_and_atomic_write(self):
        self.store.create("cc", engine="claude")
        original_get = self.store.get_readonly
        original_write = cli.atomic_write_text
        read_lock_checks = []
        write_lock_checks = []
        reads = [0]

        def inspect_read(name):
            reads[0] += 1
            if reads[0] > 1:
                handle = cli.locks.try_lock(self.store, name)
                read_lock_checks.append(handle is None)
                if handle is not None:
                    handle.release()
            return original_get(name)

        def inspect_write(path, text):
            handle = cli.locks.try_lock(self.store, "cc")
            write_lock_checks.append(handle is None)
            if handle is not None:
                handle.release()
            return original_write(path, text)

        with patch.object(self.store, "get_readonly", side_effect=inspect_read), patch.object(
            cli, "atomic_write_text", side_effect=inspect_write
        ):
            rc, _out = self._run_apply()

        self.assertEqual(rc, 0)
        self.assertEqual(read_lock_checks, [True, True])
        self.assertEqual(write_lock_checks, [True])

    def test_apply_fails_if_profile_is_deleted_before_mutation_lock(self):
        self.store.create("cc", engine="claude")
        original_get = self.store.get_readonly
        reads = [0]

        def delete_after_initial_read(name):
            reads[0] += 1
            profile = original_get(name)
            if reads[0] == 1:
                with patch.object(Store, "_guard_claude_supervisor"):
                    self.store.delete(name, backup=False)
            return profile

        with patch.object(self.store, "get_readonly", side_effect=delete_after_initial_read), patch.object(
            cli, "atomic_write_text"
        ) as write:
            with self.assertRaises(StoreError):
                self._run_apply()

        write.assert_not_called()
        self.assertFalse(self.store.exists("cc"))

    def test_apply_rechecks_profile_sequence_before_publication(self):
        import dataclasses

        self.store.create("cc", engine="claude")
        original_get = self.store.get_readonly
        reads = [0]

        def change_identity_on_final_read(name):
            reads[0] += 1
            profile = original_get(name)
            if reads[0] == 3:
                profile = dataclasses.replace(profile, seq=profile.seq + 1)
            return profile

        with patch.object(self.store, "get_readonly", side_effect=change_identity_on_final_read), patch.object(
            cli, "atomic_write_text"
        ) as write:
            with self.assertRaisesRegex(StoreError, "identity changed"):
                self._run_apply()

        write.assert_not_called()
