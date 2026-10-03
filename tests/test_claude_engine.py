"""Claude Code engine: driver, environment, auth status, stable config path, lifecycle."""
import base64
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import doctor
import engines
import isolation
import locks
import models
import platforms
import runner
import store as store_mod
from store import Store, StoreError

from conftest import BaseCase

REAL_CLAUDE = shutil.which("claude")

FAKE_CLAUDE_SOURCE = """#!{python}
import json
import os
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "logged_in")
args = sys.argv[1:]
config = os.environ.get("CLAUDE_CONFIG_DIR", "")
if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if args[:2] == ["daemon", "status"]:
    daemon = os.environ.get("FAKE_CLAUDE_DAEMON", "down")
    if daemon == "running":
        print("running")
        sys.exit(0)
    if daemon == "hang":
        time.sleep(30)
    if daemon == "odd":
        print("something else")
        sys.exit(1)
    if daemon == "crash":
        sys.exit(3)
    print("not running\\n\\nbg sessions:")
    sys.exit(1)
if args[:2] == ["auth", "status"]:
    if mode == "hang":
        time.sleep(30)
    if mode == "malformed":
        print("{{not json")
        sys.exit(0)
    if mode == "crash":
        sys.exit(2)
    report = {{
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "dev@example.com",
        "configDirectory": config,
    }}
    code = 0
    if mode == "logged_out":
        report.update(loggedIn=False, authMethod="none")
        report.pop("email")
        code = 1
    if mode == "inconsistent":
        report["loggedIn"] = False
    if mode == "foreign_dir":
        report["configDirectory"] = "/somewhere/else"
    if mode == "bedrock":
        report["apiProvider"] = "bedrock"
    if mode == "api_key":
        report["authMethod"] = "api_key"
    for method in ("oauth_token", "api_key_helper", "third_party", "none", "weird"):
        if mode == "method_" + method:
            report["authMethod"] = method
    if mode == "no_method":
        report.pop("authMethod")
    if mode == "no_provider":
        report.pop("apiProvider")
    if mode == "unknown_provider":
        report["apiProvider"] = "gateway"
    if mode == "no_config_dir":
        report.pop("configDirectory")
    if mode == "api_key_source":
        report["apiKeySource"] = "ANTHROPIC_API_KEY"
    if mode == "deep":
        sys.stdout.write("[" * 100000)
        sys.exit(0)
    if mode == "stdin_must_be_eof" and sys.stdin.read() != "":
        sys.exit(9)
    print(json.dumps(report))
    sys.exit(code)
dump = os.environ.get("FAKE_CLAUDE_DUMP")
if dump:
    with open(dump, "w") as fh:
        json.dump({{"env": dict(os.environ), "args": args}}, fh)
sys.exit(0)
"""


def write_fake_claude(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CLAUDE_SOURCE.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@unittest.skipIf(sys.platform.startswith("win"), "POSIX fake claude binary")
class ClaudeBase(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.claude_bin = write_fake_claude(self.bin_dir / "claude")
        os.environ["AGYDRA_CLAUDE_BIN"] = str(self.claude_bin)
        for name in list(os.environ):
            if name.startswith(("FAKE_CLAUDE", "ANTHROPIC_", "CLAUDE_CODE_")):
                del os.environ[name]
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.store = Store()

    def make_profile(self, name="work"):
        return self.store.create(name, engine="claude")


class TestDriverAndConfig(ClaudeBase):
    def test_driver_contract(self):
        driver = engines.get_engine("claude")
        self.assertEqual(driver.binary_name, "claude")
        self.assertEqual(driver.env_bin_var, "AGYDRA_CLAUDE_BIN")
        self.assertEqual(driver.config_binary_attr, "claude_binary")
        self.assertEqual(driver.login_args, ("auth", "login"))
        self.assertEqual(driver.env_home_var, "CLAUDE_CONFIG_DIR")
        self.assertFalse(driver.needs_keychain)
        self.assertFalse(driver.uses_overlay)
        self.assertIn("claude", engines.SUPPORTED_ENGINES)

    def test_prepare_args_never_injects_codex_daemon_flag(self):
        self.assertEqual(engines.get_engine("claude").prepare_args(["-p", "x"]), ["-p", "x"])

    def test_other_drivers_unchanged(self):
        for name in ("agy", "codex", "grok"):
            driver = engines.get_engine(name)
            self.assertTrue(driver.uses_overlay)
            self.assertEqual(driver.foreign_auth_env, ())

    def test_config_backwards_compatible(self):
        self.assertNotIn("claude_binary", models.Config().to_dict())
        legacy = models.Config.from_dict({"default_profile": "a", "agy_binary": "/x"})
        self.assertIsNone(legacy.claude_binary)
        cfg = models.Config.from_dict({"claude_binary": "/opt/claude"})
        self.assertEqual(cfg.claude_binary, "/opt/claude")
        self.assertEqual(cfg.to_dict()["claude_binary"], "/opt/claude")

    def test_config_rejects_non_string_binary(self):
        with self.assertRaises(ValueError):
            models.Config.from_dict({"claude_binary": 3})

    def test_binary_resolution_order(self):
        driver = engines.get_engine("claude")
        self.assertEqual(driver.resolve_binary(), self.claude_bin)
        other = write_fake_claude(self.bin_dir / "other-claude")
        self.assertEqual(driver.resolve_binary(str(other)), other)
        del os.environ["AGYDRA_CLAUDE_BIN"]
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)}):
            self.assertEqual(Path(driver.resolve_binary()), self.claude_bin)
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp)}):
            self.assertIsNone(driver.resolve_binary())


class TestEnvironment(ClaudeBase):
    def test_scrubs_foreign_identity_and_pins_config(self):
        cfg = self._tmp / "cfg"
        cfg.mkdir()
        noisy = {
            "ANTHROPIC_API_KEY": "sk",
            "ANTHROPIC_AUTH_TOKEN": "t",
            "CLAUDE_CODE_OAUTH_TOKEN": "o",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "",
            "ANTHROPIC_PROFILE": "p",
        }
        with mock.patch.dict(os.environ, noisy):
            env = isolation.isolated_env(cfg, {"AGYDRA_PROFILE": "work"}, engine="claude")
        for name in noisy:
            self.assertNotIn(name, env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(cfg))
        self.assertNotIn("ANTHROPIC_CONFIG_DIR", env)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AGENT_VIEW"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF"], "1")
        self.assertEqual(env["AGYDRA_PROFILE"], "work")
        self.assertEqual(env["HOME"], os.environ["HOME"])

    def test_extra_cannot_reinject_foreign_auth_and_pins_win(self):
        cfg = self._tmp / "cfg"
        cfg.mkdir()
        env = isolation.isolated_env(
            cfg,
            {
                "ANTHROPIC_API_KEY": "sk",
                "CLAUDE_CODE_OAUTH_TOKEN": "o",
                "ANTHROPIC_CONFIG_DIR": "/elsewhere",
                "CLAUDE_CONFIG_DIR": "/not-mine",
                "CLAUDE_CODE_DISABLE_AGENT_VIEW": "0",
                "AGYDRA_PROFILE": "work",
            },
            engine="claude",
        )
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_CONFIG_DIR", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(cfg))
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AGENT_VIEW"], "1")
        self.assertEqual(env["AGYDRA_PROFILE"], "work")

    def test_inherited_anthropic_config_dir_is_scrubbed(self):
        cfg = self._tmp / "cfg"
        with mock.patch.dict(os.environ, {"ANTHROPIC_CONFIG_DIR": "/home/u/.config/anthropic"}):
            env = isolation.isolated_env(cfg, {}, engine="claude")
        self.assertNotIn("ANTHROPIC_CONFIG_DIR", env)

    def test_other_engines_do_not_get_claude_foreground_flags(self):
        data = self._tmp / "data"
        data.mkdir()
        overlay = isolation.build_overlay("p", data, self.store_root, engine="codex")
        env = isolation.isolated_env(overlay, {}, engine="codex")
        self.assertNotIn("CLAUDE_CODE_DISABLE_AGENT_VIEW", env)

    def test_does_not_redirect_home_even_when_windows_flag_set(self):
        cfg = self._tmp / "cfg"
        env = isolation.isolated_env(
            cfg, {}, engine="claude", config_windows_redirect_home=True
        )
        self.assertEqual(env["HOME"], os.environ["HOME"])
        self.assertEqual(env.get("XDG_CONFIG_HOME"), os.environ.get("XDG_CONFIG_HOME"))

    def test_other_engines_keep_inherited_variables(self):
        data = self._tmp / "data"
        data.mkdir()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk"}):
            overlay = isolation.build_overlay("p", data, self.store_root, engine="codex")
            env = isolation.isolated_env(overlay, {}, engine="codex")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk")
        self.assertEqual(env["HOME"], str(overlay))

    def test_driver_reports_inherited_names(self):
        driver = engines.get_engine("claude")
        found = driver.inherited_foreign_auth({"ANTHROPIC_API_KEY": "x", "PATH": "/bin"})
        self.assertEqual(found, ["ANTHROPIC_API_KEY"])


