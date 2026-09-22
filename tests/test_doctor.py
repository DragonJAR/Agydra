"""Doctor end-to-end: healthy store → 0; missing binary/corrupt metadata → 1/warn."""
import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agydra import doctor  # noqa: E402
from agydra.store import Store  # noqa: E402

from conftest import BaseCase  # noqa: E402


class TestDoctor(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_healthy_store_returns_zero(self):
        self.store.create("work")
        # Seed the store with one of the canary filenames so schema passes.
        (self.store.profile_data_dir("work") / "antigravity-cli").mkdir()
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)

    def test_missing_binary_returns_one(self):
        self.store.create("work")
        # BaseCase installs a fake binary; point the override at a missing path.
        os.environ["AGYDRA_AGY_BIN"] = str(self.bin_dir / "missing-agy")
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 1)

    def test_corrupt_metadata_does_not_break_run(self):
        self.store.create("work")
        # Write malformed metadata into a sibling directory.
        corrupt = self.store.profiles_dir / "broken"
        (corrupt / "data").mkdir(parents=True)
        (corrupt / "profile.json").write_text("{this is not json", encoding="utf-8")
        # store remains usable: list() tolerates the corrupt profile.
        names = self.store.names()
        self.assertEqual(names, ["work"])
        self.assertIn("broken", self.store.unreadable_profiles())
        # Doctor surfaces the unreadable profile.
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)

    def test_empty_store_returns_zero(self):
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)

    def test_fake_binary_is_executable_and_resolvable(self):
        # Sanity: the conftest fixture installs a working fake agy.
        self.assertTrue(Path(self.agy_bin).exists())
        st = Path(self.agy_bin).stat()
        self.assertTrue(st.st_mode & stat.S_IXUSR)

    def test_locks_check_reports_live_session(self):
        from agydra import locks

        self.store.create("work")
        handle = locks.try_lock(self.store, "work")
        try:
            exit_code = doctor.run_checks(self.store)
        finally:
            handle.release()
        # A live session is a warning, never a failure.
        self.assertEqual(exit_code, 0)

    def test_locks_check_passes_when_free(self):
        self.store.create("work")
        exit_code = doctor.run_checks(self.store)
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
