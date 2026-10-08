"""Persistent random profile rotation and quota ranking regressions."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account
import keychain
import locks
import platforms
import profile_rotation
import resolver
import runner
import usage_snapshot
from conftest import BaseCase
from store import Store, StoreError


class TestProfileRotation(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)
        self.project = self._tmp / "project"
        self.project.mkdir()
        self.executables = {}
        for engine in ("codex", "grok", "claude"):
            executable = self.bin_dir / engine
            executable.write_text("synthetic executable", encoding="utf-8")
            executable.chmod(0o755)
            self.executables[engine] = executable
            os.environ[f"AGYDRA_{engine.upper()}_BIN"] = str(executable)
        self.launched = []
        self.now = datetime.now(timezone.utc)

    def _create(self, engine, *names):
        return [self.store.create(name, engine=engine) for name in names]

    def _auth_state(self, states=None):
        states = states or {}

        def state(_data_dir, _store, name, engine="agy"):
            return states.get(name, "authenticated")

        return state

    def _entry(
        self,
        profile,
        available,
        *,
        engine=None,
        stale=False,
        ok=True,
        error=None,
        reset_at=None,
        identity_verified=None,
    ):
        actual_engine = engine or profile.engine
        family = {
            "agy": "gemini",
            "codex": "codex",
            "grok": "grok",
            "claude": "claude_code",
        }[actual_engine]
        entry = {
            "name": profile.name,
            "seq": profile.seq,
            "engine": actual_engine,
            "ok": ok,
            "error": error,
            "stale": stale,
            "quality": "observed",
            "observed_at": self.now.isoformat(),
            "summary": {
                family: {"available": available, "reset_at": reset_at}
            },
        }
        if identity_verified is not None:
            entry["identity_verified"] = identity_verified
        return entry

    def _write_snapshot(self, entries):
        document = {
            "schema_version": usage_snapshot.SCHEMA_VERSION,
            "generated_at": self.now.isoformat(),
            "command": "usage",
            "scope": "all",
            "profiles": entries,
        }
        usage_snapshot.snapshot_path(self.store).write_text(
            json.dumps(document), encoding="utf-8"
        )

    def _record_launch(self, _argv, env):
        self.launched.append(env.get(resolver.PROFILE_ENV))
        return 0

    def _random_plan(self, engine="codex", _auth_states=None, **kwargs):
        with mock.patch.object(
            account, "auth_state", side_effect=self._auth_state(_auth_states)
        ):
            return runner.build_plan(
                self.store,
                ["chat"],
                random_pick=True,
                engine=engine,
                cwd=self.project,
                **kwargs,
            )

    def _launch_random(self, engine="codex", _auth_states=None, **kwargs):
        plan = self._random_plan(
            engine=engine, _auth_states=_auth_states, **kwargs
        )
        with mock.patch.object(
            account, "auth_state", side_effect=self._auth_state(_auth_states)
        ), mock.patch.object(
            platforms, "launch_argv", side_effect=self._record_launch
        ):
            self.assertEqual(runner.run(plan, store=self.store), 0)
        return self.launched[-1]

    def test_known_quota_ranks_first_and_unknown_profiles_finish_cycle(self):
        profiles = self._create(
            "codex", "low", "high", "missing", "stale", "expired", "error"
        )
        self._write_snapshot([
            self._entry(profiles[0], 0.6),
            self._entry(profiles[1], 0.9),
            self._entry(profiles[3], 0.99, stale=True),
            self._entry(
                profiles[4], 0.98,
                reset_at=(self.now - timedelta(seconds=1)).isoformat(),
            ),
            self._entry(profiles[5], 0.97, ok=False, error="synthetic error"),
        ])

        selected = [self._launch_random() for _ in profiles]

        self.assertEqual(selected[:2], ["high", "low"])
        self.assertEqual(set(selected[2:]), {"missing", "stale", "expired", "error"})
        self.assertEqual(len(set(selected)), len(profiles))
        self.assertEqual(self._launch_random(), "high")

    def test_new_eligible_profile_joins_current_cycle_before_any_repeat(self):
        self._create("codex", "first", "second")

        selected = {self._launch_random(), self._launch_random()}
        self.assertEqual(selected, {"first", "second"})

        self.store.create("newcomer", engine="codex")

        self.assertEqual(self._launch_random(), "newcomer")

    def test_unused_profile_precedes_a_used_profile_with_more_quota(self):
        used, unused = self._create("codex", "used-high", "unused-low")
        profile_rotation._atomic_write_json(
            profile_rotation.state_path(self.store, "codex"),
            {"version": 1, "engine": "codex", "used": [used.seq]},
        )
        self._write_snapshot([
            self._entry(used, 0.99),
            self._entry(unused, 0.05),
        ])

        plan = self._random_plan()

        self.assertEqual(plan.profile, unused.name)

    def test_unfiltered_rotation_covers_all_authenticated_engines(self):
        profiles = [
            self.store.create("agy-profile", engine="agy"),
            self.store.create("codex-profile", engine="codex"),
            self.store.create("grok-profile", engine="grok"),
            self.store.create("claude-profile", engine="claude"),
        ]
        self._write_snapshot([
            self._entry(profiles[0], 0.3),
            self._entry(profiles[1], 0.5),
            self._entry(profiles[2], 0.9),
            self._entry(profiles[3], 0.7, identity_verified=True),
        ])

        selected = [self._launch_random(engine=None) for _ in profiles]

        self.assertEqual(
            selected,
            ["grok-profile", "claude-profile", "codex-profile", "agy-profile"],
        )
        state_path = profile_rotation.state_path(self.store, None)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["engine"], "all")
        self.assertEqual(set(state["used"]), {profile.seq for profile in profiles})
        self.assertEqual(self._launch_random(engine=None), "grok-profile")

    def test_unfiltered_rotation_skips_unauthenticated_profiles(self):
        unauthenticated = self.store.create("unauthenticated", engine="agy")
        codex = self.store.create("authenticated", engine="codex")
        self._write_snapshot([
            self._entry(unauthenticated, 0.99),
            self._entry(codex, 0.1),
        ])

        selected = self._launch_random(
            engine=None,
            _auth_states={unauthenticated.name: "not-authenticated"},
        )

        self.assertEqual(selected, codex.name)
        state = json.loads(
            profile_rotation.state_path(self.store, None).read_text(encoding="utf-8")
        )
        self.assertEqual(state["used"], [codex.seq])

    def test_engine_and_all_engine_scopes_keep_independent_cycles(self):
        profile = self.store.create("codex-profile", engine="codex")

        self.assertEqual(self._launch_random(engine="codex"), profile.name)
        global_preview = self._random_plan(engine=None)

        self.assertEqual(global_preview.profile, profile.name)
        self.assertFalse(profile_rotation.state_path(self.store, None).exists())

    def test_unfiltered_retry_rebuilds_the_plan_for_the_new_engine(self):
        agy = self.store.create("agy-profile", engine="agy")
        codex = self.store.create("codex-profile", engine="codex")
        plan = self._random_plan(engine=None)
        self.assertEqual(plan.profile, agy.name)
        real_acquire = locks.acquire_lease
        acquired = []
        launched = []

        def acquire(store, name, patience_s=0.0, max_holders=None):
            acquired.append(name)
            if name == agy.name:
                raise locks.LeaseLimitError("synthetic session limit")
            return real_acquire(
                store, name, patience_s=patience_s, max_holders=max_holders
            )

        def launch(argv, env):
            launched.append((list(argv), dict(env)))
            return 0

        with mock.patch.object(
            account, "auth_state", side_effect=self._auth_state()
        ), mock.patch.object(
            locks, "acquire_lease", side_effect=acquire
        ), mock.patch.object(
            platforms, "launch_argv", side_effect=launch
        ):
            self.assertEqual(runner.run(plan, store=self.store), 0)

        self.assertEqual(acquired, [agy.name, codex.name])
        self.assertEqual(launched[0][0][0], str(self.executables["codex"]))
        self.assertIn("CODEX_HOME", launched[0][1])
        state = json.loads(
            profile_rotation.state_path(self.store, None).read_text(encoding="utf-8")
        )
        self.assertEqual(state["used"], [codex.seq])

    def test_rotation_reuses_one_quota_snapshot_across_retries(self):
        self._create("codex", "first", "second")
        plan = self._random_plan()
        real_acquire = locks.acquire_lease
        calls = []

        def acquire(store, name, patience_s=0.0, max_holders=None):
            calls.append(name)
            if len(calls) == 1:
                raise locks.LeaseLimitError("synthetic session limit")
            return real_acquire(
                store, name, patience_s=patience_s, max_holders=max_holders
            )

        with mock.patch.object(
            account, "auth_state", side_effect=self._auth_state()
        ), mock.patch.object(
            profile_rotation.usage_snapshot,
            "profile_quota_availabilities",
            wraps=profile_rotation.usage_snapshot.profile_quota_availabilities,
        ) as quota_read, mock.patch.object(
            locks, "acquire_lease", side_effect=acquire
        ), mock.patch.object(
            platforms, "launch_argv", side_effect=self._record_launch
        ):
            self.assertEqual(runner.run(plan, store=self.store), 0)

        self.assertEqual(quota_read.call_count, 1)

    def test_all_engine_rotation_lock_coordinates_with_engine_scopes(self):
        with profile_rotation.Rotation(self.store, None):
            with self.assertRaisesRegex(StoreError, "codex profile rotation is busy"):
                profile_rotation.Rotation(self.store, "codex").__enter__()

        with profile_rotation.Rotation(self.store, "codex") as codex_rotation, \
                profile_rotation.Rotation(self.store, "grok") as grok_rotation:
            self.assertNotEqual(codex_rotation.scope, grok_rotation.scope)

    def test_keychain_owner_does_not_preempt_other_engine_candidates(self):
        agy = self.store.create("agy-first", engine="agy")
        owner = self.store.create("agy-owner", engine="agy")
        codex = self.store.create("codex-profile", engine="codex")
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 2
        self.store.save_config(config)
        plan = self._random_plan(engine=None)
        self.assertEqual(plan.profile, agy.name)
        launched = []

        @contextlib.contextmanager
        def owned_slot(*_args, **_kwargs):
            raise keychain.KeychainBusyError("owned slot", owner=owner.name)
            yield

        def launch(argv, env):
            launched.append((list(argv), dict(env)))
            return 0

        def guard(_store, name, **_kwargs):
            return owned_slot() if name == agy.name else contextlib.nullcontext()

        with mock.patch.object(
            account, "auth_state", side_effect=self._auth_state()
        ), mock.patch.object(
            locks, "is_locked", side_effect=lambda _store, name: name == owner.name
        ), mock.patch.object(
            locks,
            "lease_holders",
            side_effect=lambda _store, name: [object()] if name == owner.name else None,
        ), mock.patch.object(
            keychain, "launch_guard", side_effect=guard
        ), mock.patch.object(
            platforms, "launch_argv", side_effect=launch
        ):
            self.assertEqual(runner.run(plan, store=self.store), 0)

        self.assertEqual(launched[0][0][0], str(self.executables["codex"]))
        self.assertEqual(launched[0][1][resolver.PROFILE_ENV], codex.name)

    def test_agy_random_prefers_a_fresh_profile_then_joins_a_used_keychain_owner(self):
        """Rotation order is untouched: the unused profile is tried first
        and the used owner is never re-picked mid-cycle. When the owner's
        live sessions block the fresh pick and the engine scope is
        exhausted, the launch joins the owner through explicit-selection
        semantics and the cycle state stays exactly as it was."""
        fresh = self.store.create("agy-fresh", engine="agy")
        owner = self.store.create("agy-owner", engine="agy")
        profile_rotation._atomic_write_json(
            profile_rotation.state_path(self.store, "agy"),
            {"version": 1, "engine": "agy", "used": [owner.seq]},
        )
        plan = self._random_plan(engine="agy", force=True)
        self.assertEqual(plan.profile, fresh.name)

        @contextlib.contextmanager
        def keychain_guard(_store, name, **_kwargs):
            if name == fresh.name:
                raise keychain.KeychainBusyError(
                    f"shared Antigravity keychain slot belongs to {owner.name!r}",
                    owner=owner.name,
                )
            yield

        with mock.patch.object(
            account, "auth_state", side_effect=self._auth_state()
        ), mock.patch.object(
            keychain, "launch_guard", side_effect=keychain_guard
        ), mock.patch.object(
            platforms, "launch_argv", side_effect=self._record_launch
        ), mock.patch.object(
            platforms, "run_wait", side_effect=self._record_launch
        ):
            rc = runner.run(plan, store=self.store)

        self.assertEqual(rc, 0)
        self.assertEqual(self.launched, [owner.name])
        rotation_state = json.loads(
            profile_rotation.state_path(self.store, "agy").read_text(encoding="utf-8")
        )
        self.assertEqual(rotation_state["used"], [owner.seq])

    def test_equal_quota_uses_lru_then_sequence_tie_break(self):
        earlier_sequence, less_recent = self._create("codex", "sequence-first", "lru-first")
        earlier_sequence.last_used = "2026-10-01T12:00:00+00:00"
        less_recent.last_used = "2026-09-01T12:00:00+00:00"
        self.store.save(earlier_sequence)
        self.store.save(less_recent)
        self._write_snapshot([
            self._entry(earlier_sequence, 0.7),
            self._entry(less_recent, 0.7),
        ])

        self.assertEqual(self._random_plan().profile, "lru-first")

        less_recent.last_used = earlier_sequence.last_used
        self.store.save(less_recent)

        self.assertEqual(self._random_plan().profile, "sequence-first")

    def test_equal_quota_prefers_free_profile_before_lru(self):
        busy_older, free_newer = self._create("codex", "busy-older", "free-newer")
        busy_older.last_used = "2025-09-01T12:00:00+00:00"
        free_newer.last_used = "2026-09-01T12:00:00+00:00"
        self.store.save(busy_older)
        self.store.save(free_newer)
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 2
        self.store.save_config(config)
        self._write_snapshot([
            self._entry(busy_older, 0.7),
            self._entry(free_newer, 0.7),
        ])

        with mock.patch.object(
            locks,
            "is_locked",
            side_effect=lambda _store, name: name == "busy-older",
        ), mock.patch.object(
            locks, "lease_holders", return_value=[object()]
        ), mock.patch.object(account, "auth_state", side_effect=self._auth_state()):
            selected = resolver.pick_free_profile(
                self.store, engine="codex", cwd=self.project
            )

        self.assertEqual(selected.name, "free-newer")
        self.assertIn("free profile", selected.reason)

    def test_codex_and_grok_keep_separate_cycles_and_ignore_foreign_quota(self):
        codex_profiles = self._create("codex", "c1", "c2")
        grok_profiles = self._create("grok", "g1", "g2")
        self._write_snapshot([
            self._entry(codex_profiles[0], 0.99, engine="grok"),
            self._entry(codex_profiles[1], 0.6),
            self._entry(grok_profiles[0], 0.8),
            self._entry(grok_profiles[1], 0.2),
        ])

        selected = [
            self._launch_random("codex"),
            self._launch_random("grok"),
            self._launch_random("codex"),
            self._launch_random("grok"),
        ]

        self.assertEqual(selected, ["c2", "g1", "c1", "g2"])
        self.assertIsNone(
            usage_snapshot.profile_quota_availability(
                self.store, codex_profiles[0], now=self.now
            )
        )

    def test_random_ignores_project_pin_while_explicit_profile_stays_unchanged(self):
        profiles = self._create("codex", "pinned", "ranked")
        (self.project / ".agydra").write_text("pinned", encoding="utf-8")
        self._write_snapshot([
            self._entry(profiles[0], 0.2),
            self._entry(profiles[1], 0.9),
        ])

        with mock.patch.object(account, "auth_state", side_effect=self._auth_state()):
            random_plan = self._random_plan()
            explicit_plan = runner.build_plan(
                self.store,
                ["chat"],
                random_pick=True,
                flag_ref="pinned",
                engine="codex",
                cwd=self.project,
            )

        self.assertEqual(random_plan.profile, "ranked")
        self.assertEqual(explicit_plan.profile, "pinned")
        self.assertFalse(explicit_plan.random_pick)
        self.assertIn("flag --profile=pinned", explicit_plan.reason)

    def test_force_bypasses_only_session_cap_and_runner_passes_no_limit(self):
        profiles = self._create("codex", "saturated", "saturated-next", "unauthenticated")
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 1
        self.store.save_config(config)
        self._write_snapshot([
            self._entry(profiles[0], 0.95),
            self._entry(profiles[1], 0.5),
            self._entry(profiles[2], 1.0),
        ])
        auth_states = {"unauthenticated": "not-authenticated"}

        busy_names = {"saturated", "saturated-next"}
        with mock.patch.object(locks, "is_locked", side_effect=lambda _store, name: name in busy_names), \
                mock.patch.object(locks, "lease_holders", return_value=[object()]):
            with mock.patch.object(account, "auth_state", side_effect=self._auth_state(auth_states)):
                with self.assertRaisesRegex(StoreError, "limit of 1"):
                    resolver.pick_free_profile(
                        self.store, engine="codex", cwd=self.project
                    )
                forced = resolver.pick_free_profile(
                    self.store, force=True, engine="codex", cwd=self.project
                )
        with mock.patch.object(account, "auth_state", side_effect=self._auth_state(auth_states)):
            explicit = runner.build_plan(
                self.store,
                ["chat"],
                flag_ref="unauthenticated",
                engine="codex",
                cwd=self.project,
                force=True,
            )

        self.assertEqual(forced.name, "saturated")
        self.assertEqual(explicit.profile, "unauthenticated")
        self.assertFalse(explicit.random_pick)

        forced_plan = self._random_plan(force=True)
        acquire_calls = []

        def acquire(_store, _name, patience_s=0.0, max_holders=None):
            acquire_calls.append(max_holders)
            return 0

        with mock.patch.object(account, "auth_state", side_effect=self._auth_state(auth_states)), \
                mock.patch.object(locks, "is_locked", side_effect=lambda _store, name: name in busy_names), \
                mock.patch.object(locks, "acquire_lease", side_effect=acquire), \
                mock.patch.object(locks, "release_lease"), \
                mock.patch.object(platforms, "launch_argv", side_effect=self._record_launch):
            self.assertEqual(runner.run(forced_plan, store=self.store), 0)

        self.assertEqual(acquire_calls, [None])
        self.assertEqual(self.launched[-1], "saturated")

    def test_unused_profiles_prefer_free_sessions_before_saved_quota(self):
        capped, busy_under_limit, free = self._create(
            "codex", "capped-high", "busy-high", "free-low"
        )
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 2
        self.store.save_config(config)
        self._write_snapshot([
            self._entry(capped, 0.99),
            self._entry(busy_under_limit, 0.9),
            self._entry(free, 0.3),
        ])

        busy_names = {"capped-high", "busy-high"}

        def holders(_store, name):
            return [object()] * (2 if name == "capped-high" else 1)

        with mock.patch.object(
            locks, "is_locked", side_effect=lambda _store, name: name in busy_names
        ), mock.patch.object(locks, "lease_holders", side_effect=holders), mock.patch.object(
            account, "auth_state", side_effect=self._auth_state()
        ):
            selected = resolver.pick_free_profile(
                self.store, engine="codex", cwd=self.project
            )
            forced = resolver.pick_free_profile(
                self.store, force=True, engine="codex", cwd=self.project
            )

        self.assertEqual(selected.name, "free-low")
        self.assertIn("free profile", selected.reason)
        self.assertEqual(forced.name, "free-low")
        self.assertIn("free profile", forced.reason)

    def test_repeated_profiles_prefer_saved_quota_before_session_state(self):
        high, low = self._create("codex", "high-quota", "low-quota")
        state_path = profile_rotation.state_path(self.store, "codex")
        state_path.write_text(
            json.dumps({
                "version": 1,
                "engine": "codex",
                "used": [high.seq, low.seq],
            }),
            encoding="utf-8",
        )
        config = self.store.load_config()
        config.settings["max_sessions_per_profile"] = 2
        self.store.save_config(config)
        self._write_snapshot([
            self._entry(high, 0.95),
            self._entry(low, 0.25),
        ])

        with mock.patch.object(
            account, "auth_state", side_effect=self._auth_state()
        ), mock.patch.object(
            locks, "is_locked", side_effect=lambda _store, name: name == high.name
        ), mock.patch.object(
            locks, "lease_holders", return_value=[object()]
        ):
            selected = resolver.pick_free_profile(self.store, engine="codex")

        self.assertEqual(selected.name, high.name)
        self.assertIn("joining busy profile", selected.reason)

    def test_preview_and_dry_run_do_not_consume_persistent_cycle(self):
        self._create("codex", "first", "second")

        preview = self._random_plan()
        self.assertEqual(runner.run(preview, store=self.store, dry_run=True), 0)
        state = profile_rotation.state_path(self.store, "codex")
        self.assertFalse(state.exists())

        next_preview = self._random_plan()
        self.assertEqual(next_preview.profile, preview.profile)
        selected = self._launch_random()
        self.assertEqual(selected, preview.profile)
        self.assertTrue(state.exists())
        self.assertEqual(
            json.loads(state.read_text(encoding="utf-8"))["used"],
            [self.store.get(selected).seq],
        )

        reopened = Store(self.store_root)
        with mock.patch.object(account, "auth_state", side_effect=self._auth_state()), \
                mock.patch.object(platforms, "launch_argv", side_effect=self._record_launch):
            next_plan = runner.build_plan(
                reopened,
                ["chat"],
                random_pick=True,
                engine="codex",
                cwd=self.project,
            )
            self.assertEqual(runner.run(next_plan, store=reopened), 0)

        self.assertNotEqual(self.launched[-1], selected)

    def test_legacy_zero_sequence_uses_selector_fallback_without_migration(self):
        legacy, peer, modern = self._create("codex", "legacy", "legacy-peer", "modern")
        other_engine = self.store.create("legacy-grok", engine="grok")
        for profile in (legacy, peer, other_engine):
            metadata_path = self.store.profile_meta_path(profile.name)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["seq"] = 0
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        legacy, peer = self.store.get("legacy"), self.store.get("legacy-peer")
        other_engine = self.store.get("legacy-grok")
        self.assertEqual((legacy.seq, peer.seq, other_engine.seq), (0, 0, 0))
        self._write_snapshot([
            self._entry(legacy, 0.8),
            self._entry(peer, 0.7),
            self._entry(other_engine, 0.99),
            self._entry(modern, 0.6),
        ])

        self.assertEqual(
            usage_snapshot.profile_quota_availability(self.store, legacy, now=self.now),
            0.8,
        )
        self.assertEqual(
            usage_snapshot.profile_quota_availability(self.store, peer, now=self.now),
            0.7,
        )
        self.assertEqual(self._launch_random(), "legacy")

        state = json.loads(
            profile_rotation.state_path(self.store, "codex").read_text(encoding="utf-8")
        )
        self.assertIn("legacy:codex:legacy", state["used"])
        self.assertEqual(self.store.get("legacy").seq, 0)
        self.assertEqual(self._launch_random(), "legacy-peer")
        self.assertEqual(self._launch_random(), "modern")
        self.assertGreater(modern.seq, 0)

    def test_agy_and_claude_keep_independent_random_cycles(self):
        agy_low, agy_high = self._create("agy", "agy-low", "agy-high")
        claude_low, claude_high = self._create("claude", "claude-low", "claude-high")
        self._write_snapshot([
            self._entry(agy_low, 0.2),
            self._entry(agy_high, 0.9),
            self._entry(claude_low, 0.3, identity_verified=True),
            self._entry(claude_high, 0.8, identity_verified=True),
        ])

        selected = [
            self._launch_random("agy"),
            self._launch_random("claude"),
            self._launch_random("agy"),
            self._launch_random("claude"),
        ]

        self.assertEqual(
            selected,
            ["agy-high", "claude-high", "agy-low", "claude-low"],
        )

    def test_claude_quota_without_verified_identity_is_unknown(self):
        profile = self.store.create("claude-profile", engine="claude")
        self._write_snapshot([
            self._entry(profile, 0.9, identity_verified=False),
        ])

        self.assertIsNone(
            usage_snapshot.profile_quota_availability(
                self.store, profile, now=self.now
            )
        )

        self._write_snapshot([
            self._entry(profile, 0.9, identity_verified=True),
        ])
        self.assertEqual(
            usage_snapshot.profile_quota_availability(
                self.store, profile, now=self.now
            ),
            0.9,
        )


class TestBlockedRotationEngine(BaseCase):
    def test_busy_keychain_owner_blocks_its_engine_for_random_retries(self):
        store = Store(self.store_root)
        alpha = store.create("alpha", engine="agy")
        beta = store.create("beta", engine="agy")
        with profile_rotation.Rotation(store, "agy") as rotation:
            rotation.blocked_engines.add("agy")
            with self.assertRaisesRegex(
                profile_rotation.NoEligibleProfileError,
                "no profile remains in the rotation scope",
            ):
                rotation.select([alpha, beta])


if __name__ == "__main__":
    unittest.main()
