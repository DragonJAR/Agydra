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
        native = account.ClaudeAuthStatus("unauthenticated")
        with patch.object(account, "claude_auth_status", return_value=native), patch.object(cli.runner, "run", return_value=0) as run:
            self.assertEqual(cli.cmd_login(self.store, SimpleNamespace(ref="cc", dry_run=True, force=False)), 0)
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

    def test_usage_snapshot_separate_and_same_compact_detail_formatters(self):
        self.store.create("cc", engine="claude")
        result = self._snapshot()
        with patch.object(usage, "gather_usage_report", return_value=[result]), contextlib.redirect_stdout(io.StringIO()) as compact:
            self.assertEqual(cli.cmd_usage(self.store, SimpleNamespace(ref=None)), 0)
        with patch.object(usage, "query_profile_usage", return_value=result), contextlib.redirect_stdout(io.StringIO()) as detail:
            self.assertEqual(cli.cmd_usage(self.store, SimpleNamespace(ref="cc")), 0)
        for line in cli._claude_usage_lines(result, 10):
            self.assertIn(line, compact.getvalue())
            self.assertIn(line, detail.getvalue())
        self.assertIn("ANTHROPIC CLAUDE CODE", compact.getvalue())
        self.assertIn("identity unverified", detail.getvalue())
        self.assertNotIn("USE NOW", compact.getvalue())
        self.assertNotIn("authenticated", detail.getvalue())
        self.assertNotIn("CLAUDE + GPT", compact.getvalue())

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

    def test_snapshot_reader_does_not_mutate_profile_or_authenticate(self):
        self.store.create("cc", engine="claude")
        root = self.store.root
        before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        with contextlib.redirect_stdout(io.StringIO()), patch.object(account, "claude_auth_status", side_effect=AssertionError("usage must not probe auth")):
            cli.cmd_usage(self.store, SimpleNamespace(ref="cc"))
        after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
