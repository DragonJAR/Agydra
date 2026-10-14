from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import isolation
import keychain
import locks
import platforms
import profile_rotation
import resolver
import runner
from conftest import BaseCase, _make_jwt
from store import Store


class TestFybAuthLaunch(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.alpha = self.store.create("alpha")
        self.beta = self.store.create("beta")
        self.alpha.email = "alpha@example.com"
        self.beta.email = "beta@example.com"
        self.store.save(self.alpha)
        self.store.save(self.beta)

    def _token(self, email):
        return json.dumps({
            "token": {"access_token": "access", "refresh_token": "refresh"},
            "id_token": _make_jwt({"email": email}),
        }).encode("utf-8")

    def _write_token(self, name, email):
        path = self.store.profile_data_dir(name, engine="agy") / account.AGY_CLI_DIR / account.TOKEN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._token(email))
        path.chmod(0o600)
        return path

    def test_random_eligibility_rejects_a_hard_linked_token(self):
        alpha_path = self._write_token("alpha", "alpha@example.com")
        beta_path = self._write_token("beta", "beta@example.com")
        outside = self._tmp / "linked-token"
        outside.write_bytes(alpha_path.read_bytes())
        alpha_path.unlink()
        try:
            os.link(outside, alpha_path)
        except (AttributeError, OSError):
            self.skipTest("hard links are unavailable")
        beta_path.chmod(0o600)

        rotation = runner._RotationContext(
            self.store, profile_rotation.Rotation(self.store, "agy")
        )
        with rotation:
            plan = runner.build_plan(
                self.store, [], random_pick=True, engine="agy", rotation=rotation
            )

        self.assertEqual(plan.profile, "beta")
        with self.assertRaises(isolation.AgyCredentialUntrustedError):
            isolation.prepare_agy_file_auth(
                self.store,
                self.store.get("alpha"),
                self.store.profile_data_dir("alpha", engine="agy"),
            )

    def test_adopting_an_identityless_login_does_not_publish_it(self):
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        raw = b'{"token":{"access_token":"access","refresh_token":"refresh"}}'

        keychain.adopt_login_credential(self.store, "alpha", data_dir, raw)

        self.assertFalse(account.agy_token_path(data_dir).exists())
        self.assertIsNone(keychain.load_profile_slot(self.store, "alpha"))

    def test_overlay_env_scrubs_driver_foreign_authentication(self):
        import engines

        class Driver:
            uses_overlay = True
            env_home_var = "CODEX_HOME"
            data_dir_name = ".codex"

            @staticmethod
            def scrub_env(env):
                env.pop("OPENAI_API_KEY", None)
                return ["OPENAI_API_KEY"]

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "inherited"}), \
                mock.patch.object(engines, "get_engine", return_value=Driver()):
            env = isolation.isolated_env(self._tmp / "overlay", {}, engine="codex")

        self.assertNotIn("OPENAI_API_KEY", env)

    def test_identity_anchor_prefers_profile_email_then_disk_token(self):
        self._write_token("alpha", "disk@example.com")

        self.assertEqual(account.anchor_for(self.store, "alpha"), "alpha@example.com")
        profile = self.store.get("alpha")
        profile.email = None
        self.store.save(profile)
        self.assertEqual(account.anchor_for(self.store, "alpha"), "disk@example.com")

    def test_identity_anchor_reuses_a_scanned_profile(self):
        with mock.patch.object(self.store, "get", wraps=self.store.get) as lookup:
            self.assertEqual(account.anchor_for(self.store, self.alpha), "alpha@example.com")

        lookup.assert_called_once_with("alpha", profile=self.alpha)

    def test_identity_anchor_uses_the_shared_utf8_bom_json_reader(self):
        path = self._write_token("alpha", "alpha@example.com")
        raw = b"\xef\xbb\xbf" + self._token("alpha@example.com")
        path.write_bytes(raw)
        profile = self.store.get("alpha")
        profile.email = None
        self.store.save(profile)

        self.assertEqual(account.anchor_for(self.store, "alpha"), "alpha@example.com")
        self.assertEqual(
            account.usable_agy_credential(self.store, profile, path.parent.parent), raw
        )

    def test_slot_lease_uses_the_shared_utf8_bom_json_decoder(self):
        locks.acquire_lease(self.store, "alpha", keychain=True)
        self.addCleanup(locks.release_lease, self.store, "alpha")
        lease_path = keychain._slot_lease_path(self.store)
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps(
                {"owner": "alpha", "had_shared": "b3JpZ2luYWwtYmFzZWxpbmU="}
            ).encode("utf-8")
        )

        state = keychain._load_slot_lease(self.store)

        self.assertEqual(state.owner, "alpha")
        self.assertEqual(keychain._decode_shared(state.had_shared), b"original-baseline")

    def test_identity_anchor_can_exclude_the_backup_it_is_validating(self):
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        profile = self.store.get("alpha")
        profile.email = None
        self.store.save(profile)
        keychain.save_profile_slot(
            self.store,
            "alpha",
            keychain.envelope_token_bytes(self._token("foreign@example.com")),
        )

        self.assertIsNone(
            account.anchor_for(
                self.store, "alpha", data_dir, include_secret=False
            )
        )

    def test_overlay_replacement_removes_stale_data_links_with_store_helper(self):
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        overlay = isolation.build_overlay("alpha", data_dir, self.store.root)
        link = overlay / ".gemini"
        stale_target = self._tmp / "stale-gemini"
        stale_target.mkdir()
        link.unlink()
        link.symlink_to(stale_target, target_is_directory=True)

        with mock.patch.object(isolation.store, "rmtree", wraps=isolation.store.rmtree) as remove:
            isolation.build_overlay("alpha", data_dir, self.store.root)

        remove.assert_any_call(link)

    def test_slot_lease_is_saved_before_shared_slot_mutation(self):
        import locks

        profile = self.store.create("empty")
        events = []

        def fail_lease_save(*args, **kwargs):
            events.append("lease")
            raise OSError("lease write failed")

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "_ensure_target_keychain", return_value=Path("/fake.keychain")), \
                mock.patch.object(keychain, "read_slot", return_value=b"ambient"), \
                mock.patch.object(keychain, "write_slot", side_effect=lambda *a, **k: events.append("write")), \
                mock.patch.object(keychain, "delete_slot", side_effect=lambda *a, **k: events.append("delete")), \
                mock.patch.object(keychain, "_save_slot_lease", side_effect=fail_lease_save), \
                mock.patch.object(self.store, "get", wraps=self.store.get) as get_profile, \
                mock.patch.object(self.store, "profile_data_dir", wraps=self.store.profile_data_dir) as data_dir_for:
            guard = keychain.launch_guard(
                self.store, profile.name, profile_snapshot=profile
            )
            guard.__enter__()
            guard.__exit__(None, None, None)

        self.assertEqual(events, ["lease"])
        self.assertEqual(locks.lease_holders(self.store, profile.name), [])
        get_profile.assert_called_once_with(profile.name, profile=profile)
        data_dir_for.assert_called_once_with(
            profile.name, engine="agy", profile=profile
        )

    def test_slow_claude_auth_is_probed_before_rotation_locks(self):
        self.store.create("claude-one", engine="claude")
        plan = runner.LaunchPlan(
            profile="alpha",
            reason="test",
            binary=self.agy_bin,
            args=[],
            overlay=self._tmp,
            env_home_var="HOME",
            env_home_value=self.fake_home,
            use_sandbox=False,
            random_pick=True,
        )
        probed = []
        original_enter = profile_rotation.Rotation.__enter__

        def auth_state(data_dir, store, name, engine="agy"):
            probed.append(name)
            return "authenticated"

        def enter(rotation):
            self.assertIn("claude-one", probed)
            return original_enter(rotation)

        with mock.patch.object(account, "auth_state", side_effect=auth_state), \
                mock.patch.object(profile_rotation.Rotation, "__enter__", enter), \
                mock.patch.object(runner, "build_plan", return_value=plan), \
                mock.patch.object(runner, "_run_prepared", return_value=0):
            self.assertEqual(runner.run(plan, store=self.store), 0)

    def test_rotation_replan_inputs_are_cached(self):
        import locks

        rotation = runner._RotationContext(
            self.store, profile_rotation.Rotation(self.store, "agy")
        )
        with mock.patch.object(self.store, "list", wraps=self.store.list) as list_profiles, \
                mock.patch.object(self.store, "load_config", wraps=self.store.load_config) as load_config:
            self.assertEqual(rotation.profiles(), rotation.profiles())
            self.assertEqual(rotation.config(), rotation.config())
        list_profiles.assert_called_once()
        load_config.assert_called_once()
        with mock.patch.object(locks, "is_locked", return_value=True) as is_locked, \
                mock.patch.object(locks, "lease_holders", return_value=[]) as lease_holders:
            state = rotation.session_state(self.alpha)
            self.assertEqual(rotation.session_state(self.alpha), state)
        is_locked.assert_called_once_with(self.store, "alpha")
        lease_holders.assert_called_once_with(self.store, "alpha")

    def test_random_plan_reuses_resolver_profile_snapshot(self):
        self._write_token("alpha", "alpha@example.com")
        self._write_token("beta", "beta@example.com")
        rotation = runner._RotationContext(
            self.store, profile_rotation.Rotation(self.store, "agy")
        )

        with mock.patch.object(self.store, "get", wraps=self.store.get) as lookup:
            runner.build_plan(
                self.store,
                [],
                random_pick=True,
                engine="agy",
                rotation=rotation,
            )

        self.assertEqual(lookup.call_count, 2)
        self.assertTrue(
            all(call.kwargs.get("profile") is not None for call in lookup.call_args_list)
        )

    def test_write_agy_token_validates_its_path_once(self):
        data_dir = self.store.profile_data_dir("alpha", engine="agy")
        raw = self._token("alpha@example.com")

        with mock.patch.object(account, "agy_token_path", wraps=account.agy_token_path) as validate:
            path = account.write_agy_token(data_dir, raw)

        validate.assert_called_once_with(data_dir, create_parent=True)
        self.assertEqual(path.read_bytes(), raw)

    def test_write_agy_token_rejects_linked_data_dir_before_creating_parent(self):
        outside = self._tmp / "outside-data"
        outside.mkdir()
        linked_data = self._tmp / "linked-data"
        try:
            linked_data.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("directory symlinks are unavailable")

        with self.assertRaises(account.store.StoreError):
            account.write_agy_token(linked_data, self._token("alpha@example.com"))

        self.assertFalse((outside / account.AGY_CLI_DIR).exists())


    def test_rotation_cycle_exhaustion_has_a_typed_exception(self):
        self._write_token("alpha", "alpha@example.com")
        self._write_token("beta", "beta@example.com")
        rotation = runner._RotationContext(
            self.store, profile_rotation.Rotation(self.store, "agy")
        )

        with mock.patch.object(
            profile_rotation, "_read_used", wraps=profile_rotation._read_used
        ) as read_used:
            with self.assertRaises(resolver.RotationCycleExhaustedError) as raised:
                resolver.pick_free_profile(
                    self.store,
                    engine="agy",
                    exclude={"alpha", "beta"},
                    rotation=rotation,
                )

        self.assertIsInstance(
            raised.exception.__cause__, profile_rotation.RotationCycleExhaustedError
        )
        read_used.assert_called_once()

    def test_candidate_exhaustion_message_is_typed_and_shared(self):
        exhausted = runner._candidate_exhaustion_error(
            "agy", {"alpha"}, {"alpha": "unsafe credential"}, RuntimeError("empty")
        )

        self.assertTrue(exhausted.formatted_exhaustion)
        self.assertIn("unsafe credential", str(exhausted))
        self.assertIn("alpha already tried", str(exhausted))