class TestOverlayFreeIsolation(ClaudeBase):
    def test_build_overlay_creates_no_overlay_and_no_real_home_state(self):
        real_claude = self.fake_home / ".claude"
        real_claude.mkdir()
        (real_claude / "settings.json").write_text("{}", encoding="utf-8")
        (self.fake_home / ".claude.json").write_text("{}", encoding="utf-8")
        before = sorted(p.name for p in self.fake_home.iterdir())
        profile = self.make_profile()
        data = self.store.profile_data_dir(profile.name, engine="claude")
        result = isolation.build_overlay(profile.name, data, self.store.root, engine="claude")
        self.assertEqual(result, data)
        self.assertFalse((self.store.overlays_dir / profile.name).exists())
        self.assertEqual(sorted(p.name for p in self.fake_home.iterdir()), before)
        self.assertEqual(list(real_claude.iterdir()), [real_claude / "settings.json"])
        self.assertFalse(any(p.is_symlink() for p in data.rglob("*")))

    def test_refuses_symlinked_config_dir(self):
        target = self._tmp / "elsewhere"
        target.mkdir()
        link = self._tmp / "link"
        link.symlink_to(target)
        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("p", link, self.store_root, engine="claude")

    def test_refuses_real_claude_dir(self):
        real_claude = self.fake_home / ".claude"
        real_claude.mkdir()
        with self.assertRaises(isolation.IsolationError):
            isolation.prepare_claude_config_dir(real_claude)

    def test_config_dir_is_private(self):
        profile = self.make_profile()
        data = self.store.profile_data_dir(profile.name)
        self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)

    def test_chmod_failure_allows_an_already_private_directory(self):
        profile = self.make_profile()
        data = self.store.profile_data_dir(profile.name)
        with mock.patch.object(
            isolation.os, "chmod", side_effect=PermissionError("secret chmod detail")
        ):
            prepared = isolation.prepare_claude_config_dir(data)

        self.assertEqual(prepared, data)
        self.assertEqual(stat.S_IMODE(data.stat().st_mode) & 0o077, 0)

    def test_chmod_failure_rejects_group_or_other_permissions(self):
        profile = self.make_profile()
        data = self.store.profile_data_dir(profile.name)
        os.chmod(data, 0o750)
        with mock.patch.object(
            isolation.os, "chmod", side_effect=PermissionError("secret chmod detail")
        ):
            with self.assertRaises(isolation.IsolationError) as raised:
                isolation.prepare_claude_config_dir(data)

        self.assertIn(str(data), str(raised.exception))
        self.assertNotIn("secret chmod detail", str(raised.exception))

    def test_chmod_failure_rejects_unverifiable_directory_permissions(self):
        profile = self.make_profile()
        data = self.store.profile_data_dir(profile.name)
        chmod_attempted = False
        original_lstat = Path.lstat

        def fail_chmod(_path, _mode):
            nonlocal chmod_attempted
            chmod_attempted = True
            raise PermissionError("secret chmod detail")

        def fail_verification(path, *args, **kwargs):
            if chmod_attempted and Path(path) == data:
                raise OSError("secret stat detail")
            return original_lstat(path, *args, **kwargs)

        with mock.patch.object(isolation.os, "chmod", side_effect=fail_chmod), mock.patch.object(
            Path, "lstat", autospec=True, side_effect=fail_verification
        ):
            with self.assertRaises(isolation.IsolationError) as raised:
                isolation.prepare_claude_config_dir(data)

        self.assertIn(str(data), str(raised.exception))
        self.assertNotIn("secret chmod detail", str(raised.exception))
        self.assertNotIn("secret stat detail", str(raised.exception))


class TestInspectClaudeAuth(ClaudeBase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self._tmp / "cfg"
        self.cfg.mkdir()

    def inspect(self, mode, timeout=10.0, binary="default", cfg=None):
        env = isolation.isolated_env(cfg or self.cfg, {"FAKE_CLAUDE_MODE": mode}, engine="claude")
        chosen = self.claude_bin if binary == "default" else binary
        return account.inspect_claude_auth(chosen, cfg or self.cfg, env, timeout=timeout)

    def test_authenticated(self):
        status = self.inspect("logged_in")
        self.assertEqual(status.state, "authenticated")
        self.assertEqual(status.email, "dev@example.com")
        self.assertEqual(status.auth_state_label, "authenticated")

    def test_unauthenticated(self):
        status = self.inspect("logged_out")
        self.assertEqual(status.state, "unauthenticated")
        self.assertEqual(status.auth_state_label, "not-authenticated")
        self.assertIsNone(status.email)

    def test_malformed_json_is_unknown(self):
        status = self.inspect("malformed")
        self.assertEqual(status.state, "unknown")
        self.assertIn("malformed", status.reason)

    def test_timeout_is_unknown_and_bounded(self):
        status = self.inspect("hang", timeout=0.5)
        self.assertEqual(status.state, "unknown")
        self.assertIn("timed out", status.reason)

    def test_missing_binary_is_unknown(self):
        status = self.inspect("logged_in", binary=None)
        self.assertEqual(status.state, "unknown")
        self.assertIn("not found", status.reason)

    def test_unexecutable_binary_is_unknown(self):
        status = self.inspect("logged_in", binary=self._tmp / "nope")
        self.assertEqual(status.state, "unknown")

    def test_unexpected_exit_is_unknown(self):
        self.assertEqual(self.inspect("crash").state, "unknown")

    def test_inconsistent_answer_is_unknown(self):
        self.assertEqual(self.inspect("inconsistent").state, "unknown")

    def test_foreign_config_directory_is_unknown(self):
        status = self.inspect("foreign_dir")
        self.assertEqual(status.state, "unknown")
        self.assertIn("different config directory", status.reason)

    def test_cloud_provider_or_api_key_is_not_guaranteed(self):
        self.assertEqual(self.inspect("bedrock").state, "unknown")
        self.assertEqual(self.inspect("api_key").state, "unknown")

    def test_subscription_whitelist_rejects_every_other_method(self):
        for mode in (
            "method_oauth_token", "method_api_key_helper", "method_third_party",
            "method_none", "method_weird", "api_key", "no_method",
        ):
            with self.subTest(mode=mode):
                status = self.inspect(mode)
                self.assertEqual(status.state, "unknown")
                self.assertIn("not the native claude.ai", status.reason)

    def test_missing_or_unknown_provider_is_unknown(self):
        for mode in ("no_provider", "unknown_provider", "bedrock"):
            with self.subTest(mode=mode):
                self.assertEqual(self.inspect(mode).state, "unknown")

    def test_missing_config_directory_report_cannot_authenticate(self):
        status = self.inspect("no_config_dir")
        self.assertEqual(status.state, "unknown")
        self.assertIn("config directory", status.reason)

    def test_active_api_key_source_is_not_guaranteed(self):
        self.assertEqual(self.inspect("api_key_source").state, "unknown")

    def test_recursion_bomb_is_unknown(self):
        status = self.inspect("deep")
        self.assertEqual(status.state, "unknown")
        self.assertIn("malformed", status.reason)

    def test_value_error_and_oserror_from_run_are_unknown(self):
        for exc in (ValueError("embedded null byte"), OSError("nope"), PermissionError("x")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(platforms, "run_with_group_kill", side_effect=exc):
                    status = account.inspect_claude_auth(self.claude_bin, self.cfg, {})
                self.assertEqual(status.state, "unknown")

    def test_stdin_is_devnull_so_probe_never_waits_for_input(self):
        status = self.inspect("stdin_must_be_eof", timeout=5)
        self.assertEqual(status.state, "authenticated")

    def test_symlinked_state_file_is_unknown_without_spawning(self):
        profile = self.make_profile("work")
        cfg = self.store.claude_config_dir("work")
        target = self._tmp / "host.json"
        target.write_text("{}", encoding="utf-8")
        (cfg / ".claude.json").symlink_to(target)
        with mock.patch.object(platforms, "run_with_group_kill") as spawn:
            self.assertEqual(
                account.auth_state(cfg, self.store, "work", engine="claude"), "unknown"
            )
        spawn.assert_not_called()

    def test_missing_config_dir_does_not_spawn(self):
        missing = self._tmp / "missing"
        with mock.patch.object(platforms, "run_with_group_kill") as spawn:
            status = account.inspect_claude_auth(self.claude_bin, missing, {})
        spawn.assert_not_called()
        self.assertEqual(status.state, "unknown")

    def test_only_auth_status_is_invoked(self):
        with mock.patch.object(platforms, "run_with_group_kill") as spawn:
            spawn.return_value = mock.Mock(returncode=1, stdout='{"loggedIn": false}', stderr="")
            account.inspect_claude_auth(self.claude_bin, self.cfg, {"A": "b"}, timeout=3)
        argv = spawn.call_args[0][0]
        self.assertEqual(argv[1:], ["auth", "status"])
        self.assertEqual(spawn.call_args[1]["timeout"], 3)

    def test_auth_state_and_email_use_driver_and_scrubbed_env(self):
        profile = self.make_profile()
        data = self.store.profile_data_dir(profile.name)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk"}):
            self.assertEqual(account.auth_state(data, self.store, profile.name, engine="claude"), "authenticated")
            self.assertEqual(account.detect_email(data, self.store, profile.name, engine="claude"), "dev@example.com")

    def test_auth_state_unknown_without_binary_never_authenticated(self):
        profile = self.make_profile()
        data = self.store.profile_data_dir(profile.name)
        del os.environ["AGYDRA_CLAUDE_BIN"]
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp)}):
            self.assertEqual(account.auth_state(data, self.store, profile.name, engine="claude"), "unknown")
            self.assertIsNone(account.detect_email(data, self.store, profile.name, engine="claude"))

    def test_cached_profile_email_does_not_imply_authentication(self):
        profile = self.make_profile()
        profile.email = "cached@example.com"
        self.store.save(profile)
        data = self.store.profile_data_dir(profile.name)
        os.environ["FAKE_CLAUDE_MODE"] = "logged_out"
        self.assertEqual(account.auth_state(data, self.store, profile.name, engine="claude"), "not-authenticated")


