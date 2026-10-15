import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.conftest import BaseCase
from store import Store, StoreError
from models import Profile
import cli
import locks
import doctor


class TestFxLifecycle(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(root=self.store_root)

    def _register_live_holder(self, name: str):
        import platforms
        path = locks.lock_path(self.store, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"holders": [{"pid": os.getpid(), "start": platforms.process_start_token(os.getpid())}]}),
            encoding="utf-8",
        )
        return path

    def test_c1_try_read_lock_coexists_with_lease_and_blocks_mutation(self):
        self.store.create("work")
        self._register_live_holder("work")
        self.assertTrue(locks.is_locked(self.store, "work"))

        handle = locks.try_read_lock(self.store, "work")
        self.assertIsNotNone(handle)
        try:
            self.assertIsNone(locks.try_mutation_lock(self.store, "work"))
        finally:
            handle.release()

    def test_c1_share_config_source_live_succeeds(self):
        self.store.create("work")
        self.store.create("other")
        src_dir = self.store.profile_data_dir("work")
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        self._register_live_holder("work")

        copied = cli._share_config(self.store, "work", ["other"])
        self.assertIn("other/settings.json", copied)
        dest_settings = self.store.profile_data_dir("other") / "settings.json"
        self.assertTrue(dest_settings.exists())
        self.assertEqual(json.loads(dest_settings.read_text(encoding="utf-8")), {"theme": "dark"})

    def test_c1_share_config_guards_torn_reads(self):
        self.store.create("work")
        self.store.create("other")
        src_dir = self.store.profile_data_dir("work")
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "settings.json").write_text('{"theme": "dark", INVALID', encoding="utf-8")

        with mock.patch("cli._note") as mock_note:
            copied = cli._share_config(self.store, "work", ["other"])
            self.assertEqual(copied, [])
            mock_note.assert_called()

    def test_c1_cmd_export_live_profile_notes_best_effort_snapshot(self):
        self.store.create("work")
        self._register_live_holder("work")
        args = mock.Mock(ref="work", output=str(self._tmp / "work-export.zip"))

        with mock.patch("cli._note") as mock_note:
            code = cli.cmd_export(self.store, args)
            self.assertEqual(code, 0)
            mock_note.assert_called_with("exporting live profile 'work'; the archive is a best-effort snapshot")

    def test_c2_shared_config_files_and_import_overwrites_them(self):
        self.assertEqual(cli.SHARED_CONFIG_FILES, ("settings.json", "mcp.json", "config.toml"))
        self.store.create("clean")
        data_dir = self.store.profile_data_dir("clean")
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "settings.json").write_text('{"local": true}', encoding="utf-8")

        source_dir = self._tmp / "source_generic"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "real.txt").write_text("imported", encoding="utf-8")

        args = mock.Mock(ref="clean", source=str(source_dir))
        code = cli.cmd_import(self.store, args)
        self.assertEqual(code, 0)
        self.assertTrue((data_dir / "real.txt").exists())

        self.store.create("dirty")
        dirty_dir = self.store.profile_data_dir("dirty")
        dirty_dir.mkdir(parents=True, exist_ok=True)
        (dirty_dir / "settings.json").write_text('{"local": true}', encoding="utf-8")
        (dirty_dir / "extra.dat").write_text("unexpected", encoding="utf-8")

        dirty_args = mock.Mock(ref="dirty", source=str(source_dir))
        with self.assertRaises(StoreError) as ctx:
            cli.cmd_import(self.store, dirty_args)
        self.assertIn("already has data", str(ctx.exception))

    def test_c3_cmd_create_settings_copy_catches_store_error_and_os_error(self):
        self.store.create("default-prof")
        config = self.store.load_config()
        config.default_profile = "default-prof"
        self.store.save_config(config)

        args = mock.Mock(description="", engine="agy")
        args.name = "new-prof"
        with mock.patch("cli._share_config", side_effect=StoreError("simulated share failure")), mock.patch("cli._note") as mock_note:
            code = cli.cmd_create(self.store, args)
            self.assertEqual(code, 0)
            mock_note.assert_called()

        args2 = mock.Mock(description="", engine="agy")
        args2.name = "new-prof2"
        with mock.patch("cli._share_config", side_effect=OSError("simulated disk error")), mock.patch("cli._note") as mock_note:
            code = cli.cmd_create(self.store, args2)
            self.assertEqual(code, 0)
            mock_note.assert_called()

    def test_c4_sequence_lock_default_patience_is_read_lock_patience(self):
        import inspect
        sig = inspect.signature(Store._with_sequence_lock)
        self.assertEqual(sig.parameters["patience_s"].default, Store.READ_LOCK_PATIENCE_S)

    def test_c4_try_mutation_lock_patience_and_immediate_refusal_for_live_holders(self):
        self.store.create("busy-prof")
        self._register_live_holder("busy-prof")
        start = os.times()
        handle = locks.try_mutation_lock(self.store, "busy-prof", patience_s=1.0)
        self.assertIsNone(handle)

    def test_c4_create_purges_keychain_inside_sequence_lock(self):
        calls = []
        import engines
        driver = engines.get_engine("agy")

        def mock_purge(store, name):
            handle = locks.try_sequence_lock(store)
            seq_held = handle is None
            if handle is not None:
                handle.release()
            calls.append(("purge", seq_held))

        with mock.patch("keychain.purge_profile_slot", side_effect=mock_purge):
            self.store.create("test-purge-seq", engine="agy")
        self.assertTrue(len(calls) > 0)
        purge_call, seq_held = calls[0]
        self.assertTrue(seq_held)

    def test_c5_pending_rename_recovery_action_failure_allows_unrelated_reads(self):
        self.store.create("unrelated")
        self.store.create("old")
        action_name = "tests.failing-recovery"
        def failing_handler(store, old, new, data):
            raise RuntimeError("simulated keychain recovery failure")
        self.store.register_rename_recovery_handler(action_name, failing_handler)

        def fail_after(profile):
            raise RuntimeError("interrupt rename")

        with self.assertRaises(StoreError):
            self.store.rename(
                "old",
                "new",
                after_rename=fail_after,
                recovery_action=action_name,
                recovery_data_provider=lambda: {},
            )
        self.assertTrue(self.store.rename_journal_path.is_file())

        with mock.patch("store.warn") as mock_warn:
            unrelated = self.store.get("unrelated")
            self.assertEqual(unrelated.name, "unrelated")
            mock_warn.assert_called()

        with mock.patch("store.warn"):
            unrelated_ro = self.store.get_readonly("unrelated")
            self.assertEqual(unrelated_ro.name, "unrelated")

        with mock.patch("store.warn"):
            with self.assertRaises(StoreError):
                self.store.get("new")

        with mock.patch("store.warn"):
            with self.assertRaises(StoreError):
                self.store.get_readonly("new")

    def test_c6_cmd_delete_unreadable_metadata_purges_keychain(self):
        self.store.create("corrupt-agy", engine="agy")
        meta = self.store.profile_meta_path("corrupt-agy")
        meta.write_text("corrupted json", encoding="utf-8")

        purged = []
        with mock.patch("keychain.purge_profile_slot", side_effect=lambda s, n: purged.append(n)):
            args = mock.Mock(ref="corrupt-agy", no_backup=True, force=True)
            code = cli.cmd_delete(self.store, args)
            self.assertEqual(code, 0)
            self.assertIn("corrupt-agy", purged)

    def test_c7_cmd_list_uses_record_profile_email_and_shows_unreadable(self):
        self.store.create("broken-meta")
        meta = self.store.profile_meta_path("broken-meta")
        meta.write_text("invalid json", encoding="utf-8")

        with mock.patch("account.record_profile_email") as mock_record, mock.patch("cli._warn") as mock_warn:
            code = cli.cmd_list(self.store, mock.Mock())
            self.assertEqual(code, 0)
            mock_warn.assert_called()

    def test_c7_claude_email_fallback_in_list_and_status(self):
        claude_prof = self.store.create("claude-p", engine="claude")
        claude_prof.email = "cached@anthropic.com"
        self.store.save(claude_prof)

        fake_native = mock.Mock(email=None, auth_state_label="unknown", state="unknown", reason="no token")
        with mock.patch("account.claude_auth_status", return_value=fake_native):
            with mock.patch("builtins.print") as mock_print:
                cli.cmd_list(self.store, mock.Mock())
                printed = " ".join(str(call) for call in mock_print.call_args_list)
                self.assertIn("cached@anthropic.com", printed)

            with mock.patch("builtins.print") as mock_print:
                cli.cmd_status(self.store, mock.Mock(ref="claude-p", engine=None, dry_run=False))
                printed = " ".join(str(call) for call in mock_print.call_args_list)
                self.assertIn("cached@anthropic.com", printed)

    def test_c8_duplicate_sequence_tolerant_scan_and_doctor_failure(self):
        p1 = self.store.create("first-c", engine="claude")
        p2 = self.store.create("second-c", engine="claude")
        p2_meta = self.store.profile_meta_path("second-c")
        p2_dict = json.loads(p2_meta.read_text(encoding="utf-8"))
        p2_dict["seq"] = p1.seq
        p2_meta.write_text(json.dumps(p2_dict), encoding="utf-8")

        with self.assertRaises(StoreError):
            self.store.scan()

        profiles, unreadable = self.store.scan(tolerant=True)
        self.assertEqual(len(profiles), 2)

        ctx = doctor._build_ctx(self.store)
        self.assertEqual(len(ctx.scan[0]), 2)

        status, message = doctor._check_profiles(self.store, ctx)
        self.assertEqual(status, doctor.FAIL)
        self.assertIn("duplicate positive profile sequence", message)
        self.assertIn(str(self.store.profile_meta_path("first-c")), message)
        self.assertIn(str(self.store.profile_meta_path("second-c")), message)

    def test_g1_readonly_never_mutates_or_recovers_pending_rename(self):
        self.store.create("old")
        self.store.create("other")
        self.store._write_rename_journal("old", "new", False)
        journal_content = self.store.rename_journal_path.read_text(encoding="utf-8")

        with mock.patch("locks.try_lock") as mock_lock, \
             mock.patch("locks.try_mutation_lock") as mock_mutation_lock, \
             mock.patch("locks.try_sequence_lock") as mock_seq_lock:
            unrelated = self.store.get_readonly("other")
            self.assertEqual(unrelated.name, "other")
            self.assertEqual(
                self.store.rename_journal_path.read_text(encoding="utf-8"),
                journal_content,
            )
            self.assertTrue(self.store.profile_dir("old").exists())
            self.assertFalse(self.store.profile_dir("new").exists())

            for target in ("old", "new"):
                with self.assertRaises(StoreError) as cm:
                    self.store.get_readonly(target)
                err_msg = str(cm.exception)
                self.assertIn("next launch or profile-changing command", err_msg)
                self.assertNotIn("run any agydra command that writes", err_msg)

            with self.assertRaises(StoreError) as cm:
                self.store.scan_readonly()
            err_msg = str(cm.exception)
            self.assertIn("next launch or profile-changing command", err_msg)
            self.assertNotIn("run any agydra command that writes", err_msg)

            self.assertEqual(self.store.resolve_ref_readonly("other"), "other")

            mock_lock.assert_not_called()
            mock_mutation_lock.assert_not_called()
            mock_seq_lock.assert_not_called()

        self.assertEqual(
            self.store.rename_journal_path.read_text(encoding="utf-8"),
            journal_content,
        )
        self.assertTrue(self.store.profile_dir("old").exists())
        self.assertFalse(self.store.profile_dir("new").exists())


