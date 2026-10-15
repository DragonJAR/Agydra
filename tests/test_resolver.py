"""Resolver cascade: flag → env → marker → default → first → none."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolver
import platforms
import runner
from store import Store, StoreError

from conftest import BaseCase, posix_only, write_fake_binary


class TestResolver(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("personal", "work", "lab"):
            self.store.create(name)
        self.store.set_default("work")

    def _resolve(self, flag=None, env=None, cwd=None):
        return resolver.resolve(self.store, flag_ref=flag, cwd=cwd, env=env or {})

    def test_flag_wins(self):
        res = self._resolve(flag="lab")
        self.assertEqual(res.name, "lab")
        self.assertIn("flag", res.reason)

    def test_flag_accepts_number(self):
        res = self._resolve(flag="#3")
        self.assertEqual(res.name, "lab")

    def test_env_var(self):
        res = self._resolve(env={resolver.PROFILE_ENV: "personal"})
        self.assertEqual(res.name, "personal")
        self.assertIn("environment", res.reason)

    def test_flag_beats_env(self):
        res = self._resolve(flag="lab", env={resolver.PROFILE_ENV: "personal"})
        self.assertEqual(res.name, "lab")

    def _resolve_engine(self, engine, flag=None, env=None, cwd=None):
        return resolver.resolve(
            self.store, flag_ref=flag, cwd=cwd or self.fake_home, env=env or {}, engine=engine
        )

    def test_inherited_env_profile_of_another_engine_yields_to_requested_engine(self):
        self.store.create("openai", engine="codex")
        res = self._resolve_engine("codex", env={resolver.PROFILE_ENV: "personal"})
        self.assertEqual(res.name, "openai")

    def test_inherited_env_profile_of_the_requested_engine_still_wins(self):
        self.store.create("openai", engine="codex")
        self.store.create("openai-2", engine="codex")
        res = self._resolve_engine("codex", env={resolver.PROFILE_ENV: "openai-2"})
        self.assertEqual(res.name, "openai-2")
        self.assertIn("environment", res.reason)

    def test_env_profile_of_another_engine_falls_through_to_a_matching_marker(self):
        self.store.create("openai", engine="codex")
        self.store.create("openai-2", engine="codex")
        project = self._tmp / "project"
        project.mkdir()
        (project / ".agydra").write_text("openai-2", encoding="utf-8")
        res = self._resolve_engine(
            "codex", env={resolver.PROFILE_ENV: "personal"}, cwd=project
        )
        self.assertEqual(res.name, "openai-2")
        self.assertIn("marker", res.reason)

    def test_explicit_flag_of_another_engine_still_refuses(self):
        self.store.create("openai", engine="codex")
        with self.assertRaises(StoreError) as ctx:
            self._resolve_engine("codex", flag="personal")
        self.assertIn("was requested", str(ctx.exception))

    def test_stale_env_profile_continues_cascade_silently(self):
        self.store.create("openai", engine="codex")
        res = self._resolve_engine("codex", env={resolver.PROFILE_ENV: "ghost"})
        self.assertEqual(res.name, "openai")

    def test_stale_env_profile_without_engine_falls_back_to_default_silently(self):
        res = self._resolve(env={resolver.PROFILE_ENV: "gone"}, cwd=self.fake_home)
        self.assertEqual(res.name, "work")

    def test_missing_flag_or_marker_profile_still_raises(self):
        with self.assertRaises(StoreError):
            self._resolve(flag="ghost")
        project = self._tmp / "project" / "ghost_marker"
        project.mkdir(parents=True)
        (project / ".agydra").write_text("ghost", encoding="utf-8")
        with self.assertRaises(StoreError):
            self._resolve(cwd=project)

    def test_marker_file_wins_over_default(self):
        project = self._tmp / "project" / "sub"
        project.mkdir(parents=True)
        (self._tmp / "project" / ".agydra").write_text("lab", encoding="utf-8")
        res = self._resolve(cwd=project)
        self.assertEqual(res.name, "lab")
        self.assertIn("marker", res.reason)

    def test_build_plan_forwards_cwd(self):
        project = self._tmp / "project" / "sub"
        project.mkdir(parents=True)
        (self._tmp / "project" / ".agydra").write_text("lab", encoding="utf-8")
        plan = runner.build_plan(self.store, [], cwd=project)
        self.assertEqual(plan.profile, "lab")
        self.assertIn("marker", plan.reason)

    def test_empty_marker_file_raises_actionable_error(self):
        project = self._tmp / "project" / "empty_marker"
        project.mkdir(parents=True)
        (project / ".agydra").write_text("   \n", encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            self._resolve(cwd=project)
        self.assertIn("empty", str(ctx.exception))

    def test_default_used_when_nothing_else(self):
        res = self._resolve(cwd=self.fake_home)
        self.assertEqual(res.name, "work")
        self.assertIn("default", res.reason)

    def test_first_profile_when_no_default(self):
        config = self.store.load_config()
        config.default_profile = None
        self.store.save_config(config)
        res = self._resolve(cwd=self.fake_home)
        self.assertEqual(res.name, "personal")
        self.assertIn("first", res.reason)

    def test_no_profiles_raises_actionable_error(self):
        empty_root = self._tmp / "empty-store"
        empty_store = Store(empty_root)
        with self.assertRaises(StoreError) as ctx:
            resolver.resolve(empty_store, cwd=self.fake_home, env={})
        self.assertIn("agydra create", str(ctx.exception))

    def test_corrupt_default_profile_propagates_store_error(self):
        meta = self.store.profile_meta_path("work")
        meta.write_text("{corrupted json", encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            self._resolve(cwd=self.fake_home)
        self.assertNotIn("does not exist", str(ctx.exception))

    def test_nonexistent_default_with_empty_store_does_not_warn_fallback(self):
        from unittest import mock
        import ui
        empty_root = self._tmp / "empty-store"
        empty_store = Store(empty_root)
        config = empty_store.load_config()
        config.default_profile = "ghost"
        empty_store.save_config(config)
        with mock.patch.object(ui, "warn") as mock_warn:
            with self.assertRaises(StoreError):
                resolver.resolve(empty_store, cwd=self.fake_home, env={})
            mock_warn.assert_not_called()

    def test_read_only_plan_fails_closed_without_changing_pending_journal(self):
        journal = self.store.rename_journal_path
        journal_bytes = b"pending rename journal must be preserved\x00\xff"
        journal.write_bytes(journal_bytes)

        with mock.patch.object(
            Store,
            "_recover_pending_rename",
            side_effect=AssertionError("read-only planning must not recover state"),
        ):
            with self.assertRaisesRegex(StoreError, "recovery is pending|unreadable or malformed"):
                runner.build_plan(
                    self.store, [], flag_ref="work", read_only=True
                )

        self.assertEqual(journal.read_bytes(), journal_bytes)

    def test_read_only_random_plan_uses_passive_auth_without_recovery_or_native_probes(self):
        token_dir = self.store.profile_data_dir("lab") / "antigravity-cli"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "antigravity-oauth-token").write_text(
            '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
        )
        with mock.patch.object(
            resolver.account,
            "claude_auth_status",
            side_effect=AssertionError("read-only planning must not spawn auth probes"),
        ), mock.patch.object(
            Store,
            "_recover_pending_rename",
            side_effect=AssertionError("read-only planning must not recover state"),
        ):
            plan = runner.build_plan(
                self.store, [], random_pick=True, cwd=self.fake_home, read_only=True
            )

        self.assertEqual(plan.profile, "lab")
        self.assertIn("read-only", plan.reason)
        self.assertFalse(
            any(self.store.root.glob("profile-rotation-*.json")),
            "a read-only preview must not write rotation state",
        )


class TestPickFreeProfileAuthAndBusyOrder(BaseCase):
    """pick_free_profile filter order: authenticated profiles are verified
    first so that busy profiles are not misdiagnosed as unauthenticated."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")

    def _authenticate(self, name: str) -> None:
        token_dir = self.store.profile_data_dir(name) / "antigravity-cli"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "antigravity-oauth-token").write_text(
            '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
        )

    def test_not_authenticated_when_no_profiles_have_tokens(self):
        """When neither profile is authenticated, error must indicate auth."""
        with self.assertRaises(StoreError) as ctx:
            resolver.pick_free_profile(self.store)
        self.assertIn("authenticated", str(ctx.exception))
        self.assertIn("agydra login", str(ctx.exception))

    def test_no_free_when_only_authenticated_profile_is_busy(self):
        """When an authenticated profile is busy (and the only free candidate
        is unauthenticated), -r JOINS the busy authenticated profile:
        concurrent same-profile sessions are the supported mode."""
        import locks

        self._authenticate("alpha")
        handle = locks.try_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        try:
            res = resolver.pick_free_profile(self.store)
            self.assertEqual(res.name, "alpha")
            self.assertIn("joining busy profile", res.reason)
        finally:
            handle.release()

    def test_read_only_preview_skips_unauthenticated_profile(self):
        self._authenticate("beta")
        res = resolver.pick_free_profile(self.store, read_only=True)
        self.assertEqual(res.name, "beta")

    def test_read_only_preview_without_authenticated_profile_fails_like_launch(self):
        with self.assertRaisesRegex(StoreError, "agydra login"):
            resolver.pick_free_profile(self.store, read_only=True)

    def test_read_only_preview_prefers_free_profile_over_busy(self):
        import locks

        self._authenticate("alpha")
        self._authenticate("beta")
        handle = locks.try_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        try:
            res = resolver.pick_free_profile(self.store, read_only=True)
        finally:
            handle.release()
        self.assertEqual(res.name, "beta")
        self.assertIn("free profile", res.reason)

    def test_read_only_preview_honors_session_limit(self):
        import locks

        self._authenticate("alpha")
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 1
        self.store.save_config(config)
        locks.acquire_lease(self.store, "alpha", patience_s=1.0, max_holders=1)
        try:
            with self.assertRaisesRegex(StoreError, "limit of 1 live session"):
                resolver.pick_free_profile(self.store, read_only=True)
            res = resolver.pick_free_profile(self.store, read_only=True, force=True)
        finally:
            locks.release_lease(self.store, "alpha", patience_s=1.0)
        self.assertEqual(res.name, "alpha")
        self.assertIn("joining busy profile", res.reason)

    def test_picks_authenticated_profile_when_unauthenticated_is_free(self):
        """When alpha is authenticated and free, and beta is unauthenticated,
        alpha must be picked."""
        self._authenticate("alpha")
        res = resolver.pick_free_profile(self.store)
        self.assertEqual(res.name, "alpha")

    def test_force_picks_busy_authenticated_over_unauthenticated_free(self):
        """``force`` keeps its CLI compatibility alias: pick_free_profile
        joins the busy authenticated profile and reports a "joining"
        reason. The old "forced" reason is gone — the join semantics
        subsume the old busy-skip behavior."""
        import locks

        self._authenticate("alpha")
        handle = locks.try_lock(self.store, "alpha")
        self.assertIsNotNone(handle)
        try:
            res = resolver.pick_free_profile(self.store, force=True)
            self.assertEqual(res.name, "alpha")
            self.assertIn("joining busy profile", res.reason)
        finally:
            handle.release()

    def test_single_profile_random_pick_ignores_marker_pin(self):
        """A project pin affects normal resolution, never random selection."""
        single_root = self._tmp / "single-store"
        single_store = Store(single_root)
        single_store.create("solo")
        token_dir = single_store.profile_data_dir("solo") / "antigravity-cli"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "antigravity-oauth-token").write_text(
            '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
        )
        proj = self._tmp / "proj"
        proj.mkdir()
        (proj / ".agydra").write_text("solo\n", encoding="utf-8")
        res = resolver.pick_free_profile(single_store, cwd=proj)
        self.assertEqual(res.name, "solo")
        self.assertIn("rotation", res.reason)
        self.assertNotIn("marker", res.reason)

        res = resolver.pick_free_profile(single_store, cwd=self._tmp)
        self.assertEqual(res.name, "solo")
        self.assertIn("rotation", res.reason)

    def test_random_pick_ignores_project_marker_pin(self):
        self._authenticate("alpha")
        self._authenticate("beta")
        project = self._tmp / "pinned-project"
        project.mkdir()
        (project / ".agydra").write_text("beta", encoding="utf-8")

        result = resolver.pick_free_profile(self.store, cwd=project)

        self.assertEqual(result.name, "alpha")
        self.assertNotIn("marker", result.reason)

    def test_random_retry_exclusion_is_applied_with_project_marker_present(self):
        self._authenticate("alpha")
        self._authenticate("beta")
        project = self._tmp / "pinned-project"
        project.mkdir()
        (project / ".agydra").write_text("alpha", encoding="utf-8")

        result = resolver.pick_free_profile(
            self.store, cwd=project, exclude={"alpha"}
        )

        self.assertEqual(result.name, "beta")


