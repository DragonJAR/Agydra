"""Doctor end-to-end: healthy store → 0; missing binary/corrupt metadata → 1/warn."""
import argparse
import base64
import contextlib
import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

    def _ctx(self):
        return doctor._DoctorContext(
            scan=self.store.scan(), names=self.store.names(),
        )

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

        with mock.patch.object(keychain, "supported", return_value=True):
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

        with mock.patch.object(keychain, "supported", return_value=True):
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
        data_dir = self.store.profile_data_dir("alpha")
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        # pre-conditions: data dir empty of the marker file, overlay has it
        self.assertFalse((data_dir / "antigravity-cli" / "token.json").exists())
        self.assertTrue((link / "antigravity-cli" / "token.json").exists())

        with mock.patch.object(keychain, "supported", return_value=False):
            rc = doctor.run_checks(self.store, fix=True)

        self.assertEqual(rc, 0)
        self.assertTrue((data_dir / "antigravity-cli" / "token.json").exists(),
                        "real-dir contents must move into profile data dir")
        self.assertTrue(isolation._is_link(link),
                        "build_overlay must relink after migrate")

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

        def fake_orphan(_store, known_names, keychain_path=None):
            calls.append(("orphan_slots", keychain_path))
            return ["ghost"]

        def fake_delete_slot(service, keychain_path=None):
            calls.append(("delete_slot", service, keychain_path))

        with mock.patch.object(keychain, "supported", return_value=True), \
                mock.patch.object(
                    doctor.keychain, "_ensure_target_keychain", return_value=fake_path
                ), \
                mock.patch.object(doctor.keychain, "orphan_slots", fake_orphan), \
                mock.patch.object(doctor.keychain, "delete_slot", fake_delete_slot):
            doctor.run_checks(self.store, fix=True)

        self.assertIn(("orphan_slots", fake_path), calls)
        self.assertIn(("delete_slot", "gemini/agydra/ghost", fake_path), calls)
        self.assertNotIn(("delete_slot", "gemini", fake_path), calls,
                         "shared slot is the real login — must never be touched")

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


if __name__ == "__main__":
    unittest.main()
