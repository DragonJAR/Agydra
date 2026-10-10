"""Doctor end-to-end: healthy store → 0; missing binary/corrupt metadata → 1/warn."""
import argparse
import base64
import contextlib
import io
import json
import os
import stat
import sys
import tokenize
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import doctor
import isolation
import keychain
import platforms
import ui
from store import Store

from conftest import BaseCase, _make_jwt


class TestDoctor(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_runtime_sources_have_no_comments(self):
        source_dir = Path(__file__).resolve().parents[1]
        for path in sorted(source_dir.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            comments = [
                (token.start[0], token.string.strip())
                for token in tokenize.generate_tokens(io.StringIO(source).readline)
                if token.type == tokenize.COMMENT
                and not (token.start[0] == 1 and token.string.startswith("#!"))
            ]
            with self.subTest(filename=path.name):
                self.assertEqual(comments, [])

    def test_codex_schema_canary_accepts_daemonless_config_toml(self):
        """A Codex profile whose only layout file is the config.toml isolation
        writes (daemon_auto_start = false) must pass the schema canary.
        config.json is not the Codex config."""
        self.store.create("cx", engine="codex")
        data = self.store.profile_data_dir("cx", engine="codex")
        (data / "config.toml").write_text(
            "[features]\ndaemon_auto_start = false\n",
            encoding="utf-8",
        )
        self.assertFalse((data / "config.json").exists())
        self.assertFalse((data / "auth.json").exists())
        status, message = doctor._check_schema_canary(self.store, self._ctx())
        self.assertEqual(status, doctor.OK, message)

    def test_healthy_store_returns_zero(self):
        self.store.create("work")
        (self.store.profile_data_dir("work") / "antigravity-cli").mkdir()
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)

    def test_missing_binary_returns_one(self):
        self.store.create("work")
        os.environ["AGYDRA_AGY_BIN"] = str(self.bin_dir / "missing-agy")
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 1)

    def test_corrupt_metadata_does_not_break_run(self):
        self.store.create("work")
        corrupt = self.store.profiles_dir / "broken"
        (corrupt / "data").mkdir(parents=True)
        (corrupt / "profile.json").write_text("{this is not json", encoding="utf-8")
        names = self.store.names()
        self.assertEqual(names, ["work"])
        self.assertIn("broken", self.store.unreadable_profiles())
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)

    def test_corrupt_metadata_warning_points_to_delete(self):
        """The unreadable-metadata WARN must tell the user how to recover
        (``agydra delete <name>``) instead of leaving them stuck between a
        resolve_ref that says 'unknown' and a create that says 'already
        exists'."""
        self.store.create("work")
        corrupt = self.store.profiles_dir / "broken"
        (corrupt / "data").mkdir(parents=True)
        (corrupt / "profile.json").write_text("{this is not json", encoding="utf-8")
        ctx = doctor._DoctorContext(
            scan=self.store.scan(),
            names=self.store.names(),
        )
        status, message = doctor._check_profiles(self.store, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertIn("agydra delete", message)

    def test_empty_store_returns_zero(self):
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)

    def test_fake_binary_is_executable_and_resolvable(self):
        self.assertTrue(Path(self.agy_bin).exists())
        st = Path(self.agy_bin).stat()
        self.assertTrue(st.st_mode & stat.S_IXUSR)

    def test_locks_check_reports_live_session(self):
        import locks

        self.store.create("work")
        handle = locks.try_lock(self.store, "work")
        try:
            exit_code = doctor.run_checks(self.store)
        finally:
            handle.release()
        self.assertEqual(exit_code, 0)

    def test_locks_check_passes_when_free(self):
        self.store.create("work")
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)

    def _secret_for(self, email):
        jwt = _make_jwt({"email": email})
        payload = json.dumps({
            "token": {"access_token": "a", "refresh_token": "r"},
            "auth_method": "consumer",
            "id_token": jwt,
        }).encode("utf-8")
        return b"go-keyring-base64:" + base64.b64encode(payload)

    def _hermetic_keychain(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(keychain, "supported", return_value=True))
        stack.enter_context(mock.patch.object(keychain, "_ensure_target_keychain", return_value=None))
        stack.enter_context(mock.patch.object(keychain, "read_slot", return_value=None))
        stack.enter_context(mock.patch.object(keychain, "orphan_slots", return_value=[]))
        return stack

    def _ctx(self):
        return doctor._DoctorContext(
            scan=self.store.scan(), names=self.store.names(),
        )

    def test_bootstrap_check_accepts_pip_managed_install_without_inspecting_shim(self):
        import bootstrap

        fake_root = self._tmp / "pip-managed-install"
        state = {
            "venv": True,
            "console": True,
            "shim_ok": True,
            "shim_state": "pip-managed",
            "on_path": True,
            "installed": True,
        }
        with mock.patch.object(bootstrap, "project_root", return_value=fake_root), \
                mock.patch.object(bootstrap, "check_state", return_value=state) as check_state, \
                mock.patch.object(
                    bootstrap,
                    "shim_path",
                    side_effect=AssertionError("pip-managed installs have no Agydra shim"),
                ) as shim_path, \
                mock.patch.object(doctor.platforms, "is_windows", return_value=False):
            status, message = doctor._check_bootstrap(None, None)

        self.assertEqual(
            (status, message),
            (
                doctor.OK,
                "install: pip-managed package (pip/pipx; no Agydra shim expected)",
            ),
        )
        check_state.assert_called_once_with(fake_root)
        shim_path.assert_not_called()
        self.assertFalse(fake_root.exists())

    def test_bootstrap_check_warns_when_the_editable_install_is_behind_the_repo(self):
        import bootstrap

        fake_root = self._tmp / "source-checkout"
        fake_root.mkdir(parents=True, exist_ok=True)
        state = {
            "venv": True,
            "console": True,
            "shim_ok": True,
            "shim_state": "ok",
            "on_path": True,
        }
        for unimportable, expected_status in (([], doctor.OK), (["i18n"], doctor.WARN)):
            with mock.patch.object(bootstrap, "project_root", return_value=fake_root), \
                    mock.patch.object(bootstrap, "check_state", return_value=dict(state)), \
                    mock.patch.object(
                        bootstrap, "unimportable_modules", return_value=unimportable
                    ) as drift, \
                    mock.patch.object(doctor.platforms, "is_windows", return_value=False):
                status, message = doctor._check_bootstrap(None, None)

            self.assertEqual(status, expected_status)
            drift.assert_called_once()
            if unimportable:
                # The message has to name the missing modules AND the fix:
                # this is the only place the failure is visible before the
                # console script dies with a bare ModuleNotFoundError.
                self.assertIn("editable install is stale", message)
                self.assertIn("i18n", message)
                self.assertIn("agydra setup", message)
            else:
                self.assertNotIn("editable install is stale", message)

    def test_keychain_check_flags_identity_mismatch(self):
        self.store.create("alpha")
        profile = self.store.get("alpha")
        profile.email = "alpha@example.com"
        self.store.save(profile)
        keychain.save_profile_slot(
            self.store, "alpha", self._secret_for("mallory@example.com")
        )

        with mock.patch.object(keychain, "supported", return_value=True):
            status, message = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn("alpha", message)
        self.assertIn("mallory@example.com", message)
        self.assertIn("alpha@example.com", message)

    def test_keychain_check_ok_when_identity_matches(self):
        self.store.create("alpha")
        profile = self.store.get("alpha")
        profile.email = "alpha@example.com"
        self.store.save(profile)
        keychain.save_profile_slot(
            self.store, "alpha", self._secret_for("alpha@example.com")
        )

        with self._hermetic_keychain():
            status, _ = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.OK)

    def test_keychain_check_flags_undecodable_secret_as_such(self):
        self.store.create("alpha")
        profile = self.store.get("alpha")
        profile.email = "alpha@example.com"
        self.store.save(profile)
        keychain.save_profile_slot(self.store, "alpha", b"not go-keyring at all")

        with mock.patch.object(keychain, "supported", return_value=True):
            status, message = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn("undecodable", message)

    def test_keychain_check_skips_profiles_with_no_known_identity(self):
        """A profile whose email was never synced has nothing to compare
        the secret against -- it must not be flagged."""
        self.store.create("alpha")
        keychain.save_profile_slot(
            self.store, "alpha", self._secret_for("whoever@example.com")
        )

        with self._hermetic_keychain():
            status, _ = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.OK)

    def test_keychain_check_warns_on_orphan_slots(self):
        """Orphaned keychain-database slots (profile deleted by hand) must
        be visible on a plain ``agydra doctor`` run, not only in --fix's
        preview -- same proactive-WARN pattern ``_check_orphans`` already
        uses for file-based orphans."""
        self.store.create("alpha")

        def fake_orphan(store, known_names):
            return ["ghost"]

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(keychain, "orphan_slots", fake_orphan):
            status, message = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn("ghost", message)
        self.assertIn("doctor --fix", message)

    def test_keychain_check_warns_when_swap_lock_is_held(self):
        """A wedged swap.lock (leftover or pre-upgrade session holding it
        for its whole lifetime) must be visible on a plain ``agydra
        doctor`` run, before the next launch fails with the shared-slot
        busy error."""
        import locks

        if keychain.fcntl is None:
            self.skipTest("swap.lock contention requires POSIX fcntl")
        lock_path = keychain.swap_lock_path(self.store)
        platforms.ensure_dir(lock_path.parent)
        holder = locks.try_lock_path(lock_path, "test holder")
        self.assertIsNotNone(holder)
        try:
            with mock.patch.object(keychain, "supported", return_value=True):
                status, message = doctor._check_keychain(self.store, self._ctx())
        finally:
            holder.release()
        self.assertEqual(status, doctor.WARN)
        self.assertIn("swap lock is held", message)
        self.assertIn("lsof", message)

    def test_keychain_check_warns_when_swap_lock_probe_raises(self):
        lock_path = keychain.swap_lock_path(self.store)
        platforms.ensure_dir(lock_path.parent)
        lock_path.touch()
        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    doctor.locks, "try_lock_path", side_effect=doctor.locks.LockError("denied")
                ):
            status, message = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn("not inspectable", message)

    def test_keychain_check_reports_bridge_disabled_via_env(self):
        """When AGYDRA_NO_KEYCHAIN is active, doctor must report that the bridge
        was disabled via the environment variable rather than falsely claiming
        `security` binary was missing."""
        with mock.patch.dict(os.environ, {"AGYDRA_NO_KEYCHAIN": "1"}):
            status, message = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn("bridge disabled via AGYDRA_NO_KEYCHAIN", message)
        self.assertNotIn("`security` not found", message)

    def test_keychain_check_warns_on_setup_skipped_marker(self):
        """When .setup-skipped marker exists, doctor must surface an explanatory
        WARN explaining why it exists and instructing how to retry."""
        marker = keychain._slots_dir(self.store) / getattr(keychain, "_SKIP_MARKER_NAME", ".setup-skipped")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        with mock.patch.object(keychain, "supported", return_value=True):
            status, message = doctor._check_keychain(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn(".setup-skipped", message)
        self.assertIn("delete", message.lower())

    def test_apply_fixes_purges_orphan_keychain_slots_with_fresh_names(self):
        """Regression: ``_apply_fixes``'s keychain-orphan purge must re-derive
        the current profile list at purge time, never reuse the STALE
        pre-confirmation ``ctx.names`` snapshot -- the same fresh-scan guard
        ``orphans.remove_orphans`` already applies to its file-based purge
        (see ``test_orphans.py::
        test_skips_an_overlay_and_secret_recreated_between_detect_and_fix``).

        Scenario: "ghost" is flagged as an orphaned keychain slot before it
        exists as a profile (``ctx`` captured then). During the (arbitrarily
        long) confirmation pause a real profile named "ghost" is created,
        with its own genuine keychain slot. ``_apply_fixes`` must not purge
        that brand-new, now-legitimate slot just because it wasn't in the
        stale ``ctx.names`` it was built from."""
        ctx = self._ctx()
        self.assertNotIn("ghost", ctx.names)

        # Simulate the pause: "ghost" gets created for real, with its own
        # genuine keychain slot -- after ``ctx`` was already captured.
        self.store.create("ghost")
        keychain.save_profile_slot(self.store, "ghost", b"real-secret")

        deleted = []

        def fake_orphan_slots(_store, known_names, keychain_path=None):
            # The system keychain always has a "gemini/agydra/ghost"
            # service; it is only reported as orphaned when "ghost" is
            # absent from the names it is called with.
            return sorted({"ghost"} - set(known_names))

        def fake_delete_slot(service, keychain_path=None):
            deleted.append(service)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    doctor.keychain, "_ensure_target_keychain", return_value=None
                ), \
                mock.patch.object(doctor.keychain, "orphan_slots", fake_orphan_slots), \
                mock.patch.object(doctor.keychain, "delete_slot", fake_delete_slot):
            doctor._apply_fixes(self.store, ctx)

        self.assertNotIn(
            "gemini/agydra/ghost", deleted,
            "the freshly-created profile's own keychain slot must survive "
            "a purge driven by a stale pre-confirmation ctx",
        )
        self.assertEqual(
            keychain.load_profile_slot(self.store, "ghost"), b"real-secret",
            "ghost's own file-backed slot backup must be untouched",
        )

    def test_apply_fixes_detects_file_orphans_deleted_after_ctx_was_built(self):
        """Regression: ``_fix_orphans`` must scan against the SAME fresh
        ``current_names`` snapshot ``_apply_fixes`` already computes for the
        keychain-orphan purge, never the stale pre-confirmation ``ctx.names``
        it used to close over.

        Scenario: "foo" is alive (and thus in ``ctx.names``) when ``ctx`` is
        captured. During the (arbitrarily long) confirmation pause, "foo"'s
        profile directory is removed by hand (not through ``agydra
        delete``), leaving its overlay and keychain-secret backup genuinely
        orphaned. A stale ``ctx.names`` still lists "foo" as known, so
        scanning against it would miss these newly-orphaned artifacts in
        this very ``--fix`` pass -- under-reported until the next `doctor`
        run, contradicting the reason the file-based orphan purge exists."""
        self.store.create("foo")
        keychain.save_profile_slot(self.store, "foo", b"foo-secret")
        (self.store.overlays_dir / "foo").mkdir(parents=True, exist_ok=True)

        ctx = self._ctx()
        self.assertIn("foo", ctx.names)

        # Simulate the pause: "foo"'s profile directory disappears by hand,
        # leaving its overlay/keychain-secret backup truly orphaned.
        import shutil

        shutil.rmtree(self.store.profile_dir("foo"))
        self.assertNotIn("foo", self.store.names())

        with mock.patch.object(keychain, "supported", return_value=False):
            doctor._apply_fixes(self.store, ctx)

        self.assertFalse(
            (self.store.overlays_dir / "foo").exists(),
            "foo's overlay must be recognized and purged as an orphan in "
            "this same --fix pass, not left for a later run",
        )
        self.assertIsNone(
            keychain.load_profile_slot(self.store, "foo"),
            "foo's keychain-secret backup must be purged alongside its "
            "overlay",
        )