class TestStablePhysicalPath(ClaudeBase):
    def test_path_is_keyed_by_seq(self):
        a = self.make_profile("alpha")
        b = self.make_profile("beta")
        expected = self.store.root / "claude-config" / str(a.seq)
        self.assertEqual(self.store.claude_config_dir("alpha"), expected)
        self.assertEqual(self.store.profile_data_dir("alpha"), expected)
        self.assertEqual(self.store.profile_data_dir("alpha", engine="claude"), expected)
        self.assertNotEqual(self.store.claude_config_dir("beta"), expected)
        self.assertEqual(self.store.claude_config_dir_for_seq(b.seq).name, str(b.seq))
        self.assertTrue(expected.is_dir())
        self.assertFalse((self.store.profile_dir("alpha") / "data").exists())

    def test_rename_keeps_physical_path_and_data(self):
        profile = self.make_profile("alpha")
        before = self.store.claude_config_dir("alpha")
        (before / ".claude.json").write_text('{"k": 1}', encoding="utf-8")
        self.store.rename("alpha", "gamma")
        self.assertEqual(self.store.claude_config_dir("gamma"), before)
        self.assertEqual((before / ".claude.json").read_text(encoding="utf-8"), '{"k": 1}')
        self.assertEqual(self.store.get("gamma").seq, profile.seq)
        self.assertEqual(self.store.claude_config_orphans(self.store.list()), [])

    def test_seq_never_reused_after_delete(self):
        first = self.make_profile("alpha")
        self.store.delete("alpha", backup=False)
        second = self.make_profile("beta")
        self.assertGreater(second.seq, first.seq)

    def test_agy_profile_layout_unchanged(self):
        self.store.create("legacy")
        self.assertEqual(
            self.store.profile_data_dir("legacy"), self.store.profile_dir("legacy") / "data"
        )
        self.assertFalse((self.store.root / "claude-config").exists())

    def test_claude_config_dir_rejects_other_engines(self):
        self.store.create("legacy")
        with self.assertRaises(StoreError):
            self.store.claude_config_dir("legacy")

    def test_invalid_seq_rejected(self):
        for bad in (0, -1, "1", True):
            with self.assertRaises(StoreError):
                self.store.claude_config_dir_for_seq(bad)
            with self.assertRaises(StoreError):
                self.store.usage_cache_dir(bad)


