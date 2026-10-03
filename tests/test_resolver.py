"""Resolver cascade: flag → env → marker → default → first → none."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolver
import runner
from store import Store, StoreError

from conftest import BaseCase


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
            with self.assertRaisesRegex(StoreError, "recovery is pending"):
                runner.build_plan(
                    self.store, [], flag_ref="work", read_only=True
                )

        self.assertEqual(journal.read_bytes(), journal_bytes)

    def test_read_only_random_plan_skips_auth_and_recovery_probes(self):
        with mock.patch.object(
            resolver.account,
            "auth_state",
            side_effect=AssertionError("read-only planning must not probe auth"),
        ), mock.patch.object(
            Store,
            "_recover_pending_rename",
            side_effect=AssertionError("read-only planning must not recover state"),
        ):
            plan = runner.build_plan(
                self.store, [], random_pick=True, cwd=self.fake_home, read_only=True
            )

        self.assertEqual(plan.profile, "personal")
        self.assertIn("not probed", plan.reason)


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

        with mock.patch.object(resolver.account, "auth_state", side_effect=fake_auth_state):
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


@unittest.skipIf(sys.platform.startswith("win"), "POSIX fake claude executable")
class TestPickFreeProfileClaudeProbeCount(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.log = self._tmp / "claude-probes.log"
        fake = self._tmp / "bin" / "claude"
        fake.parent.mkdir(exist_ok=True)
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"open({str(self.log)!r}, 'a').write(os.environ.get('CLAUDE_CONFIG_DIR', '') + '\\n')\n"
            "print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', "
            "'apiProvider': 'firstParty', 'email': 'cc@example.test', "
            "'configDirectory': os.environ.get('CLAUDE_CONFIG_DIR')}))\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
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


if __name__ == "__main__":
    unittest.main()