class TestClaudeIsolationCheck(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("cc", engine="claude")
        isolation.build_overlay("cc", self.store.claude_config_dir("cc"), self.store.root, engine="claude")

    def _check(self):
        return doctor._check_isolation(
            self.store,
            doctor._DoctorContext(scan=self.store.scan(), names=self.store.names()),
        )

    def test_plain_config_directory_is_ok(self):
        status, message = self._check()
        self.assertEqual(status, doctor.OK)
        self.assertIn("claude", message)

    def test_symlinked_config_directory_fails_via_shared_validator(self):
        config = self.store.claude_config_dir("cc")
        target = self.store.root / "elsewhere"
        target.mkdir()
        config.rmdir()
        try:
            config.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        status, message = self._check()
        self.assertEqual(status, doctor.FAIL)
        self.assertIn("cc:", message)
        self.assertIn("symlink", message)

    def test_alias_of_real_claude_directory_fails(self):
        real = platforms.claude_data_dir()
        real.mkdir(parents=True)
        config = self.store.claude_config_dir("cc")
        config.rmdir()
        try:
            config.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        status, message = self._check()
        self.assertEqual(status, doctor.FAIL)
        self.assertIn("cc:", message)


class TestIsolationRecovery(BaseCase):
    """``_check_isolation`` must distinguish the recoverable real-dir case
    (WARN: run doctor --fix) from a real mis-pointed symlink (FAIL). The
    real-dir case is the alpha breakage."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        (self.store.profile_data_dir("alpha") / "antigravity-cli").mkdir()
        isolation.build_overlay("alpha", self.store.profile_data_dir("alpha"), self.store.root)
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        link.unlink()
        link.mkdir()
        (link / "antigravity-cli").mkdir()
        (link / "antigravity-cli" / "token.json").write_text("{}", encoding="utf-8")
        self._ctx = lambda: doctor._DoctorContext(
            scan=self.store.scan(),
            names=self.store.names(),
        )

    def test_real_dir_is_warn_with_fix_pointer(self):
        status, message = doctor._check_isolation(self.store, self._ctx())
        self.assertEqual(status, doctor.WARN)
        self.assertIn("doctor --fix", message)
        self.assertIn("alpha", message)

    def test_fix_migrates_real_dir_overlay_into_profile_store(self):
        """``doctor --fix`` recovers the alpha-class breakage: migrates the
        real-dir .gemini into the profile data dir and relinks."""
        import locks

        data_dir = self.store.profile_data_dir("alpha")
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        original_try_lock = locks.try_mutation_lock
        original_migrate = isolation.migrate_real_dir_to_store
        original_build_overlay = isolation.build_overlay
        lock_checks = []

        def inspect_migrate(real_dir, target_dir):
            handle = original_try_lock(self.store, "alpha")
            lock_checks.append(handle is None)
            if handle is not None:
                handle.release()
            return original_migrate(real_dir, target_dir)

        def inspect_relink(*args, **kwargs):
            handle = original_try_lock(self.store, "alpha")
            lock_checks.append(handle is None)
            if handle is not None:
                handle.release()
            return original_build_overlay(*args, **kwargs)

        # pre-conditions: data dir empty of the marker file, overlay has it
        self.assertFalse((data_dir / "antigravity-cli" / "token.json").exists())
        self.assertTrue((link / "antigravity-cli" / "token.json").exists())

        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(
                    isolation, "migrate_real_dir_to_store", side_effect=inspect_migrate
                ), \
                mock.patch.object(
                    isolation, "build_overlay", side_effect=inspect_relink
                ):
            rc = doctor.run_checks(self.store, fix=True)

        self.assertEqual(rc, 0)
        self.assertEqual(lock_checks, [True, True])
        self.assertTrue((data_dir / "antigravity-cli" / "token.json").exists(),
                        "real-dir contents must move into profile data dir")
        self.assertTrue(isolation._is_link(link),
                        "build_overlay must relink after migrate")

    def test_fix_is_silent_for_healthy_symlinked_overlay(self):
        """A healthy overlay whose data dir is properly symlinked into the
        profile store is not a migration candidate: ``_apply_fixes`` must
        skip it silently instead of warning that the owner cannot be
        established."""
        import shutil
        from unittest import mock

        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        shutil.rmtree(link)
        isolation.build_overlay("alpha", self.store.profile_data_dir("alpha"), self.store.root)
        self.assertTrue(isolation._is_link(link))

        output = io.StringIO()
        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(isolation, "migrate_real_dir_to_store") as migrate, \
                contextlib.redirect_stderr(output):
            doctor._apply_fixes(self.store, self._ctx())

        migrate.assert_not_called()
        self.assertNotIn("skipping overlay recovery", output.getvalue())

    def test_fix_skips_overlay_recovery_when_profile_lock_is_held(self):
        import locks
        from unittest import mock

        data_dir = self.store.profile_data_dir("alpha")
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        source_token = link / "antigravity-cli" / "token.json"
        handle = locks.try_lock(self.store, "alpha")
        output = io.StringIO()
        try:
            with mock.patch.object(keychain, "supported", return_value=False), \
                    mock.patch.object(isolation, "migrate_real_dir_to_store") as migrate, \
                    contextlib.redirect_stderr(output):
                doctor._apply_fixes(self.store, self._ctx())
        finally:
            handle.release()

        migrate.assert_not_called()
        self.assertTrue(source_token.is_file())
        self.assertFalse((data_dir / "antigravity-cli" / "token.json").exists())
        self.assertIn("profile lock is held", output.getvalue())

    def test_fix_skips_overlay_recovery_when_a_leased_session_is_live(self):
        """A session registered in the lease registry blocks the overlay
        migration even though its flock is free."""
        import locks
        from unittest import mock

        data_dir = self.store.profile_data_dir("alpha")
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        source_token = link / "antigravity-cli" / "token.json"
        locks.acquire_lease(self.store, "alpha")
        self.addCleanup(locks.release_lease, self.store, "alpha")
        self.assertFalse(locks._flock_probe_locked(self.store, "alpha"))
        registry = locks.lock_path(self.store, "alpha").read_bytes()
        output = io.StringIO()
        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(isolation, "migrate_real_dir_to_store") as migrate, \
                contextlib.redirect_stderr(output):
            doctor._apply_fixes(self.store, self._ctx())

        migrate.assert_not_called()
        self.assertTrue(source_token.is_file())
        self.assertFalse((data_dir / "antigravity-cli" / "token.json").exists())
        self.assertIn("profile lock is held", output.getvalue())
        self.assertEqual(locks.lock_path(self.store, "alpha").read_bytes(), registry)

    def test_fix_skips_overlay_recovery_when_profile_owner_changed(self):
        import locks
        from unittest import mock

        data_dir = self.store.profile_data_dir("alpha")
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        source_token = link / "antigravity-cli" / "token.json"
        original_try_lock = locks.try_mutation_lock
        changed = []

        def change_profile_owner(store, name):
            handle = original_try_lock(store, name)
            if name == "alpha" and handle is not None and not changed:
                profile = store.get(name)
                profile.seq += 1
                store.save(profile)
                changed.append(name)
            return handle

        output = io.StringIO()
        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(locks, "try_mutation_lock", side_effect=change_profile_owner), \
                mock.patch.object(isolation, "migrate_real_dir_to_store") as migrate, \
                contextlib.redirect_stderr(output):
            doctor._apply_fixes(self.store, self._ctx())

        migrate.assert_not_called()
        self.assertEqual(changed, ["alpha"])
        self.assertTrue(source_token.is_file())
        self.assertFalse((data_dir / "antigravity-cli" / "token.json").exists())
        self.assertIn("profile or overlay owner changed", output.getvalue())

    def test_fix_skips_overlay_recovery_when_overlay_directory_is_replaced(self):
        import locks
        import shutil
        from unittest import mock

        overlay = self.store.overlays_dir / "alpha"
        moved_overlay = self.fake_home / "alpha-before-replacement"
        replacement_token = self.fake_home / "replacement-token.txt"
        replacement_token.write_text("foreign overlay", encoding="utf-8")
        original_try_lock = locks.try_mutation_lock
        replaced = []

        def replace_overlay_after_lock(store, name):
            handle = original_try_lock(store, name)
            if name == "alpha" and handle is not None and not replaced:
                overlay.rename(moved_overlay)
                overlay.mkdir()
                replacement_data = overlay / platforms.AGY_DATA_DIR_NAME
                replacement_data.mkdir()
                shutil.copy2(replacement_token, replacement_data / "foreign.txt")
                replaced.append(name)
            return handle

        output = io.StringIO()
        with mock.patch.object(keychain, "supported", return_value=False), \
                mock.patch.object(locks, "try_mutation_lock", side_effect=replace_overlay_after_lock), \
                mock.patch.object(isolation, "migrate_real_dir_to_store") as migrate, \
                contextlib.redirect_stderr(output):
            doctor._apply_fixes(self.store, self._ctx())

        migrate.assert_not_called()
        self.assertEqual(replaced, ["alpha"])
        self.assertTrue(
            (moved_overlay / platforms.AGY_DATA_DIR_NAME / "antigravity-cli" / "token.json").is_file()
        )
        self.assertEqual(
            (overlay / platforms.AGY_DATA_DIR_NAME / "foreign.txt").read_text(
                encoding="utf-8"
            ),
            "foreign overlay",
        )
        self.assertIn("profile or overlay owner changed", output.getvalue())

    def test_fix_clears_dangling_default_profile(self):
        """If the default points at a profile that no longer exists, --fix
        clears it instead of leaving the user unable to operate."""
        config = self.store.load_config()
        config.default_profile = "ghost"
        self.store.save_config(config)
        self.assertEqual(self.store.default_name(), "ghost")

        with mock.patch.object(keychain, "supported", return_value=False):
            doctor.run_checks(self.store, fix=True)
        self.assertIsNone(self.store.default_name())

    def test_fix_purges_orphan_keychain_slots(self):
        """orphan_slots -> delete_slot for each; shared slot NEVER targeted.
        Both calls made by the purge step must be threaded the SAME
        resolved keychain path (never the ambient default)."""
        calls = []
        fake_path = Path("/fake/login.keychain-db")
        import locks
        original_serialize = keychain.serialized_access
        profile_locks_at_swap = []

        def fake_orphan(_store, known_names, keychain_path=None):
            calls.append(("orphan_slots", keychain_path))
            return ["ghost"]

        def fake_delete_slot(service, keychain_path=None):
            calls.append(("delete_slot", service, keychain_path))

        @contextlib.contextmanager
        def inspect_swap_lock(store):
            handle = locks.try_lock(store, "ghost")
            profile_locks_at_swap.append(handle is None)
            if handle is not None:
                handle.release()
            with original_serialize(store):
                yield

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    doctor.keychain, "_ensure_target_keychain", return_value=fake_path
                ), \
                mock.patch.object(doctor.keychain, "orphan_slots", fake_orphan), \
                mock.patch.object(
                    doctor.keychain, "serialized_access", side_effect=inspect_swap_lock
                ), \
                mock.patch.object(doctor.keychain, "delete_slot", fake_delete_slot):
            doctor.run_checks(self.store, fix=True)

        self.assertIn(("orphan_slots", fake_path), calls)
        self.assertIn(("delete_slot", "gemini/agydra/ghost", fake_path), calls)
        self.assertNotIn(("delete_slot", "gemini", fake_path), calls,
                         "shared slot is the real login — must never be touched")
        self.assertEqual(profile_locks_at_swap, [True])

    def test_fix_skips_keychain_slot_when_unreadable_profile_directory_exists(self):
        corrupt_dir = self.store.profiles_dir / "broken"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "profile.json").write_text("{broken", encoding="utf-8")
        self.assertNotIn("broken", self.store.names())
        deleted = []
        fake_path = Path("/fake/login.keychain-db")

        def fake_delete_slot(service, keychain_path=None):
            deleted.append(service)

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    doctor.keychain, "_ensure_target_keychain", return_value=fake_path
                ), \
                mock.patch.object(
                    doctor.keychain, "orphan_slots", return_value=["broken"]
                ), \
                mock.patch.object(
                    doctor.keychain, "delete_slot", side_effect=fake_delete_slot
                ):
            doctor._apply_fixes(self.store, self._ctx())

        self.assertEqual(deleted, [])
        self.assertTrue(corrupt_dir.exists())

    def test_fix_rechecks_profile_directory_after_acquiring_its_lock(self):
        import locks
        original_try_lock = locks.try_mutation_lock
        corrupt_dir = self.store.profile_dir("ghost")
        fake_path = Path("/fake/login.keychain-db")
        deleted = []

        def fake_delete_slot(service, keychain_path=None):
            deleted.append(service)

        def create_owner_during_lock(store, name):
            handle = original_try_lock(store, name)
            if name == "ghost":
                corrupt_dir.mkdir(parents=True)
                (corrupt_dir / "profile.json").write_text("{broken", encoding="utf-8")
            return handle

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    doctor.keychain, "_ensure_target_keychain", return_value=fake_path
                ), \
                mock.patch.object(
                    doctor.keychain, "orphan_slots", return_value=["ghost"]
                ), \
                mock.patch.object(
                    doctor.keychain, "delete_slot", side_effect=fake_delete_slot
                ), \
                mock.patch.object(locks, "try_mutation_lock", side_effect=create_owner_during_lock):
            doctor._apply_fixes(self.store, self._ctx())

        self.assertEqual(deleted, [])
        self.assertTrue(corrupt_dir.exists())

    def test_fix_skips_keychain_slot_when_profile_lock_is_already_held(self):
        import locks

        fake_path = Path("/fake/login.keychain-db")
        deleted = []

        def fake_delete_slot(service, keychain_path=None):
            deleted.append(service)

        handle = locks.try_lock(self.store, "ghost")
        try:
            with mock.patch.object(keychain, "supported", return_value=True), \
                    mock.patch.object(
                        doctor.keychain, "_ensure_target_keychain", return_value=fake_path
                    ), \
                    mock.patch.object(
                        doctor.keychain, "orphan_slots", return_value=["ghost"]
                    ), \
                    mock.patch.object(
                        doctor.keychain, "delete_slot", side_effect=fake_delete_slot
                    ):
                doctor._apply_fixes(self.store, self._ctx())
        finally:
            handle.release()

        self.assertEqual(deleted, [])

    def test_cmd_doctor_fix_reports_header_once_with_post_fix_exit_code(self):
        """`agydra doctor --fix` must run the check-and-print pass exactly
        ONCE (never re-print the whole report a second time), and its exit
        code must reflect POST-fix state: a dangling default that --fix
        clears must not leave the run reporting FAIL/exit 1 for that item
        (regression for the double full-report / pre-fix-exit-code bug)."""
        import cli

        config = self.store.load_config()
        config.default_profile = "ghost"
        self.store.save_config(config)
        self.assertEqual(self.store.default_name(), "ghost")

        args = argparse.Namespace(fix=True, force=True)
        buf = io.StringIO()
        with mock.patch.object(keychain, "supported", return_value=False), \
                contextlib.redirect_stdout(buf):
            rc = cli.cmd_doctor(self.store, args)

        output = buf.getvalue()
        self.assertEqual(
            output.count("agydra doctor —"), 1,
            "the check-report banner must be printed exactly once:\n" + output,
        )
        self.assertEqual(
            output.count("fixable items:"), 1,
            "the fixable-items preview must be printed exactly once:\n" + output,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(self.store.default_name())

    def test_apply_fixes_continues_when_build_overlay_fails(self):
        """If build_overlay raises an error when relinking after migration,
        _apply_fixes must warn and proceed with other repairs instead of aborting."""
        config = self.store.load_config()
        config.default_profile = "ghost"
        self.store.save_config(config)

        with mock.patch.object(isolation, "build_overlay", side_effect=isolation.IsolationError("symlink failed")), \
                mock.patch.object(doctor, "warn") as mock_warn:
            doctor._apply_fixes(self.store, self._ctx())

        mock_warn.assert_called()
        self.assertIsNone(self.store.default_name(), "dangling default profile must still be cleared")

    def test_apply_fixes_uses_fresh_names_for_default_and_orphans(self):
        """A profile created during the confirmation pause must not have its
        default cleared or its artifacts treated as orphans."""
        ctx = self._ctx()
        self.store.create("fresh")
        config = self.store.load_config()
        config.default_profile = "fresh"
        self.store.save_config(config)

        self.store.overlays_dir.mkdir(parents=True, exist_ok=True)
        fresh_overlay = self.store.overlays_dir / "fresh"
        fresh_overlay.mkdir()

        doctor._apply_fixes(self.store, ctx)

        self.assertEqual(self.store.default_name(), "fresh")
        self.assertTrue(fresh_overlay.exists())

    def test_preview_fixables_describes_actions_with_type_and_path(self):
        """Preview of fixable items must describe action + resource type + path."""
        (self.store.overlays_dir / "ghost").mkdir(parents=True, exist_ok=True)
        preview = doctor._preview_fixables(self.store, self._ctx())
        self.assertIn("remove orphan overlay directory overlays/ghost", preview)

    def test_preview_omits_non_overlay_engines_apply_skips(self):
        """The confirmed preview must never offer an action ``--fix``
        would silently skip: overlay migration is overlay-engine-only, so
        a claude profile's stale directory under ``overlays/`` stays out
        of the preview exactly as ``_apply_fixes`` skips it."""
        self.store.create("cc", engine="claude")
        (self.store.overlays_dir / "cc" / ".claude").mkdir(parents=True)
        preview = doctor._preview_fixables(self.store, self._ctx())
        self.assertFalse(
            any("migrate overlay data for 'cc'" in line for line in preview),
            f"non-overlay engine offered an unfixable migration:\n{preview}",
        )


class TestUiPaintValidation(unittest.TestCase):
    def test_paint_invalid_style_raises_keyerror_with_color_disabled(self):
        with mock.patch.object(ui, "color_enabled", return_value=False):
            with self.assertRaises(KeyError):
                ui.paint("hello", "invalid_style_name")


class TestOrphanScanCaching(BaseCase):
    """``_DoctorContext`` already exists to stop doctor's main check pass
    from re-globbing ``profiles/`` per check; the orphan scan (added later)
    slipped through as a second layer of the same duplicate work --
    ``find_orphans``/``orphan_slots`` used to run once for the check pass,
    once (redundantly) for ``_preview_fixables``, and once more (correctly)
    as the fix-time refresh."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        # A genuine file-based orphan: keeps find_orphans's scan non-empty
        # across every call, so the preview/fix steps actually run instead
        # of short-circuiting on "no fixable items".
        (self.store.overlays_dir / "ghost").mkdir(parents=True, exist_ok=True)

    def test_fix_run_scans_orphans_at_most_twice(self):
        """The check-and-fix decision flow ``cli.cmd_doctor`` drives --
        ``run_checks`` (check pass), ``_preview_fixables`` (what --fix
        would do) and ``_apply_fixes`` (the fix-time refresh) -- must call
        ``orphans.find_orphans``/``keychain.orphan_slots`` at most twice
        each: once for the check pass, once for the fix-time refresh
        (state may have drifted during the confirmation pause).
        ``_preview_fixables`` must reuse the check pass's cached result
        instead of recomputing a third time. (``doctor._post_fix_exit_code``
        is intentionally excluded here: it is a separate, necessary,
        state-changed-by-then recompute against a brand-new ``Store``, not
        part of this duplicate-work pattern.)"""
        import orphans as orphans_mod

        find_orphans_spy = mock.Mock(wraps=orphans_mod.find_orphans)
        orphan_slots_spy = mock.Mock(return_value=["ghost"])

        with mock.patch.object(orphans_mod, "find_orphans", find_orphans_spy), \
                mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    doctor.keychain, "describe",
                    return_value={
                        "supported": True, "shared": False,
                        "shared_format": None, "profile_slots": {},
                    },
                ), \
                mock.patch.object(doctor.keychain, "orphan_slots", orphan_slots_spy), \
                mock.patch.object(
                    doctor.keychain, "_ensure_target_keychain", return_value=None
                ), \
                mock.patch.object(doctor.keychain, "delete_slot"):
            ctx = doctor._build_ctx(self.store)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                doctor.run_checks(self.store, ctx=ctx)
            preview = doctor._preview_fixables(self.store, ctx)
            self.assertTrue(preview, "fixture must produce fixable items")
            doctor._apply_fixes(self.store, ctx)

        self.assertLessEqual(
            find_orphans_spy.call_count, 2,
            f"find_orphans should run at most twice (check pass + fix "
            f"refresh), got {find_orphans_spy.call_count}",
        )
        self.assertLessEqual(
            orphan_slots_spy.call_count, 2,
            f"orphan_slots should run at most twice (check pass + fix "
            f"refresh), got {orphan_slots_spy.call_count}",
        )

    def test_unreadable_default_profile_is_not_treated_as_dangling(self):
        self.store.create("work", engine="codex")
        self.store.set_default("work")
        (self.store.profiles_dir / "work" / "profile.json").write_text("{corrupt json", encoding="utf-8")

        ctx = doctor._build_ctx(self.store)
        status, output = doctor._check_profiles(self.store, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertNotIn("MISSING (dangling)", output)
        self.assertNotIn("does not exist", output)
        self.assertIn("unreadable profile metadata: work", output)

        preview = doctor._preview_fixables(self.store, ctx)
        self.assertNotIn("clear dangling default profile 'work'", preview)

        doctor._apply_fixes(self.store, ctx)
        self.assertEqual(self.store.default_name(), "work")


if __name__ == "__main__":
    unittest.main()