class TestCreateLifecycle(ClaudeBase):
    def test_failed_publish_rolls_back_config(self):
        real_rename = store_mod.rename_dir_with_retry
        calls = []

        def flaky(src, dst, attempts=3):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("boom")
            real_rename(src, dst, attempts)

        with mock.patch.object(store_mod, "rename_dir_with_retry", side_effect=flaky):
            with self.assertRaises(OSError):
                self.store.create("work", engine="claude")
        root = self.store.root / "claude-config"
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(self.store.list(), [])

    def test_refuses_to_adopt_existing_target(self):
        occupied = self.store.root / "claude-config" / "1"
        occupied.mkdir(parents=True)
        (occupied / "keep").write_text("x", encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.create("work", engine="claude")
        self.assertTrue((occupied / "keep").exists())

    def test_stale_stage_is_cleaned_on_next_create(self):
        stale = self.store.root / "claude-config" / ".agydra-stage-old"
        stale.mkdir(parents=True)
        self.make_profile()
        self.assertFalse(stale.exists())

    def test_orphans_are_reported_not_deleted(self):
        self.make_profile("alpha")
        orphan = self.store.root / "claude-config" / "999"
        orphan.mkdir()
        self.assertEqual(self.store.claude_config_orphans(self.store.list()), ["999"])
        self.store.delete("alpha", backup=False)
        self.assertTrue(orphan.exists())


class TestDeleteAndBackup(ClaudeBase):
    def test_backup_includes_physical_config_then_removes_only_own_data(self):
        a = self.make_profile("alpha")
        b = self.make_profile("beta")
        cfg_a = self.store.claude_config_dir("alpha")
        cfg_b = self.store.claude_config_dir("beta")
        (cfg_a / ".claude.json").write_text('{"a": 1}', encoding="utf-8")
        (cfg_b / ".claude.json").write_text('{"b": 1}', encoding="utf-8")
        cache = self.store.usage_cache_dir(a.seq)
        cache.mkdir(parents=True)
        (cache / "s.json").write_text("{}", encoding="utf-8")
        other_cache = self.store.usage_cache_dir(b.seq)
        other_cache.mkdir(parents=True)
        backup = self.store.delete("alpha")
        self.assertIsNotNone(backup)
        with zipfile.ZipFile(backup) as zf:
            self.assertIn("_claude-config/.claude.json", zf.namelist())
            self.assertEqual(json.loads(zf.read("_claude-config/.claude.json")), {"a": 1})
        self.assertFalse(cfg_a.exists())
        self.assertFalse(cache.exists())
        self.assertTrue((cfg_b / ".claude.json").exists())
        self.assertTrue(other_cache.exists())
        self.assertFalse(self.store.profile_dir("alpha").exists())

    def test_empty_config_adds_no_config_entries_to_backup(self):
        self.make_profile("alpha")
        backup = self.store.delete("alpha")
        with zipfile.ZipFile(backup) as zf:
            self.assertEqual(zf.namelist(), ["profile.json"])
        self.assertFalse((self.store.root / "claude-config" / "1").exists())

    def test_symlinked_config_is_unlinked_not_followed(self):
        self.make_profile("alpha")
        cfg = self.store.claude_config_dir("alpha")
        cfg.rmdir()
        victim = self._tmp / "victim"
        victim.mkdir()
        (victim / "precious").write_text("x", encoding="utf-8")
        cfg.symlink_to(victim)
        self.store.delete("alpha", backup=False)
        self.assertTrue((victim / "precious").exists())
        self.assertFalse(os.path.lexists(cfg))

    def seed_background(self, name="alpha", roster=None):
        cfg = self.store.claude_config_dir(name)
        (cfg / "daemon").mkdir()
        (cfg / "daemon.log").write_text("log", encoding="utf-8")
        (cfg / ".claude.json").write_text('{"keep": 1}', encoding="utf-8")
        if roster is not None:
            (cfg / "daemon" / "roster.json").write_text(json.dumps(roster), encoding="utf-8")
        return cfg

    def assert_delete_refused_untouched(self, name="alpha"):
        before = sorted(self.store.backups_dir.glob("*")) if self.store.backups_dir.exists() else []
        with self.assertRaises(StoreError) as ctx:
            self.store.delete(name)
        after = sorted(self.store.backups_dir.glob("*")) if self.store.backups_dir.exists() else []
        self.assertEqual(before, after)
        self.assertTrue(self.store.profile_dir(name).exists())
        self.assertEqual(
            (self.store.claude_config_dir(name) / ".claude.json").read_text(encoding="utf-8"),
            '{"keep": 1}',
        )
        return str(ctx.exception)

    def test_no_background_evidence_means_no_fork(self):
        self.make_profile("alpha")
        with mock.patch.object(platforms, "run_with_group_kill") as spawn:
            self.store.delete("alpha", backup=False)
        spawn.assert_not_called()

    def test_running_supervisor_blocks_delete_before_backup(self):
        self.make_profile("alpha")
        self.seed_background()
        os.environ["FAKE_CLAUDE_DAEMON"] = "running"
        self.assertIn("daemon stop", self.assert_delete_refused_untouched())

    def test_unknown_supervisor_state_fails_closed(self):
        for state in ("hang", "odd", "crash"):
            with self.subTest(state=state):
                self.store.create(f"p-{state}", engine="claude")
                cfg = self.store.claude_config_dir(f"p-{state}")
                (cfg / "daemon").mkdir()
                (cfg / ".claude.json").write_text('{"keep": 1}', encoding="utf-8")
                os.environ["FAKE_CLAUDE_DAEMON"] = state
                with mock.patch.object(
                    store_mod.Store, "CLAUDE_SUPERVISOR_PROBE_TIMEOUT", 0.5
                ):
                    self.assert_delete_refused_untouched(f"p-{state}")

    def test_missing_binary_with_background_evidence_fails_closed(self):
        self.make_profile("alpha")
        self.seed_background()
        del os.environ["AGYDRA_CLAUDE_BIN"]
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp)}):
            message = self.assert_delete_refused_untouched()
        self.assertIn("AGYDRA_CLAUDE_BIN", message)

    def test_down_supervisor_with_listed_workers_blocks(self):
        self.make_profile("alpha")
        self.seed_background(roster={"version": 1, "workers": {"abc": {"pid": 1}}})
        self.assertIn("workers", self.assert_delete_refused_untouched())

    def test_unreadable_roster_blocks(self):
        self.make_profile("alpha")
        cfg = self.seed_background()
        (cfg / "daemon" / "roster.json").write_text("{broken", encoding="utf-8")
        self.assertIn("roster", self.assert_delete_refused_untouched())

    def test_down_supervisor_with_empty_roster_allows_delete(self):
        self.make_profile("alpha")
        self.seed_background(roster={"version": 1, "workers": {}})
        self.assertIsNotNone(self.store.delete("alpha"))
        self.assertFalse(self.store.profile_dir("alpha").exists())

    def test_running_supervisor_blocks_rename_but_not_recovery_of_data_path(self):
        self.make_profile("alpha")
        self.seed_background()
        os.environ["FAKE_CLAUDE_DAEMON"] = "running"
        with self.assertRaises(StoreError):
            self.store.rename("alpha", "beta")
        self.assertTrue(self.store.profile_dir("alpha").exists())
        self.assertFalse(self.store.profile_dir("beta").exists())
        self.assertFalse(self.store.rename_journal_path.exists())
        os.environ["FAKE_CLAUDE_DAEMON"] = "down"
        self.store.rename("alpha", "beta")
        self.assertEqual(
            (self.store.claude_config_dir("beta") / ".claude.json").read_text(encoding="utf-8"),
            '{"keep": 1}',
        )

    def test_damaged_claude_metadata_fails_closed(self):
        profile = self.make_profile("alpha")
        meta = self.store.profile_meta_path("alpha")
        raw = json.loads(meta.read_text(encoding="utf-8"))
        raw["seq"] = 0
        meta.write_text(json.dumps(raw), encoding="utf-8")
        cfg = self.store.claude_config_dir_for_seq(profile.seq)
        (cfg / ".credentials.json").write_text("secret", encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            self.store.delete("alpha")
        self.assertIn("damaged", str(ctx.exception))
        self.assertTrue((cfg / ".credentials.json").exists())
        self.assertTrue(self.store.profile_dir("alpha").exists())

    def test_unparseable_metadata_keeps_external_config_and_reports_it(self):
        profile = self.make_profile("alpha")
        cfg = self.store.claude_config_dir_for_seq(profile.seq)
        (cfg / ".credentials.json").write_text("secret", encoding="utf-8")
        self.store.profile_meta_path("alpha").write_text("{not json", encoding="utf-8")
        with mock.patch.object(store_mod, "warn") as warned:
            self.store.delete("alpha", backup=False)
        self.assertTrue(warned.called)
        self.assertTrue((cfg / ".credentials.json").exists())
        self.assertFalse(self.store.profile_dir("alpha").exists())
        self.assertEqual(self.store.claude_config_orphans(self.store.list()), [str(profile.seq)])

    def test_backup_does_not_include_reused_agy_keychain_slot(self):
        self.make_profile("alpha")
        import keychain

        slot = keychain.slot_backup_path(self.store, "alpha")
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_text("agy-secret", encoding="utf-8")
        cfg = self.store.claude_config_dir("alpha")
        (cfg / ".claude.json").write_text("{}", encoding="utf-8")
        backup = self.store.delete("alpha")
        with zipfile.ZipFile(backup) as zf:
            self.assertFalse(any(n.startswith("_keychain/") for n in zf.namelist()))
            self.assertIn("_claude-config/.claude.json", zf.namelist())

    def test_keychain_only_content_does_not_trigger_claude_backup(self):
        self.make_profile("alpha")
        self.assertFalse(
            store_mod._has_backup_worthy_content(
                self._tmp / "nothing", None, None, (self.store.claude_config_dir("alpha"),)
            )
        )

    def test_backup_stores_inner_symlinks_without_following_them(self):
        self.make_profile("alpha")
        cfg = self.store.claude_config_dir("alpha")
        outside = self._tmp / "outside"
        outside.mkdir()
        (outside / "host-secret").write_text("DO-NOT-ARCHIVE", encoding="utf-8")
        (cfg / "linkdir").symlink_to(outside)
        (cfg / "linkfile").symlink_to(outside / "host-secret")
        (cfg / ".claude.json").write_text("{}", encoding="utf-8")
        backup = self.store.delete("alpha")
        with zipfile.ZipFile(backup) as zf:
            names = zf.namelist()
            self.assertIn("_claude-config/linkdir", names)
            self.assertIn("_claude-config/linkfile", names)
            self.assertFalse(any("host-secret" in n for n in names))
            blob = b"".join(zf.read(n) for n in names)
            self.assertNotIn(b"DO-NOT-ARCHIVE", blob)
            info = zf.getinfo("_claude-config/linkdir")
            self.assertEqual(stat.S_IFMT(info.external_attr >> 16), stat.S_IFLNK)
            self.assertEqual(zf.read("_claude-config/linkdir").decode(), str(outside))
        self.assertEqual((outside / "host-secret").read_text(encoding="utf-8"), "DO-NOT-ARCHIVE")
        self.assertFalse(self.store.claude_config_dir_for_seq(1).exists())

    def test_symlinked_config_root_is_archived_as_link_and_target_survives(self):
        self.make_profile("alpha")
        cfg = self.store.claude_config_dir("alpha")
        cfg.rmdir()
        victim = self._tmp / "victim"
        victim.mkdir()
        (victim / "precious").write_text("x", encoding="utf-8")
        cfg.symlink_to(victim)
        backup = self.store.delete("alpha")
        with zipfile.ZipFile(backup) as zf:
            self.assertIn("_claude-config", zf.namelist())
            self.assertNotIn("_claude-config/precious", zf.namelist())
        self.assertTrue((victim / "precious").exists())
        self.assertFalse(os.path.lexists(cfg))

    def test_symlinked_claude_config_root_refuses_create_and_delete(self):
        self.make_profile("alpha")
        root = self.store.root / "claude-config"
        moved = self._tmp / "moved-root"
        root.rename(moved)
        root.symlink_to(moved)
        with self.assertRaises(StoreError):
            self.store.create("beta", engine="claude")
        with self.assertRaises(StoreError):
            self.store.delete("alpha", backup=False)
        self.assertTrue((moved / "1").exists())

    def test_delete_tombstones_usage_after_backup_before_removal(self):
        import claude_usage

        self.make_profile("alpha")
        cfg = self.store.claude_config_dir("alpha")
        (cfg / ".claude.json").write_text("{}", encoding="utf-8")
        seen = {}

        def invalidate(store, name, deleted=False):
            seen["deleted"] = deleted
            seen["backups"] = len(list(store.backups_dir.glob("*.zip")))
            seen["config_still_there"] = cfg.exists()
            return 3

        with mock.patch.object(claude_usage, "invalidate_profile_usage", side_effect=invalidate):
            self.store.delete("alpha")
        self.assertEqual(seen, {"deleted": True, "backups": 1, "config_still_there": True})

    def test_usage_invalidation_failure_preserves_everything(self):
        import claude_usage

        self.make_profile("alpha")
        cfg = self.store.claude_config_dir("alpha")
        (cfg / ".claude.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(
            claude_usage, "invalidate_profile_usage", side_effect=RuntimeError("locked")
        ):
            with self.assertRaises(StoreError) as ctx:
                self.store.delete("alpha")
        self.assertIn("nothing was deleted", str(ctx.exception))
        self.assertTrue((cfg / ".claude.json").exists())
        self.assertTrue(self.store.profile_meta_path("alpha").exists())

    def test_real_usage_tombstone_survives_delete(self):
        import claude_usage

        profile = self.make_profile("alpha")
        self.store.delete("alpha", backup=False)
        generation, deleted = claude_usage._read_generation(self.store, profile.seq)
        self.assertTrue(deleted)
        self.assertEqual(claude_usage.capture_environment(self.store, profile), {})

    def test_delete_verifies_physical_removal_before_metadata(self):
        self.make_profile("alpha")
        cfg = self.store.claude_config_dir("alpha")
        (cfg / ".claude.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(store_mod, "rmtree", lambda path: None):
            with self.assertRaises(StoreError):
                self.store.delete("alpha", backup=False)
        self.assertTrue(self.store.profile_dir("alpha").exists())
        self.assertTrue(self.store.profile_meta_path("alpha").exists())

    def test_delete_blocked_by_live_session_lock(self):
        self.make_profile("alpha")
        handle = locks.try_lock(self.store, "alpha")
        try:
            with self.assertRaises(StoreError):
                self.store.delete("alpha", backup=False)
            with self.assertRaises(StoreError):
                self.store.rename("alpha", "beta")
        finally:
            handle.release()
        self.assertTrue(self.store.claude_config_dir("alpha").exists())


class TestRunner(ClaudeBase):
    def plan_for(self, name):
        return runner.build_plan(self.store, ["--flag"], flag_ref=name, launch_as_child=True)

    def test_plan_points_at_physical_config_without_overlay(self):
        self.make_profile("work")
        plan = self.plan_for("work")
        self.assertEqual(plan.engine, "claude")
        self.assertEqual(plan.binary, self.claude_bin)
        self.assertEqual(plan.env_home_var, "CLAUDE_CONFIG_DIR")
        self.assertEqual(plan.env_home_value, self.store.claude_config_dir("work"))
        self.assertEqual(plan.args, ["--flag"])
        self.assertIn("CLAUDE_CONFIG_DIR", plan.describe())
        self.assertEqual(plan.env["CLAUDE_CONFIG_DIR"], str(self.store.claude_config_dir("work")))
        self.assertEqual(plan.env["HOME"], str(self.fake_home))
        self.assertEqual(plan.env["AGYDRA_PROFILE"], "work")
        self.assertFalse((self.store.overlays_dir / "work").exists())

    def test_login_plan_uses_native_login_args(self):
        self.make_profile("work")
        driver = engines.get_engine("claude")
        plan = runner.build_plan(self.store, list(driver.login_args), flag_ref="work", launch_as_child=True)
        self.assertEqual(plan.args, ["auth", "login"])

    def test_missing_binary_is_explicit(self):
        self.make_profile("work")
        del os.environ["AGYDRA_CLAUDE_BIN"]
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp)}):
            with self.assertRaises(StoreError) as ctx:
                self.plan_for("work")
        self.assertIn("AGYDRA_CLAUDE_BIN", str(ctx.exception))

    def test_run_launches_with_pinned_config_and_scrubbed_identity(self):
        self.make_profile("work")
        dump = self._tmp / "dump.json"
        os.environ["FAKE_CLAUDE_DUMP"] = str(dump)
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "oauth-test"
        plan = self.plan_for("work")
        with mock.patch.object(runner, "warn") as warned:
            rc = runner.run(plan, store=self.store)
        self.assertEqual(rc, 0)
        seen = json.loads(dump.read_text(encoding="utf-8"))
        env = seen["env"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.store.claude_config_dir("work")))
        self.assertEqual(env["HOME"], str(self.fake_home))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["AGYDRA_PROFILE"], "work")
        self.assertEqual(seen["args"], ["--flag"])
        message = warned.call_args[0][0]
        self.assertIn("ANTHROPIC_API_KEY", message)
        self.assertNotIn("sk-test", message)
        self.assertFalse((self.store.overlays_dir / "work").exists())
        self.assertFalse((self.fake_home / ".claude").exists())
        self.assertFalse((self.fake_home / ".claude.json").exists())

    def test_unprivateable_config_aborts_before_claude_launch(self):
        profile = self.make_profile("work")
        config_dir = self.store.claude_config_dir(profile.name)
        os.chmod(config_dir, 0o750)
        plan = self.plan_for("work")

        with mock.patch.object(
            isolation.os, "chmod", side_effect=PermissionError("secret chmod detail")
        ), mock.patch.object(runner.platforms, "launch_argv") as launch, mock.patch.object(
            runner.platforms, "run_wait"
        ) as waited_launch:
            with self.assertRaises(isolation.IsolationError) as raised:
                runner.run(plan, store=self.store)

        self.assertIn(str(config_dir), str(raised.exception))
        self.assertNotIn("secret chmod detail", str(raised.exception))
        launch.assert_not_called()
        waited_launch.assert_not_called()

    def test_busy_profile_joins_instead_of_refusing(self):
        """Concurrent sessions of the same profile JOIN (no refusal). A live
        registry holder from another session does not block a launch — the
        new session joins, runs, and releases its own entry."""
        self.make_profile("work")
        locks.acquire_lease(self.store, "work")
        try:
            rc = runner.run(self.plan_for("work"), store=self.store)
            self.assertEqual(rc, 0)
        finally:
            locks.release_lease(self.store, "work")

    def test_dry_run_touches_nothing(self):
        self.make_profile("work")
        plan = self.plan_for("work")
        self.assertEqual(runner.run(plan, store=self.store, dry_run=True), 0)