class TestPickFreeProfileLazyProbing(BaseCase):
    """Auth probes stop at the first eligible profile in (last_used, seq) order."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta", "gamma"):
            self.store.create(name)

    def _probed_names(self, states, **kwargs):
        probed = []

        def fake_auth_state(_data_dir, _store, name, engine="agy"):
            probed.append(name)
            return states.get(name, "not-authenticated")

        with mock.patch.object(resolver.account, "auth_state", side_effect=fake_auth_state), \
                mock.patch.object(
                    resolver.account, "usable_agy_credential", return_value=b"credential"
                ):
            try:
                res = resolver.pick_free_profile(self.store, **kwargs)
            except StoreError as exc:
                return None, probed, exc
        return res, probed, None

    def test_first_authenticated_free_profile_costs_one_probe(self):
        res, probed, _ = self._probed_names({n: "authenticated" for n in ("alpha", "beta", "gamma")})
        self.assertEqual(res.name, "alpha")
        self.assertEqual(probed, ["alpha"])

    def test_least_recently_used_wins_over_creation_order(self):
        beta = self.store.get("beta")
        beta.last_used = "2026-01-01T00:00:00Z"
        self.store.save(beta)
        gamma = self.store.get("gamma")
        gamma.last_used = "2025-01-01T00:00:00Z"
        self.store.save(gamma)
        res, probed, _ = self._probed_names({n: "authenticated" for n in ("alpha", "beta", "gamma")})
        self.assertEqual(res.name, "alpha")
        res, probed, _ = self._probed_names({"beta": "authenticated", "gamma": "authenticated"})
        self.assertEqual(res.name, "gamma")
        self.assertEqual(probed, ["alpha", "gamma"])

    def test_busy_profile_is_skipped_unless_forced(self):
        import locks

        handle = locks.try_lock(self.store, "alpha")
        try:
            states = {n: "authenticated" for n in ("alpha", "beta", "gamma")}
            res, probed, _ = self._probed_names(states)
            self.assertEqual((res.name, probed), ("beta", ["beta"]))
        finally:
            handle.release()

    def test_error_selection_matches_busy_versus_unauthenticated(self):
        import locks

        _res, _probed, exc = self._probed_names({})
        self.assertIn("no authenticated profile available", str(exc))
        handles = [locks.try_lock(self.store, n) for n in ("alpha", "beta", "gamma")]
        try:
            res, probed, _ = self._probed_names({"beta": "authenticated"})
            self.assertEqual((res.name, probed), ("beta", ["alpha", "beta"]))
            self.assertIn("joining busy profile", res.reason)
        finally:
            for handle in handles:
                handle.release()


@posix_only("POSIX fake claude executable")
class TestPickFreeProfileClaudeProbeCount(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.log = self._tmp / "claude-probes.log"
        fake = write_fake_binary(
            self._tmp / "bin",
            "claude",
            body=(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                f"with open({str(self.log)!r}, 'a', encoding='utf-8') as output:\n"
                "    output.write(os.environ.get('CLAUDE_CONFIG_DIR', '') + '\\n')\n"
                "print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', "
                "'apiProvider': 'firstParty', 'email': 'cc@example.test', "
                "'configDirectory': os.environ.get('CLAUDE_CONFIG_DIR')}))\n"
            ),
        )
        os.environ["AGYDRA_CLAUDE_BIN"] = str(fake)
        for name in ("c1", "c2", "c3", "c4"):
            self.store.create(name, engine="claude")

    def test_claude_rotation_runs_one_status_probe_when_first_is_eligible(self):
        res = resolver.pick_free_profile(self.store, engine="claude", force=True)
        self.assertEqual(res.name, "c1")
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)

    def test_claude_rotation_without_engine_and_without_agy_profiles_probes_once(self):
        res = resolver.pick_free_profile(self.store)
        self.assertEqual(res.name, "c1")
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)


    def test_read_only_preview_never_spawns_claude_status_probe(self):
        res = resolver.pick_free_profile(self.store, read_only=True)
        self.assertEqual(res.name, "c1")
        self.assertIn("native CLI probe, not probed", res.reason)
        self.assertFalse(self.log.exists())


class TestF2StoreErrorPropagation(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        for name in ("alpha", "beta"):
            self.store.create(name)

    def test_pick_free_profile_raises_no_eligible_profile_error(self):
        with self.assertRaises(resolver.NoEligibleProfileError):
            resolver.pick_free_profile(self.store, exclude={"alpha", "beta"})

    def test_build_plan_does_not_mask_store_wide_error_when_candidates_excluded(self):
        calls = 0

        def fake_pick(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return resolver.Resolution(name="alpha", reason="rot")
            raise StoreError("FATAL ROOT PERMISSION FAILURE")

        import engines
        driver = engines.get_engine("agy")
        with mock.patch("resolver.pick_free_profile", side_effect=fake_pick), \
             mock.patch.object(driver, "resolve_configured_binary_result", side_effect=[platforms.BinaryResult(reason="not_found"), platforms.BinaryResult(found=Path("/bin/echo"))]):
            with self.assertRaises(StoreError) as cm:
                runner.build_plan(self.store, [], random_pick=True)
            self.assertEqual(str(cm.exception), "FATAL ROOT PERMISSION FAILURE")


if __name__ == "__main__":
    unittest.main()
