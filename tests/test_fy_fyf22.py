import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import i18n
import locks
try:
    from conftest import BaseCase, snapshot_tree
except ImportError:
    from tests.conftest import BaseCase, snapshot_tree
from store import Store, StoreError


class TestTargetedReadsDuringPendingRename(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alice")
        self.store.create("bob")
        self.store._write_rename_journal("alice", "charlie", False)

    def _run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_unrelated_status_reads_succeed_without_changing_disk(self):
        commands = (
            ("status", "bob"),
            ("status", "-n", "bob"),
            ("-n", "-p", "bob"),
        )
        before = snapshot_tree(self.store.root, metadata=True)

        for command in commands:
            with self.subTest(command=command):
                code, stdout, stderr = self._run_cli(*command)
                self.assertEqual(code, 0, stderr)
                self.assertIn("bob", stdout)
                self.assertEqual(snapshot_tree(self.store.root, metadata=True), before)

    def test_references_are_checked_by_the_resolved_profile_name(self):
        before = snapshot_tree(self.store.root, metadata=True)

        self.assertEqual(self.store.resolve_ref_readonly("2"), "bob")
        self.assertEqual(self.store.resolve_ref_readonly("#2"), "bob")
        for ref in ("alice", "charlie", "1", "#1"):
            with self.subTest(ref=ref):
                with self.assertRaises(StoreError):
                    self.store.resolve_ref_readonly(ref)

        self.assertEqual(snapshot_tree(self.store.root, metadata=True), before)

    def test_rename_targets_and_whole_store_list_still_fail_closed(self):
        before = snapshot_tree(self.store.root, metadata=True)

        for command in (
            ("status", "alice"),
            ("status", "charlie"),
            ("list",),
        ):
            with self.subTest(command=command):
                code, _stdout, stderr = self._run_cli(*command)
                self.assertEqual(code, 1)
                self.assertIn(i18n.t("store.rename.interrupted"), stderr)
                self.assertEqual(snapshot_tree(self.store.root, metadata=True), before)

    def test_pending_rename_with_live_profile_lock_keeps_retry_message(self):
        handle = locks.try_lock(self.store, "alice")
        self.assertIsNotNone(handle)
        try:
            before = snapshot_tree(self.store.root, metadata=True)
            code, _stdout, stderr = self._run_cli("status", "alice")
            self.assertEqual(code, 1)
            self.assertIn(i18n.t("store.rename.pending"), stderr)
            self.assertNotIn(i18n.t("store.rename.interrupted"), stderr)
            self.assertEqual(snapshot_tree(self.store.root, metadata=True), before)
        finally:
            handle.release()

    def test_pending_rename_with_live_profile_and_sequence_locks_keeps_retry_message(self):
        profile_handle = locks.try_lock(self.store, "alice")
        sequence_handle = locks.try_sequence_lock(self.store)
        self.assertIsNotNone(profile_handle)
        self.assertIsNotNone(sequence_handle)
        try:
            before = snapshot_tree(self.store.root, metadata=True)
            code, _stdout, stderr = self._run_cli("status", "alice")
            self.assertEqual(code, 1)
            self.assertIn(i18n.t("store.rename.pending"), stderr)
            self.assertNotIn(i18n.t("store.rename.interrupted"), stderr)
            self.assertEqual(snapshot_tree(self.store.root, metadata=True), before)
        finally:
            sequence_handle.release()
            profile_handle.release()

    def test_unrelated_usage_reads_succeed_without_recovering_rename(self):
        import usage
        from unittest import mock

        before = snapshot_tree(self.store.root, metadata=True)
        result = usage.UsageResult(name="bob", ok=True, engine="agy", email="bob@example.com")

        with mock.patch.object(cli.usage, "query_profile_usage", return_value=result):
            code, stdout, stderr = self._run_cli("usage", "bob")

        self.assertEqual(code, 0, stderr)
        self.assertIn("bob", stdout)
        self.assertEqual(snapshot_tree(self.store.root, metadata=True), before)

    def test_usage_on_rename_targets_fails_closed(self):
        before = snapshot_tree(self.store.root, metadata=True)

        for ref in ("alice", "charlie"):
            with self.subTest(ref=ref):
                code, _stdout, stderr = self._run_cli("usage", ref)
                self.assertEqual(code, 1)
                self.assertIn(i18n.t("store.rename.interrupted"), stderr)
                self.assertEqual(snapshot_tree(self.store.root, metadata=True), before)


if __name__ == "__main__":
    unittest.main()