class TestDoctor(ClaudeBase):
    def checks(self):
        ctx = doctor._build_ctx(self.store)
        return {label: check(self.store, ctx) for label, check in doctor.CHECKS}

    def test_binary_check_covers_claude(self):
        self.make_profile("work")
        results = self.checks()
        status, message = next(v for k, v in results.items() if "binary" in k.lower())
        self.assertEqual(status, doctor.OK)
        self.assertIn("claude binary", message)

    def test_missing_binary_fails_explicitly(self):
        self.make_profile("work")
        del os.environ["AGYDRA_CLAUDE_BIN"]
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp)}):
            status, message = doctor._check_binary(self.store, doctor._build_ctx(self.store))
        self.assertEqual(status, doctor.FAIL)
        self.assertIn("AGYDRA_CLAUDE_BIN", message)

    def test_profile_check_reports_unknown_instead_of_agy(self):
        self.make_profile("work")
        os.environ["FAKE_CLAUDE_MODE"] = "malformed"
        status, message = doctor._check_profiles(self.store, doctor._build_ctx(self.store))
        self.assertIn("work: unknown", message)

    def test_isolation_check_accepts_overlay_free_profile(self):
        self.make_profile("work")
        status, message = doctor._check_isolation(self.store, doctor._build_ctx(self.store))
        self.assertEqual(status, doctor.OK)
        self.assertIn("claude", message)

    def test_isolation_check_flags_linked_config(self):
        self.make_profile("work")
        cfg = self.store.claude_config_dir("work")
        cfg.rmdir()
        target = self._tmp / "t"
        target.mkdir()
        cfg.symlink_to(target)
        status, message = doctor._check_isolation(self.store, doctor._build_ctx(self.store))
        self.assertEqual(status, doctor.FAIL)

    def test_isolation_check_warns_on_orphans(self):
        self.make_profile("work")
        (self.store.root / "claude-config" / "42").mkdir()
        status, message = doctor._check_isolation(self.store, doctor._build_ctx(self.store))
        self.assertEqual(status, doctor.WARN)
        self.assertIn("claude-config/42", message)

    def test_fix_pass_skips_overlay_recovery_for_claude(self):
        self.make_profile("work")
        ctx = doctor._build_ctx(self.store)
        doctor._apply_fixes(self.store, ctx)
        self.assertFalse((self.store.overlays_dir / "work").exists())
        self.assertTrue(self.store.claude_config_dir("work").is_dir())