class TestFybBinaryUnavailableExitCodes(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)
        self.store.create("work")

    def _assert_exit_code(self, expected, binary_override=None):
        with self.assertRaises(runner.BinaryUnavailableError) as ctx:
            runner.build_plan(
                self.store, [], flag_ref="work", binary_override=binary_override
            )
        self.assertEqual(ctx.exception.exit_code, expected)

    def _write_non_executable(self, path):
        path.write_text("not executable", encoding="utf-8")
        if not platforms.is_windows():
            path.chmod(0o644)
        return path

    def test_missing_binary_from_path_exits_127(self):
        path = self._tmp / "empty-path"
        path.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(path), "AGYDRA_AGY_BIN": ""}):
            self._assert_exit_code(127)

    def test_missing_environment_binary_exits_127(self):
        missing = str(self._tmp / "missing-environment-binary")
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp), "AGYDRA_AGY_BIN": missing}):
            self._assert_exit_code(127)

    def test_missing_configured_binary_exits_127(self):
        missing = str(self._tmp / "missing-configured-binary")
        self.store.update_config(lambda config: setattr(config, "agy_binary", missing))
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp), "AGYDRA_AGY_BIN": ""}):
            self._assert_exit_code(127)

    def test_missing_explicit_binary_exits_127(self):
        missing = str(self._tmp / "missing-explicit-binary")
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp), "AGYDRA_AGY_BIN": ""}):
            self._assert_exit_code(127, binary_override=missing)

    def test_non_executable_binary_from_path_exits_126(self):
        path = self._tmp / "path-with-non-executable"
        path.mkdir()
        binary = path / "agy"
        self._write_non_executable(binary)
        with mock.patch.dict(os.environ, {"PATH": str(path), "AGYDRA_AGY_BIN": ""}):
            self._assert_exit_code(126)

    def test_non_executable_environment_binary_exits_126(self):
        binary = self._write_non_executable(self._tmp / "configured-non-executable.txt")
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp), "AGYDRA_AGY_BIN": str(binary)}):
            self._assert_exit_code(126)

    def test_non_executable_explicit_binary_exits_126(self):
        binary = self._write_non_executable(self._tmp / "explicit-non-executable.txt")
        self._assert_exit_code(126, binary_override=str(binary))

    def test_non_executable_configured_binary_exits_126(self):
        binary = self._write_non_executable(self._tmp / "configured-non-executable.txt")
        self.store.update_config(lambda config: setattr(config, "agy_binary", str(binary)))
        with mock.patch.dict(os.environ, {"PATH": str(self._tmp), "AGYDRA_AGY_BIN": ""}):
            self._assert_exit_code(126)
