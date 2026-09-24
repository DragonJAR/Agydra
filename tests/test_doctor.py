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
import isolation, keychain, platforms
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
            profile_count=len(self.store.names()),
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
            profile_count=len(self.store.names()),
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
            profile_count=len(self.store.names()),
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


if __name__ == "__main__":
    unittest.main()