class TestReadonlyStoreApi(ClaudeBase):
    def test_get_scan_resolve_match_locked_variants(self):
        a = self.make_profile("alpha")
        self.make_profile("beta")
        self.assertEqual(self.store.get_readonly("alpha"), self.store.get("alpha"))
        self.assertEqual(self.store.scan_readonly(), self.store.scan())
        self.assertEqual(self.store.list_readonly(), self.store.list())
        self.assertEqual(self.store.resolve_ref_readonly("alpha"), "alpha")
        self.assertEqual(self.store.resolve_ref_readonly("2"), "beta")
        self.assertEqual(self.store.resolve_ref_readonly("#1"), self.store.resolve_ref("#1"))
        self.assertEqual(self.store.get_readonly("alpha").seq, a.seq)

    def test_errors_match_locked_resolution(self):
        self.make_profile("alpha")
        for ref in ("ghost", "9"):
            with self.assertRaises(StoreError) as ro:
                self.store.resolve_ref_readonly(ref)
            with self.assertRaises(StoreError) as rw:
                self.store.resolve_ref(ref)
            self.assertEqual(str(ro.exception), str(rw.exception))
        with self.assertRaises(StoreError):
            self.store.get_readonly("Bad Name")
        with self.assertRaises(StoreError):
            self.store.get_readonly("ghost")

    def test_never_takes_locks_or_writes(self):
        self.make_profile("alpha")
        before = sorted(str(p) for p in self.store.root.rglob("*"))
        with mock.patch.object(locks, "try_lock") as lock, mock.patch.object(
            locks, "try_sequence_lock"
        ) as seq_lock:
            self.store.get_readonly("alpha")
            self.store.scan_readonly()
            self.store.resolve_ref_readonly("alpha")
        lock.assert_not_called()
        seq_lock.assert_not_called()
        self.assertEqual(sorted(str(p) for p in self.store.root.rglob("*")), before)

    def test_pending_rename_fails_closed_without_recovering(self):
        self.make_profile("alpha")
        self.store._write_rename_journal("alpha", "beta", False)
        journal = self.store.rename_journal_path.read_text(encoding="utf-8")
        for call in (
            lambda: self.store.get_readonly("alpha"),
            lambda: self.store.scan_readonly(),
            lambda: self.store.resolve_ref_readonly("alpha"),
        ):
            with self.assertRaises(StoreError) as ctx:
                call()
            self.assertIn("rename recovery is pending", str(ctx.exception))
        self.assertEqual(self.store.rename_journal_path.read_text(encoding="utf-8"), journal)
        self.assertTrue(self.store.profile_dir("alpha").exists())

    def test_journal_appearing_during_read_discards_the_answer(self):
        self.make_profile("alpha")
        real = self.store._get_unlocked

        def read_then_journal(name):
            result = real(name)
            self.store._write_rename_journal("alpha", "beta", False)
            return result

        with mock.patch.object(self.store, "_get_unlocked", side_effect=read_then_journal):
            with self.assertRaises(StoreError):
                self.store.get_readonly("alpha")

    def test_unreadable_journal_inspection_fails_closed(self):
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(StoreError):
                self.store.get_readonly("alpha")

    def test_corrupt_metadata_is_a_store_error(self):
        self.make_profile("alpha")
        self.store.profile_meta_path("alpha").write_text("{nope", encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.get_readonly("alpha")


class TestShellCommand(unittest.TestCase):
    NASTY = [
        "/tmp/dir with spaces/py thon",
        "-m",
        "claude_usage",
        "--store",
        "/tmp/it's a \"store\"/$HOME/`id`;&|<>(){}*?~#!%PATH%",
        "caf\u00e9 \u2018quoted\u2019",
    ]

    def test_posix_round_trips_through_a_real_shell(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX shell")
        line = platforms.shell_command(self.NASTY)
        self.assertEqual(shlex.split(line), self.NASTY)
        probe = subprocess.run(
            ["/bin/sh", "-c", "printf '%s\\0' " + line],
            capture_output=True,
        )
        self.assertEqual(probe.stdout.decode("utf-8").split("\0")[:-1], self.NASTY)

    def test_windows_command_is_ascii_and_free_of_shell_metacharacters(self):
        with mock.patch.object(platforms, "is_windows", return_value=True):
            line = platforms.shell_command(self.NASTY)
        self.assertTrue(line.isascii())
        prefix = "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
        self.assertTrue(line.startswith(prefix))
        payload = line[len(prefix):]
        self.assertRegex(payload, r"^[A-Za-z0-9+/=]+$")
        script = base64.b64decode(payload).decode("utf-16-le")
        self.assertEqual(script, platforms.powershell_script(self.NASTY))
        self.assertTrue(script.startswith("& '"))
        self.assertTrue(script.endswith("; exit $LASTEXITCODE"))
        self.assertNotIn("ReadToEnd", script)

    def test_windows_forward_stdin_reads_and_pipes_utf8(self):
        with mock.patch.object(platforms, "is_windows", return_value=True):
            line = platforms.shell_command(self.NASTY, forward_stdin=True)
        script = base64.b64decode(line.rsplit(" ", 1)[1]).decode("utf-16-le")
        self.assertIn("[Console]::In.ReadToEnd()", script)
        self.assertIn("InputEncoding = New-Object System.Text.UTF8Encoding $false", script)
        self.assertIn("$OutputEncoding = New-Object System.Text.UTF8Encoding $false", script)
        self.assertIn("$agydraStdin | & '", script)
        self.assertTrue(script.endswith("; exit $LASTEXITCODE"))
        self.assertTrue(line.isascii())

    def test_posix_ignores_forward_stdin(self):
        with mock.patch.object(platforms, "is_windows", return_value=False):
            self.assertEqual(
                platforms.shell_command(["a b"], forward_stdin=True), platforms.shell_command(["a b"])
            )

    @unittest.skipUnless(sys.platform.startswith("win"), "needs Windows PowerShell")
    def test_windows_generated_command_forwards_stdin_to_the_child(self):
        child = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
        payload = '{"name": "caf\u00e9"}'
        line = platforms.shell_command(child, forward_stdin=True)
        out = subprocess.run(
            line, shell=True, input=payload.encode("utf-8"), capture_output=True, timeout=60
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.decode("utf-8").strip(), payload)

    def test_powershell_literals_double_every_quote_variant(self):
        self.assertEqual(platforms.powershell_literal("it's"), "'it''s'")
        for quote in ("\u2018", "\u2019", "\u201a", "\u201b"):
            self.assertEqual(platforms.powershell_literal(f"a{quote}b"), f"'a{quote}{quote}b'")
        self.assertEqual(platforms.powershell_literal("$env:X `n %P% & |"), "'$env:X `n %P% & |'")

    def test_windows_paths_with_backslashes_and_spaces_are_literal(self):
        with mock.patch.object(platforms, "is_windows", return_value=True):
            line = platforms.shell_command([r"C:\Program Files\Python\python.exe", "100%"])
        script = base64.b64decode(line.rsplit(" ", 1)[1]).decode("utf-16-le")
        self.assertEqual(
            script, "& 'C:\\Program Files\\Python\\python.exe' '100%'; exit $LASTEXITCODE"
        )

    def test_rejects_empty_argv_and_nul(self):
        for windows in (False, True):
            with mock.patch.object(platforms, "is_windows", return_value=windows):
                with self.assertRaises(ValueError):
                    platforms.shell_command([])
                with self.assertRaises(ValueError):
                    platforms.shell_command(["a\x00b"])


class TestConfigDirValidation(ClaudeBase):
    def test_alias_of_real_claude_dir_is_rejected_via_symlinked_ancestor(self):
        real_claude = self.fake_home / ".claude"
        real_claude.mkdir()
        alias_root = self._tmp / "alias-root"
        alias_root.symlink_to(real_claude)
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_claude_config_dir(alias_root / "1")

    def test_nested_inside_real_claude_dir_is_rejected(self):
        real_claude = self.fake_home / ".claude"
        (real_claude / "inside").mkdir(parents=True)
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_claude_config_dir(real_claude / "inside")

    def test_state_files_must_not_be_links(self):
        cfg = self._tmp / "cfg"
        cfg.mkdir()
        outside = self._tmp / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        for name in (".claude.json", "settings.json", ".credentials.json"):
            with self.subTest(name=name):
                (cfg / name).symlink_to(outside)
                with self.assertRaises(isolation.IsolationError):
                    isolation.prepare_claude_config_dir(cfg)
                (cfg / name).unlink()
        (cfg / ".claude.json").write_text("{}", encoding="utf-8")
        self.assertEqual(isolation.prepare_claude_config_dir(cfg), cfg)

    def test_build_overlay_runs_the_central_validation(self):
        cfg = self._tmp / "cfg"
        cfg.mkdir()
        (cfg / "settings.json").symlink_to(self._tmp)
        with self.assertRaises(isolation.IsolationError):
            isolation.build_overlay("p", cfg, self.store_root, engine="claude")


class TestRunnerUsageIntegration(ClaudeBase):
    def setUp(self) -> None:
        super().setUp()
        self.make_profile("work")
        self.dump = self._tmp / "dump.json"
        os.environ["FAKE_CLAUDE_DUMP"] = str(self.dump)

    def run_plan(self, args, **kwargs):
        plan = runner.build_plan(self.store, args, flag_ref="work", launch_as_child=True)
        return plan, runner.run(plan, store=self.store, **kwargs)

    def seen(self):
        return json.loads(self.dump.read_text(encoding="utf-8"))

    def test_login_invalidates_inside_lock_then_captures_new_generation(self):
        import claude_usage

        events = []
        real_capture = claude_usage.capture_environment

        def invalidate(store, name, **kw):
            handle = locks.try_lock(store, name)
            events.append(("invalidate", handle is not None))
            if handle is not None:
                handle.release()
            return 7

        def capture(store, profile):
            events.append(("capture", None))
            return {"AGYDRA_CLAUDE_USAGE_SEQ": str(profile.seq), "AGYDRA_CLAUDE_USAGE_GENERATION": "7"}

        with mock.patch.object(claude_usage, "invalidate_profile_usage", side_effect=invalidate), \
                mock.patch.object(claude_usage, "capture_environment", side_effect=capture):
            plan = runner.build_plan(self.store, ["auth", "login"], flag_ref="work", launch_as_child=True)
            events.clear()
            self.assertEqual(runner.run(plan, store=self.store), 0)
        self.assertEqual(events, [("invalidate", True), ("capture", None)])
        env = self.seen()["env"]
        self.assertEqual(env["AGYDRA_CLAUDE_USAGE_GENERATION"], "7")
        self.assertEqual(env["AGYDRA_CLAUDE_USAGE_SEQ"], str(self.store.get("work").seq))
        self.assertTrue(callable(real_capture))

    def test_real_module_bumps_generation_on_login_but_not_on_plain_launch(self):
        import claude_usage

        self.run_plan(["--flag"])
        plain = self.seen()["env"]
        self.run_plan(["auth", "login"])
        after_login = self.seen()["env"]
        self.assertGreater(
            int(after_login["AGYDRA_CLAUDE_USAGE_GENERATION"]),
            int(plain.get("AGYDRA_CLAUDE_USAGE_GENERATION", "0")),
        )
        self.run_plan(["--flag"])
        again = self.seen()["env"]
        self.assertEqual(
            again["AGYDRA_CLAUDE_USAGE_GENERATION"], after_login["AGYDRA_CLAUDE_USAGE_GENERATION"]
        )
        self.assertTrue(claude_usage.SEQUENCE_ENV in again)

    def test_login_detected_from_raw_args_too(self):
        import claude_usage

        plan = runner.build_plan(self.store, ["auth", "login"], flag_ref="work", launch_as_child=True)
        plan.args = ["--something"]
        with mock.patch.object(claude_usage, "invalidate_profile_usage", return_value=1) as inval:
            runner.run(plan, store=self.store)
        inval.assert_called_once()

    def test_invalidation_failure_aborts_launch_and_releases_lock(self):
        import claude_usage

        plan = runner.build_plan(self.store, ["auth", "login"], flag_ref="work", launch_as_child=True)
        with mock.patch.object(
            claude_usage, "invalidate_profile_usage", side_effect=RuntimeError("disk full")
        ):
            with self.assertRaises(StoreError) as ctx:
                runner.run(plan, store=self.store)
        self.assertIn("launch aborted", str(ctx.exception))
        self.assertFalse(self.dump.exists())
        handle = locks.try_lock(self.store, "work")
        self.assertIsNotNone(handle)
        handle.release()

    def test_dry_run_login_mutates_nothing(self):
        import claude_usage

        before = sorted(str(p) for p in self.store.root.rglob("*"))
        saved = self.store.get("work")
        plan = runner.build_plan(self.store, ["auth", "login"], flag_ref="work", launch_as_child=True)
        with mock.patch.object(claude_usage, "invalidate_profile_usage") as inval:
            self.assertEqual(runner.run(plan, store=self.store, dry_run=True), 0)
        inval.assert_not_called()
        self.assertEqual(sorted(str(p) for p in self.store.root.rglob("*")), before)
        self.assertEqual(self.store.get("work"), saved)
        self.assertFalse(self.dump.exists())

    def test_build_plan_is_read_only(self):
        before = sorted(str(p) for p in self.store.root.rglob("*"))
        runner.build_plan(self.store, ["auth", "login"], flag_ref="work", launch_as_child=True)
        self.assertEqual(sorted(str(p) for p in self.store.root.rglob("*")), before)

    def test_foreground_flags_reach_the_child(self):
        self.run_plan(["--flag"])
        env = self.seen()["env"]
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AGENT_VIEW"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF"], "1")

    def test_describe_shows_real_env_without_secrets(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-super-secret"
        os.environ["SOME_OTHER_SECRET"] = "hunter2"
        plan = runner.build_plan(self.store, ["--flag"], flag_ref="work", launch_as_child=True)
        text = plan.describe()
        cfg = str(self.store.claude_config_dir("work"))
        self.assertIn(f"config  : {cfg}", text)
        self.assertIn(f"CLAUDE_CONFIG_DIR={cfg}", text)
        self.assertIn(f"HOME={self.fake_home}", text)
        self.assertIn("CLAUDE_CODE_DISABLE_AGENT_VIEW=1", text)
        seq = plan.env.get("AGYDRA_CLAUDE_USAGE_SEQ")
        self.assertIsNotNone(seq)
        self.assertIn(f"AGYDRA_CLAUDE_USAGE_SEQ={seq}", text)
        self.assertIn("ignored : ANTHROPIC_API_KEY", text)
        for secret in ("sk-super-secret", "hunter2"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, repr(plan))


@unittest.skipUnless(REAL_CLAUDE and not sys.platform.startswith("win"), "real claude binary not installed")
class TestRealBinarySandbox(unittest.TestCase):
    def test_status_is_read_only_and_stays_inside_config_dir(self):
        root = Path(tempfile.mkdtemp(prefix="agydra-real-claude-"))
        try:
            home, cfg = root / "home", root / "cfg"
            for name in ("home", "cfg", "xdg-config", "xdg-data", "xdg-cache", "xdg-state", "local"):
                (root / name).mkdir()
            base = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "USERPROFILE": str(home),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "XDG_DATA_HOME": str(root / "xdg-data"),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
                "XDG_STATE_HOME": str(root / "xdg-state"),
                "LOCALAPPDATA": str(root / "local"),
                "APPDATA": str(root / "local"),
                "TMPDIR": str(root),
            }
            with mock.patch.dict(os.environ, base, clear=True):
                env = isolation.isolated_env(cfg, {}, engine="claude")
            version = subprocess.run(
                [REAL_CLAUDE, "--version"], env=env, capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(version.returncode, 0)
            status = account.inspect_claude_auth(Path(REAL_CLAUDE), cfg, env, timeout=30)
            self.assertIn(status.state, ("unauthenticated", "unknown"))
            self.assertNotEqual(status.state, "authenticated")
            self.assertEqual(status.config_directory and Path(status.config_directory).resolve(), cfg.resolve())
            self.assertEqual(list(home.iterdir()), [])
            self.assertTrue((cfg / ".claude.json").is_file())
            self.assertFalse((cfg / ".claude.json").is_symlink())
            outside = [
                p.name for p in root.iterdir()
                if p.name not in {"home", "cfg", "xdg-config", "xdg-data", "xdg-cache", "xdg-state", "local"}
            ]
            self.assertEqual(outside, [])
            for name in ("xdg-config", "xdg-data", "xdg-cache", "xdg-state", "local"):
                self.assertEqual(list((root / name).iterdir()), [], name)
            print(f"[real claude sandbox] {version.stdout.strip()}")
        finally:
            shutil.rmtree(root, ignore_errors=True)



class TestInheritedUsageState(ClaudeBase):
    USAGE = ("AGYDRA_CLAUDE_USAGE_SEQ", "AGYDRA_CLAUDE_USAGE_GENERATION")

    def setUp(self) -> None:
        super().setUp()
        self.dump = self._tmp / "dump.json"
        os.environ["FAKE_CLAUDE_DUMP"] = str(self.dump)
        os.environ["AGYDRA_CLAUDE_USAGE_SEQ"] = "9"
        os.environ["AGYDRA_CLAUDE_USAGE_GENERATION"] = "77"

    def child_usage_env(self, name):
        plan = runner.build_plan(self.store, ["--flag"], flag_ref=name, launch_as_child=True)
        planned = {k: plan.env[k] for k in self.USAGE if k in plan.env}
        self.assertEqual(runner.run(plan, store=self.store), 0)
        seen = json.loads(self.dump.read_text(encoding="utf-8"))["env"]
        launched = {k: seen[k] for k in self.USAGE if k in seen}
        self.assertEqual(planned, launched)
        return launched

    def corrupt_generation(self, profile):
        path = self.store.usage_cache_root / ".generations" / f"{profile.seq}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")

    def test_constants_match_the_usage_module(self):
        import claude_usage

        self.assertEqual(
            (claude_usage.SEQUENCE_ENV, claude_usage.GENERATION_ENV), self.USAGE
        )
        self.assertEqual(
            engines.get_engine("claude").inherited_state_env,
            (platforms.CLAUDE_USAGE_SEQ_ENV, platforms.CLAUDE_USAGE_GENERATION_ENV),
        )

    def test_inherited_values_are_dropped_before_extra_and_extra_survives(self):
        cfg = self._tmp / "cfg"
        cfg.mkdir()
        bare = isolation.isolated_env(cfg, {}, engine="claude")
        for name in self.USAGE:
            self.assertNotIn(name, bare)
        injected = isolation.isolated_env(
            cfg,
            {"AGYDRA_CLAUDE_USAGE_SEQ": "3", "AGYDRA_CLAUDE_USAGE_GENERATION": "5"},
            engine="claude",
        )
        self.assertEqual(injected["AGYDRA_CLAUDE_USAGE_SEQ"], "3")
        self.assertEqual(injected["AGYDRA_CLAUDE_USAGE_GENERATION"], "5")

    def test_other_engines_keep_their_inherited_environment(self):
        data = self._tmp / "data"
        data.mkdir()
        overlay = isolation.build_overlay("p", data, self.store_root, engine="codex")
        env = isolation.isolated_env(overlay, {}, engine="codex")
        self.assertEqual(env["AGYDRA_CLAUDE_USAGE_SEQ"], "9")

    def test_corrupt_generation_never_falls_back_to_inherited_values(self):
        profile = self.make_profile("work")
        self.corrupt_generation(profile)
        self.assertEqual(self.child_usage_env("work"), {})

    def test_deleted_tombstone_never_falls_back_to_inherited_values(self):
        import claude_usage

        profile = self.make_profile("work")
        claude_usage.invalidate_profile_usage(self.store, "work", deleted=True)
        self.assertEqual(self.child_usage_env("work"), {})

    def test_valid_generation_overrides_inherited_values(self):
        import claude_usage

        profile = self.make_profile("work")
        generation = claude_usage.invalidate_profile_usage(self.store, "work")
        self.assertEqual(
            self.child_usage_env("work"),
            {
                "AGYDRA_CLAUDE_USAGE_SEQ": str(profile.seq),
                "AGYDRA_CLAUDE_USAGE_GENERATION": str(generation),
            },
        )

    def test_nested_launch_of_another_profile_gets_its_own_values(self):
        import claude_usage

        first = self.make_profile("alpha")
        second = self.make_profile("beta")
        claude_usage.invalidate_profile_usage(self.store, "alpha")
        claude_usage.invalidate_profile_usage(self.store, "alpha")
        parent = self.child_usage_env("alpha")
        with mock.patch.dict(os.environ, parent):
            child = self.child_usage_env("beta")
        self.assertEqual(parent["AGYDRA_CLAUDE_USAGE_SEQ"], str(first.seq))
        self.assertEqual(child["AGYDRA_CLAUDE_USAGE_SEQ"], str(second.seq))
        self.assertEqual(child["AGYDRA_CLAUDE_USAGE_GENERATION"], "0")

    def test_nested_launch_into_unreadable_generation_has_no_parent_leak(self):
        first = self.make_profile("alpha")
        second = self.make_profile("beta")
        self.corrupt_generation(second)
        with mock.patch.dict(
            os.environ,
            {
                "AGYDRA_CLAUDE_USAGE_SEQ": str(first.seq),
                "AGYDRA_CLAUDE_USAGE_GENERATION": "4",
            },
        ):
            self.assertEqual(self.child_usage_env("beta"), {})


if __name__ == "__main__":
    unittest.main()
