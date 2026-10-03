"""Store transaction boundaries for config writes and slow deletions."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import BaseCase
from models import Profile
from store import Config, Store, StoreError


class TestStoreUpdateConfig(BaseCase):
    def test_mutator_runs_under_sequence_lock_and_returns_saved_config(self):
        import locks

        store = Store(self.store_root)
        observations = []

        def mutate(config):
            handle = locks.try_sequence_lock(store)
            observations.append(handle is None)
            if handle is not None:
                handle.release()
            config.default_profile = "alpha"
            config.settings["lang"] = "es"

        updated = store.update_config(mutate)

        self.assertEqual(observations, [True])
        self.assertEqual(updated.default_profile, "alpha")
        self.assertEqual(updated.settings["lang"], "es")
        self.assertEqual(
            json.loads(store.config_path.read_text(encoding="utf-8")),
            updated.to_dict(),
        )

    def test_mutator_failure_writes_nothing_and_releases_sequence_lock(self):
        import locks

        store = Store(self.store_root)
        store.save_config(Config(default_profile="before"))
        original = store.config_path.read_bytes()

        def fail(config):
            config.default_profile = "partial"
            raise RuntimeError("mutator failed")

        with self.assertRaisesRegex(RuntimeError, "mutator failed"):
            store.update_config(fail)

        self.assertEqual(store.config_path.read_bytes(), original)
        handle = locks.try_sequence_lock(store)
        self.assertIsNotNone(handle)
        handle.release()

    def test_busy_sequence_lock_fails_immediately_without_running_mutator(self):
        import locks

        store = Store(self.store_root)
        store.save_config(Config(default_profile="before"))
        original = store.config_path.read_bytes()
        handle = locks.try_sequence_lock(store)
        self.assertIsNotNone(handle)
        called = []
        try:
            with self.assertRaisesRegex(StoreError, "sequence lock is busy"):
                store.update_config(lambda config: called.append(config))
        finally:
            handle.release()

        self.assertEqual(called, [])
        self.assertEqual(store.config_path.read_bytes(), original)


class TestDeleteSequenceLockScope(BaseCase):
    def test_backup_runs_without_sequence_lock_and_with_profile_lock_held(self):
        import locks

        store = Store(self.store_root)
        store.create("victim")
        data = store.profile_dir("victim") / "data" / "payload"
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_bytes(b"archive me")
        entered_backup = threading.Event()
        continue_backup = threading.Event()
        sequence_was_free = []
        profile_was_busy = []
        outcomes = []
        original_backup = store._write_backup

        def pause_backup(name, claude_config=None, *, prune=True):
            sequence_handle = locks.try_sequence_lock(store)
            sequence_was_free.append(sequence_handle is not None)
            if sequence_handle is not None:
                sequence_handle.release()
            profile_handle = locks.try_lock(store, name)
            profile_was_busy.append(profile_handle is None)
            if profile_handle is not None:
                profile_handle.release()
            entered_backup.set()
            if not continue_backup.wait(10):
                raise RuntimeError("backup barrier timed out")
            return original_backup(name, claude_config, prune=prune)

        def delete_victim():
            try:
                outcomes.append(store.delete("victim"))
            except BaseException as exc:
                outcomes.append(exc)

        worker = threading.Thread(target=delete_victim)
        with unittest.mock.patch.object(
            store, "_write_backup", side_effect=pause_backup
        ):
            worker.start()
            try:
                self.assertTrue(entered_backup.wait(10))
                created = store.create("parallel")
                self.assertEqual(created.seq, 2)
                with self.assertRaisesRegex(StoreError, "live session"):
                    store.delete("victim", backup=False)
            finally:
                continue_backup.set()
                worker.join(15)

        self.assertFalse(worker.is_alive())
        self.assertEqual(sequence_was_free, [True])
        self.assertEqual(profile_was_busy, [True])
        self.assertEqual(len(outcomes), 1)
        if isinstance(outcomes[0], BaseException):
            raise outcomes[0]
        backup = outcomes[0]
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertFalse(store.profile_dir("victim").exists())

    def test_busy_sequence_lock_after_verified_backup_preserves_profile(self):
        import locks
        import zipfile

        store = Store(self.store_root)
        store.create("victim")
        data = store.profile_dir("victim") / "data" / "payload"
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_bytes(b"keep me")
        original_backup = store._write_backup
        backup_paths = []
        sequence_handles = []

        def create_backup_then_hold_sequence_lock(
            name, claude_config=None, *, prune=True
        ):
            backup = original_backup(name, claude_config, prune=prune)
            backup_paths.append(backup)
            handle = locks.try_sequence_lock(store)
            self.assertIsNotNone(handle)
            sequence_handles.append(handle)
            return backup

        try:
            with unittest.mock.patch.object(
                store,
                "_write_backup",
                side_effect=create_backup_then_hold_sequence_lock,
            ):
                with self.assertRaisesRegex(StoreError, "sequence lock is busy"):
                    store.delete("victim")
        finally:
            for handle in sequence_handles:
                handle.release()

        self.assertEqual(len(backup_paths), 1)
        backup = backup_paths[0]
        self.assertTrue(backup.is_file())
        with zipfile.ZipFile(backup) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("data/payload"), b"keep me")
        self.assertTrue(store.profile_dir("victim").is_dir())
        self.assertEqual(data.read_bytes(), b"keep me")
        self.assertEqual(store.get("victim").name, "victim")


class TestClaudeSupervisorStoreRoot(BaseCase):
    def test_claude_supervisor_environment_receives_physical_store_root(self):
        store = Store(self.store_root)
        profile = Profile(name="claude", seq=1, engine="claude")
        profile_dir = store.profile_dir(profile.name)
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.json").write_text(
            json.dumps(profile.to_dict()), encoding="utf-8"
        )
        config_dir = store.claude_config_dir_for_seq(profile.seq)
        config_dir.mkdir(parents=True)
        (config_dir / "daemon.log").write_text("evidence", encoding="utf-8")
        driver = unittest.mock.Mock()
        driver.resolve_binary.return_value = Path("/sandbox/bin/claude")
        completed = subprocess.CompletedProcess(
            args=["claude", "daemon", "status"],
            returncode=1,
            stdout="not running\n",
            stderr="",
        )

        with unittest.mock.patch("engines.get_engine", return_value=driver):
            with unittest.mock.patch(
                "isolation.isolated_env", return_value={}
            ) as isolated_env:
                with unittest.mock.patch(
                    "platforms.run_with_group_kill", return_value=completed
                ):
                    store._guard_claude_supervisor(profile.name, config_dir)

        isolated_env.assert_called_once_with(
            config_dir, {}, engine="claude", store_root=store.root
        )


if __name__ == "__main__":
    unittest.main()