class TestUsageFinishesPendingRename(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("old")
        self.store.create("other")
        self.store._write_rename_journal("old", "new", False)

    def _usage(self, argv):
        import contextlib
        import io
        import usage

        def fake_query(store, name, *args, **kwargs):
            return usage.UsageResult(name=name, ok=False, error="not authenticated")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(usage, "query_profile_usage", side_effect=fake_query), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(argv)
        return status, out.getvalue(), err.getvalue()

    def _assert_read_reports_retry_and_next_command_heals(self, argv):
        journal_before = self.store.rename_journal_path.read_bytes()
        status, out, err = self._usage(argv)
        report = out + err
        self.assertEqual(status, 1, report)
        self.assertIn("profile rename recovery is pending", report)
        self.assertIn("next launch or profile-changing command", report)
        self.assertEqual(self.store.rename_journal_path.read_bytes(), journal_before)

        with mock.patch.object(cli, "Store", return_value=self.store):
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
                status = cli.main(["create", "recovered", "--engine", "codex"])
        self.assertEqual(status, 0)
        self.assertFalse(self.store.rename_journal_path.exists())
        status, out, err = self._usage(argv)
        self.assertNotIn("next launch or profile-changing command", out + err)

    def test_usage_table_reports_retry_then_next_command_heals(self):
        self._assert_read_reports_retry_and_next_command_heals(["usage"])

    def test_usage_detail_reports_retry_then_next_command_heals(self):
        self._assert_read_reports_retry_and_next_command_heals(["usage", "new"])


class TestPendingRenameCommandRecovery(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("old", engine="codex")
        self.store.create("other", engine="codex")
        self.store._write_rename_journal("old", "new", True)
        self.store.profile_dir("old").rename(self.store.profile_dir("new"))

    def _launch(self, argv, random_pick):
        import contextlib
        import io

        def build_after_recovery(store, *_args, **_kwargs):
            self.assertFalse(store.rename_journal_path.exists())
            self.assertEqual(store.get("new").name, "new")
            return mock.sentinel.plan

        with mock.patch.object(cli, "Store", return_value=self.store), mock.patch.object(
            cli.runner, "build_plan", side_effect=build_after_recovery
        ) as build_plan, mock.patch.object(
            cli.runner, "run", return_value=0
        ) as run, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            result = cli.main(argv)

        self.assertEqual(result, 0)
        self.assertTrue(build_plan.called)
        self.assertTrue(run.called)
        self.assertFalse(self.store.rename_journal_path.exists())
        if random_pick:
            self.assertTrue(build_plan.call_args.kwargs["random_pick"])
        else:
            self.assertEqual(build_plan.call_args.kwargs["flag_ref"], "new")

    def test_explicit_launch_recovers_before_profile_resolution(self):
        self._launch(["--profile", "new"], random_pick=False)

    def test_random_launch_recovers_before_profile_resolution(self):
        self._launch(["--random"], random_pick=True)

    def test_default_command_recovers_before_resolving_reference(self):
        import contextlib
        import io

        with mock.patch.object(cli, "Store", return_value=self.store), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(["default", "new"])

        self.assertEqual(result, 0)
        self.assertFalse(self.store.rename_journal_path.exists())
        self.assertEqual(self.store.default_name(), "new")

    def test_list_status_and_dry_run_report_retry_without_recovery(self):
        import contextlib
        import io

        journal_before = self.store.rename_journal_path.read_bytes()
        for argv in (
            ["list"],
            ["status", "new"],
            ["default"],
            ["--profile", "new", "--dry-run"],
        ):
            with self.subTest(argv=argv), mock.patch.object(
                cli, "Store", return_value=self.store
            ), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(
                io.StringIO()
            ) as err:
                result = cli.main(argv)

            self.assertEqual(result, 1)
            self.assertIn(
                "next launch or profile-changing command", out.getvalue() + err.getvalue()
            )
            self.assertEqual(self.store.rename_journal_path.read_bytes(), journal_before)
        self.assertTrue(self.store.rename_journal_path.exists())


class TestUnsafeRenameLaunchRecovery(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("old", engine="codex")
        self.store.create("new", engine="codex")
        self.store.create("unrelated", engine="codex")
        self.store._write_rename_journal("old", "new", True)

    def _run_launch(self, argv, build_plan):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "Store", return_value=self.store), mock.patch.object(
            cli.runner, "build_plan", side_effect=build_plan
        ) as plan, mock.patch.object(cli.runner, "run", return_value=0) as run, contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            result = cli.main(argv)
        return result, out.getvalue() + err.getvalue(), plan, run

    def test_malformed_journal_blocks_unrelated_launch_then_mutation_repairs_it(self):
        import contextlib
        import io

        raw = b"{broken journal"
        self.store.rename_journal_path.write_bytes(raw)
        result, output, plan, run = self._run_launch(
            ["--profile", "unrelated"], lambda *_args, **_kwargs: mock.sentinel.plan
        )

        self.assertEqual(result, 1, output)
        self.assertIn("launch was not started", output)
        self.assertIn("ordinary mutating command", output)
        plan.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.store.rename_journal_path.read_bytes(), raw)
        self.assertTrue(self.store.profile_dir("unrelated").is_dir())
        self.assertFalse(list(self.store.root.glob("profile-rename.json.corrupt-*")))

        with mock.patch.object(cli, "Store", return_value=self.store), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(["create", "healed", "--engine", "codex"])

        self.assertEqual(result, 0)
        self.assertFalse(self.store.rename_journal_path.exists())
        quarantines = list(self.store.root.glob("profile-rename.json.corrupt-*"))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(quarantines[0].read_bytes(), raw)

    def test_conflicting_journal_blocks_launches_of_both_ambiguous_names(self):
        import store as store_module

        journal_before = self.store.rename_journal_path.read_bytes()
        for directories_present in (True, False):
            if not directories_present:
                for name in ("old", "new"):
                    store_module.rmtree(self.store.profile_dir(name))
            for name in ("old", "new"):
                with self.subTest(directories_present=directories_present, name=name):
                    result, output, plan, run = self._run_launch(
                        ["--profile", name], lambda *_args, **_kwargs: mock.sentinel.plan
                    )

                    self.assertEqual(result, 1, output)
                    self.assertIn("conflicting profile state", output)
                    plan.assert_not_called()
                    run.assert_not_called()
                    self.assertEqual(
                        self.store.rename_journal_path.read_bytes(), journal_before
                    )
                    self.assertEqual(self.store.profile_dir("old").is_dir(), directories_present)
                    self.assertEqual(self.store.profile_dir("new").is_dir(), directories_present)

    def test_conflicting_journal_allows_unrelated_launch_without_state_changes(self):
        import store as store_module

        def build_unrelated(store, *_args, **_kwargs):
            profiles, _unreadable = store.scan()
            self.assertEqual({profile.name for profile in profiles}, {"unrelated"})
            self.assertEqual(store.get("unrelated").name, "unrelated")
            return mock.sentinel.plan

        for directories_present in (True, False):
            if not directories_present:
                for name in ("old", "new"):
                    store_module.rmtree(self.store.profile_dir(name))
            journal_before = self.store.rename_journal_path.read_bytes()
            with self.subTest(directories_present=directories_present):
                result, output, plan, run = self._run_launch(
                    ["--profile", "unrelated"], build_unrelated
                )

                self.assertEqual(result, 0, output)
                plan.assert_called_once()
                run.assert_called_once_with(
                    mock.sentinel.plan, store=self.store, dry_run=False
                )
                self.assertEqual(
                    self.store.rename_journal_path.read_bytes(), journal_before
                )
                self.assertEqual(
                    self.store.profile_dir("old").is_dir(), directories_present
                )
                self.assertEqual(
                    self.store.profile_dir("new").is_dir(), directories_present
                )
                with self.assertRaisesRegex(
                    StoreError, "profile rename recovery is pending"
                ):
                    self.store.scan()
                self.assertFalse(
                    list(self.store.root.glob("profile-rename.json.corrupt-*"))
                )

    def test_conflicting_journal_allows_random_launch_for_unrelated_profile(self):
        import sys

        selected = []

        def run(plan, **_kwargs):
            selected.append(plan.profile)
            return 0

        journal_before = self.store.rename_journal_path.read_bytes()
        with mock.patch.object(cli, "Store", return_value=self.store), mock.patch(
            "account.passive_auth_state", return_value="authenticated"
        ), mock.patch.object(cli.runner, "run", side_effect=run):
            result = cli.main(["-r", "--binary", sys.executable])

        self.assertEqual(result, 0)
        self.assertEqual(selected, ["unrelated"])
        self.assertEqual(self.store.rename_journal_path.read_bytes(), journal_before)
        self.assertTrue(self.store.profile_dir("old").is_dir())
        self.assertTrue(self.store.profile_dir("new").is_dir())

    def test_direct_public_recovery_never_leaves_launch_filter_installed(self):
        with self.assertRaises(TypeError):
            self.store.recover_pending_rename(
                target_name="unrelated", for_launch=True
            )

        with self.assertRaises(StoreError):
            self.store.recover_pending_rename(target_name="unrelated")

        self.assertIsNone(self.store._launch_rename_conflict.get())
        with self.assertRaises(StoreError):
            self.store.scan()

    def test_conflicting_launch_scope_is_context_local_across_threads(self):
        from queue import Queue
        from threading import Thread

        outcomes = Queue()

        def scan_from_another_thread():
            try:
                outcomes.put(self.store.scan())
            except Exception as exc:
                outcomes.put(exc)

        with self.store.launch_recovery_scope(target_name="unrelated"):
            worker = Thread(target=scan_from_another_thread)
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        outcome = outcomes.get_nowait()
        self.assertIsInstance(outcome, StoreError)
        self.assertIn("rename", str(outcome))

    def test_conflicting_launch_scope_resets_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "scope failure"):
            with self.store.launch_recovery_scope(target_name="unrelated"):
                self.assertEqual(
                    {profile.name for profile in self.store.scan()[0]},
                    {"unrelated"},
                )
                with self.assertRaises(StoreError):
                    self.store.get_readonly("old")
                with self.assertRaises(StoreError):
                    self.store.get("new")
                raise RuntimeError("scope failure")

        self.assertIsNone(self.store._launch_rename_conflict.get())
        with self.assertRaises(StoreError):
            self.store.scan()
