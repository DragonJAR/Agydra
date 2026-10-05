"""Regression tests for issues found running the real CLI end-to-end.

Two runtime bugs surfaced while exercising every subcommand with a live
shim (see cmd_list / cmd_import):

1. ``list`` header fusion: with a single short profile name (e.g. "work",
   width=4), ``{'PROFILE':<width+2}`` produced a 6-char field — shorter
   than the label itself — so the header rendered "PROFILEEMAIL".
2. ``import`` onto a profile that already has data crashed with a raw
   ``OSError: [Errno 66] Directory not empty`` from the tmp->data rename
   instead of an actionable error. The same rename would also fail on
   Windows for an empty-but-existing data/ (os.rename FileExistsError),
   fixed by rmdir'ing the empty dir before the swap.
"""
import argparse
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store import Store, StoreError, read_json_object
import shutil as shutil_mod

import ui
from ui import strip_ansi

from conftest import BaseCase


class TestListColumnsAlignWithColor(BaseCase):
    """Colorized list must keep columns aligned.

    Painted cells embed ANSI bytes that an f-string ``:<width`` spec counts
    as visible width, so a green ``authenticated`` cell rendered with 20
    bytes of padding and 13 visible chars shifted every following column.
    The fix pads by visible width (``ui.pad``) and moves the filler outside
    the color span.
    """

    def setUp(self):
        super().setUp()
        self.store = Store()
        # NO_COLOR wins over FORCE_COLOR. Drop an inherited NO_COLOR so this
        # class actually paints; BaseCase.tearDown restores the process env.
        os.environ.pop("NO_COLOR", None)
        os.environ["FORCE_COLOR"] = "1"

    def test_row_columns_align_with_header_under_color(self):
        self.store.create("work")
        data_dir = self.store.profile_data_dir("work") / "antigravity-cli"
        data_dir.mkdir(parents=True)
        (data_dir / "antigravity-oauth-token").write_text(
            '{"access_token": "t"}', encoding="utf-8"
        )
        lines = self.capture_list()
        plain_header, plain_row = strip_ansi(lines[0]), strip_ansi(lines[1])
        auth_at = plain_header.index("AUTH")
        default_at = plain_header.index("DEFAULT")
        busy_at = plain_header.index("BUSY")
        last_at = plain_header.index("LAST USED")
        self.assertEqual(
            plain_row.index("authenticated"), auth_at,
            "AUTH column misaligned under color",
        )
        self.assertEqual(plain_row[default_at], "*")
        self.assertEqual(plain_row[busy_at], "-")
        self.assertEqual(plain_row[last_at], "-")

    def test_ansi_padding_places_filler_before_trailing_reset(self):
        painted = ui.paint("authenticated", "green")
        cell = ui.pad(painted, 20)
        self.assertTrue(cell.endswith(ui.RESET))
        self.assertEqual(len(ui.strip_ansi(cell)), 20)
        filler = " " * (20 - len("authenticated"))
        self.assertEqual(cell, painted[:-len(ui.RESET)] + filler + ui.RESET)

    def capture_list(self):
        import cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_list(self.store, None)
        self.assertEqual(rc, 0)
        return buf.getvalue().splitlines()


class TestListHeaderNeverFusesColumns(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_short_profile_name_keeps_header_separated(self):
        import cli

        self.store.create("work")
        lines = self.capture_list()
        header = lines[0]
        self.assertIn(f"{cli._PROFILE_LABEL}  EMAIL", header)

    def test_long_profile_name_still_aligned(self):
        import cli

        self.store.create("a" * 30)
        lines = self.capture_list()
        header = lines[0]
        row = lines[1]
        self.assertEqual(header.index("EMAIL"), row.index("-"))
        self.assertLess(header.index("EMAIL"), header.index("AUTH"))

    def capture_list(self):
        import cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_list(self.store, None)
        self.assertEqual(rc, 0)
        return buf.getvalue().splitlines()


class TestDeleteSkipsPurgeWhenBackupFails(BaseCase):
    """``keychain.purge_profile_slot`` must only run once a requested
    backup has actually landed on disk -- a disk-full/corrupt-zip failure
    during the backup must not also purge a keychain-only profile's only
    credential, or the failed delete would still lose it for good."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("kc")

    def test_backup_failure_leaves_profile_and_secret_untouched(self):
        import cli
        import keychain
        from unittest import mock

        keychain.save_profile_slot(self.store, "kc", b"secret-bytes")

        class Args:
            ref = "kc"
            force = True
            no_backup = False

        with mock.patch.object(
            Store, "_write_backup", side_effect=OSError("disk full")
        ), mock.patch.object(keychain, "purge_profile_slot") as purge:
            with self.assertRaises(OSError):
                cli.cmd_delete(self.store, Args())

        purge.assert_not_called()
        self.assertTrue(self.store.exists("kc"))
        self.assertEqual(keychain.load_profile_slot(self.store, "kc"), b"secret-bytes")


class TestDeletePurgesUnderProfileLock(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("kc")

    def test_same_name_cannot_be_recreated_during_keychain_purge(self):
        import cli
        import keychain
        import locks
        from unittest import mock

        if keychain.fcntl is None:
            self.skipTest("swap.lock requires POSIX flock")

        keychain.save_profile_slot(self.store, "kc", b"old-credential")
        original_purge = keychain.purge_profile_slot
        original_serialized_access = keychain.serialized_access
        profile_lock_states = []
        purge_lock_states = []
        recreation_blocked = []
        recreated_credentials = []

        @contextlib.contextmanager
        def inspect_serialized_access(store):
            handle = locks.try_lock(store, "kc")
            profile_lock_states.append(handle is None)
            if handle is not None:
                handle.release()
            with original_serialized_access(store):
                yield

        def recreate_during_purge(store, name):
            handle = locks.try_lock(store, name)
            purge_lock_states.append(handle is None)
            if handle is not None:
                handle.release()
            try:
                store.create(name)
            except StoreError:
                recreation_blocked.append(name)
            else:
                keychain.save_profile_slot(store, name, b"new-credential")
                recreated_credentials.append(name)
            original_purge(store, name)

        class Args:
            ref = "kc"
            force = True
            no_backup = True

        with mock.patch.object(
            cli.keychain, "serialized_access", side_effect=inspect_serialized_access
        ), mock.patch.object(
            cli.keychain, "purge_profile_slot", side_effect=recreate_during_purge
        ), contextlib.redirect_stdout(io.StringIO()):
            result = cli.cmd_delete(self.store, Args())

        self.assertEqual(result, 0)
        self.assertEqual(profile_lock_states, [True])
        self.assertEqual(purge_lock_states, [True])
        self.assertEqual(recreation_blocked, ["kc"])
        self.assertEqual(recreated_credentials, [])
        self.assertFalse(self.store.exists("kc"))

        self.store.create("kc")
        keychain.save_profile_slot(self.store, "kc", b"new-credential")
        self.assertEqual(keychain.load_profile_slot(self.store, "kc"), b"new-credential")

    def test_purge_exception_reports_completed_delete_and_backup(self):
        import cli
        from unittest import mock

        data_file = self.store.profile_data_dir("kc") / "session.json"
        data_file.write_text("profile data", encoding="utf-8")

        class Args:
            ref = "kc"
            force = True
            no_backup = False

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            cli.keychain, "purge_profile_slot", side_effect=OSError("keychain offline")
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cli.cmd_delete(self.store, Args())

        self.assertEqual(result, 1)
        self.assertFalse(self.store.exists("kc"))
        self.assertIn("backup saved:", stdout.getvalue())
        self.assertIn("deleted profile: kc", stdout.getvalue())
        self.assertIn("was deleted, but keychain purge failed", stderr.getvalue())
        self.assertTrue(list(self.store.backups_dir.glob("kc-*.zip")))


class TestImportGuards(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.source = self.fake_home / ".gemini"
        self.source.mkdir(parents=True, exist_ok=True)
        (self.source / "settings.json").write_text("{}", encoding="utf-8")

    def test_import_onto_populated_profile_is_actionable(self):
        import cli

        self.store.create("populated")
        data = self.store.profile_data_dir("populated")
        data.mkdir(parents=True, exist_ok=True)
        (data / "leftover.txt").write_text("x", encoding="utf-8")

        class Args:
            ref = "populated"
            source = None

        with self.assertRaises(StoreError) as ctx:
            cli.cmd_import(self.store, Args())
        self.assertIn("already has data", str(ctx.exception))
        self.assertIn("agydra delete populated", str(ctx.exception))
        parent = data.parent
        self.assertFalse(
            [p for p in parent.iterdir() if p.name.startswith(".import-")],
            "rejected import left temp litter",
        )

    def test_import_onto_locked_profile_reports_the_live_session(self):
        import cli
        import locks
        from unittest import mock

        self.store.create("held")
        data = self.store.profile_data_dir("held")
        data.mkdir(parents=True, exist_ok=True)
        (data / "leftover.txt").write_text("x", encoding="utf-8")
        handle = locks.try_lock(self.store, "held")
        try:
            class Args:
                ref = "held"
                source = None

            with mock.patch.object(cli.shutil, "copytree") as copytree:
                with self.assertRaises(StoreError) as ctx:
                    cli.cmd_import(self.store, Args())
        finally:
            handle.release()
        self.assertNotIn("already has data", str(ctx.exception))
        self.assertEqual((data / "leftover.txt").read_text(encoding="utf-8"), "x")
        copytree.assert_not_called()

    def test_import_calls_keychain_capture_after_copy(self):
        """Wiring guard: `import` must ask the keychain module to consider
        capturing the shared slot only AFTER the data is on disk (so the
        identity check has a fresh on-disk token to compare against)."""
        import cli
        from unittest import mock

        self.store.create("fresh2")
        data = self.store.profile_data_dir("fresh2")

        class Args:
            ref = "fresh2"
            source = None

        with mock.patch.object(cli.keychain, "capture_shared_slot_for_import") as cap:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_import(self.store, Args())
        self.assertEqual(rc, 0)
        cap.assert_called_once_with(self.store, "fresh2", data)

    def test_import_holds_profile_lock_through_publication_and_capture(self):
        import cli
        import locks
        from unittest import mock

        self.store.create("locked-import")
        data = self.store.profile_data_dir("locked-import")
        original_try_lock = locks.try_lock
        original_rename = cli.rename_dir_with_retry
        original_copytree = cli.shutil.copytree
        lock_checks = []
        copytree_checks = []
        swap_active = [False]
        events = []

        def assert_lock_is_held():
            handle = original_try_lock(self.store, "locked-import")
            lock_checks.append(handle is None)
            if handle is not None:
                handle.release()

        def inspect_publication(source, destination):
            assert_lock_is_held()
            self.assertTrue(swap_active[0])
            events.append("publication")
            return original_rename(source, destination)

        def inspect_copy(source, destination, *args, **kwargs):
            if not copytree_checks:
                assert_lock_is_held()
                self.assertTrue(swap_active[0])
                events.append("copy")
                copytree_checks.append(True)
            return original_copytree(source, destination, *args, **kwargs)

        def inspect_capture(store, name, data_dir):
            assert_lock_is_held()
            self.assertTrue(swap_active[0])
            events.append("capture")
            self.assertEqual(data_dir, data)
            self.assertTrue((data_dir / "settings.json").is_file())

        @contextlib.contextmanager
        def inspect_swap_guard(store):
            assert_lock_is_held()
            self.assertEqual(list(data.iterdir()), [])
            swap_active[0] = True
            events.append("guard entered")
            try:
                yield
            finally:
                assert_lock_is_held()
                swap_active[0] = False
                events.append("guard released")

        class Args:
            ref = "locked-import"
            source = None

        with mock.patch.object(
            cli, "rename_dir_with_retry", side_effect=inspect_publication
        ), mock.patch.object(
            cli.shutil, "copytree", side_effect=inspect_copy
        ), mock.patch.object(
            cli.keychain, "capture_shared_slot_for_import", side_effect=inspect_capture
        ), mock.patch.object(
            cli.keychain, "serialized_access", side_effect=inspect_swap_guard
        ):
            result = cli.cmd_import(self.store, Args())

        self.assertEqual(result, 0)
        self.assertEqual(copytree_checks, [True])
        self.assertEqual(lock_checks, [True, True, True, True, True])
        self.assertEqual(events, ["guard entered", "copy", "publication", "capture", "guard released"])
        self.assertFalse(swap_active[0])
        released = original_try_lock(self.store, "locked-import")
        self.assertIsNotNone(released)
        released.release()

    def _assert_guard_failure_leaves_import_untouched(self, failure):
        import cli
        import locks
        from unittest import mock

        name = "guard-failure"
        self.store.create(name)
        data = self.store.profile_data_dir(name)
        profile_path = self.store.profile_meta_path(name)
        profile_before = profile_path.read_bytes()
        data_before = data.stat()
        source_file = self.source / "settings.json"
        source_before = source_file.read_bytes()
        profile_locks_at_swap = []

        def deny_swap(store):
            self.assertIs(store, self.store)
            handle = locks.try_lock(store, name)
            profile_locks_at_swap.append(handle is None)
            if handle is not None:
                handle.release()
            self.assertEqual(list(data.iterdir()), [])
            raise failure

        class Args:
            ref = name
            source = str(self.source)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            cli.keychain, "serialized_access", side_effect=deny_swap
        ) as serialized, mock.patch.object(
            cli.keychain, "capture_shared_slot_for_import"
        ) as capture, mock.patch.object(
            cli.shutil, "copytree"
        ) as copytree, mock.patch.object(
            cli, "rename_dir_with_retry"
        ) as publish, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(type(failure)) as caught:
                cli.cmd_import(self.store, Args())

        self.assertIs(caught.exception, failure)
        serialized.assert_called_once_with(self.store)
        capture.assert_not_called()
        copytree.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(profile_locks_at_swap, [True])
        self.assertEqual(source_file.read_bytes(), source_before)
        self.assertEqual(profile_path.read_bytes(), profile_before)
        self.assertEqual(list(data.iterdir()), [])
        self.assertEqual((data.stat().st_dev, data.stat().st_ino), (data_before.st_dev, data_before.st_ino))
        self.assertEqual(list(data.parent.glob(".import-*")), [])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        released = locks.try_lock(self.store, name)
        self.assertIsNotNone(released)
        released.release()

    def test_import_refuses_busy_swap_before_copy(self):
        import cli

        self._assert_guard_failure_leaves_import_untouched(
            cli.keychain.KeychainBusyError("shared keychain slot busy")
        )

    def test_import_aborts_when_swap_lock_cannot_be_acquired(self):
        self._assert_guard_failure_leaves_import_untouched(
            OSError("swap lock unavailable")
        )

    def test_import_onto_empty_profile_succeeds(self):
        import cli

        self.store.create("fresh")
        data = self.store.profile_data_dir("fresh")

        class Args:
            ref = "fresh"
            source = None

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_import(self.store, Args())
        self.assertEqual(rc, 0)
        self.assertTrue((data / "settings.json").is_file())
        self.assertFalse(
            [p for p in data.parent.iterdir() if p.name.startswith(".import-")],
            "completed import left temp litter",
        )


class TestIsolationErrorPointsToDoctorFix(BaseCase):
    """When login (or any launch) trips the real-dir isolation guard, the
    user must be told the automatic recovery path, not just "remove it
    manually"."""

    def test_login_error_message_mentions_doctor_fix(self):
        import isolation
        import platforms

        self.store = Store()
        self.store.create("alpha")
        data_dir = self.store.profile_data_dir("alpha")
        isolation.build_overlay("alpha", data_dir, self.store.root)
        link = self.store.overlays_dir / "alpha" / platforms.AGY_DATA_DIR_NAME
        link.unlink()
        link.mkdir()  # the alpha-class breakage: real dir where link belongs

        result = self._run_cli("login", "alpha")
        combined = result.stdout + result.stderr
        self.assertIn("doctor --fix", combined,
                      "isolation guard must point to `agydra doctor --fix`")
        self.assertNotEqual(result.returncode, 0)


class TestDashedSubcommandDispatch(BaseCase):
    """``--help``/``-h`` and ``--version`` have long accepted a dashed form
    alongside their bare subcommand name; every other subcommand did not,
    so a natural guess like ``agydra --usage`` matched nothing and fell
    through to launcher mode -- trying to resolve/launch a profile using
    "--usage" as noise, which surfaced as an unrelated busy-profile error
    instead of running the usage report.

    The single-dash form (``agydra -usage``) had the same gap: it fell
    through to launcher mode, where it was parsed as an unrecognized
    short-flag bundle instead of dispatching."""

    def test_dashed_form_dispatches_the_same_subcommand_as_the_bare_name(self):
        self.store = Store()
        self.store.create("alpha")
        bare = self._run_cli("list")
        dashed = self._run_cli("--list")
        self.assertEqual(strip_ansi(bare.stdout), strip_ansi(dashed.stdout))
        self.assertEqual(bare.returncode, dashed.returncode)

    def test_dashed_usage_ignores_a_busy_profile_instead_of_falling_through_to_launcher_mode(self):
        import locks

        self.store = Store()
        self.store.create("alpha")
        handle = locks.try_lock(self.store, "alpha")
        try:
            result = self._run_cli("--usage")
        finally:
            handle.release()
        combined = result.stdout + result.stderr
        self.assertNotIn("is busy", combined)

    def test_single_dash_form_dispatches_the_same_subcommand_as_the_bare_name(self):
        self.store = Store()
        self.store.create("alpha")
        bare = self._run_cli("list")
        single_dashed = self._run_cli("-list")
        self.assertEqual(strip_ansi(bare.stdout), strip_ansi(single_dashed.stdout))
        self.assertEqual(bare.returncode, single_dashed.returncode)

    def test_single_dash_usage_ignores_a_busy_profile_instead_of_falling_through_to_launcher_mode(self):
        import locks

        self.store = Store()
        self.store.create("alpha")
        handle = locks.try_lock(self.store, "alpha")
        try:
            result = self._run_cli("-usage")
        finally:
            handle.release()
        combined = result.stdout + result.stderr
        self.assertNotIn("is busy", combined)

    def test_single_dash_dispatches_every_canonical_name_and_every_alias(self):
        """Exhaustive over the real vocabulary table (DRY, self-updating):
        every canonical subcommand name and every alias must dispatch
        identically whether spelled bare, ``--<name>`` or ``-<name>``. Using
        ``--help`` on each subcommand keeps this side-effect-free (no store
        mutation, no real launch) while still exercising the exact same
        dispatch path a real invocation would take."""
        import vocab

        self.store = Store()
        self.store.create("alpha")
        for canonical, aliases in vocab.SUBCOMMAND_ALIASES.items():
            for name in (canonical, *aliases):
                with self.subTest(name=name):
                    bare = self._run_cli(name, "--help")
                    single_dashed = self._run_cli("-" + name, "--help")
                    double_dashed = self._run_cli("--" + name, "--help")
                    self.assertEqual(
                        strip_ansi(bare.stdout), strip_ansi(single_dashed.stdout)
                    )
                    self.assertEqual(bare.returncode, single_dashed.returncode)
                    self.assertEqual(
                        strip_ansi(bare.stdout), strip_ansi(double_dashed.stdout)
                    )
                    self.assertEqual(bare.returncode, double_dashed.returncode)

    def test_single_dash_launcher_flags_are_unaffected(self):
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")
        result = self._run_cli("-n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("profile", strip_ansi(result.stdout))

    def test_single_dash_launcher_bundles_are_never_resolved_as_subcommands(self):
        """Single-dash launcher bundles (boolean-only, value-taking, or
        mixed) must never resolve to a subcommand -- dispatch must defer to
        launcher-mode bundle parsing for every one of them."""
        import cli

        for token in ("-n", "-r", "-nr", "-rp", "-rn", "-b/opt/agy", "-nrf"):
            with self.subTest(token=token):
                self.assertIsNone(cli._resolve_subcommand(token))

    def test_single_dash_launcher_bundles_still_launch_end_to_end(self):
        self.store = Store()
        self.store.create("alpha")
        self.store.create("beta")
        for name in ("alpha", "beta"):
            cli_dir = self.store.profile_data_dir(name) / "antigravity-cli"
            cli_dir.mkdir(parents=True, exist_ok=True)
            (cli_dir / "antigravity-oauth-token").write_text(
                '{"access_token": "tok"}', encoding="utf-8"
            )
        result = self._run_cli("-nr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile", strip_ansi(result.stdout))


class TestLateFlagWarningNoDuplicates(BaseCase):
    """Regression: when an already-consumed flag repeats and swallows the rest
    of argv into agy's args, warnings for flags caught in that tail (such as -b
    or boolean flags -n/-r/-f) must be emitted EXACTLY ONCE, never duplicated
    by a redundant second pass over raw in main()."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")

    def test_repeated_flag_swallowing_value_flag_warns_exactly_once(self):
        # -p alpha consumes profile; -p beta repeats and is itself the
        # first forwarded flag the note must name; -b /opt/fake follows it
        result = self._run_cli("-p", "alpha", "-p", "beta", "-b", "/opt/fake")
        needle = "'-p beta' was passed to agy, not agydra"
        self.assertIn(needle, result.stderr)
        self.assertEqual(
            result.stderr.count(needle), 1,
            f"Expected exactly 1 warning for the forwarded repeat, got:\n{result.stderr}",
        )

    def test_repeated_flag_swallowing_boolean_flag_warns_exactly_once(self):
        # -p alpha consumes profile; -p beta repeats, swallowing -n
        # (This was the original bug that launched without dry-run)
        result = self._run_cli("-p", "alpha", "-p", "beta", "-n")
        needle = "'-p beta' was passed to agy, not agydra"
        self.assertIn(needle, result.stderr)
        self.assertEqual(
            result.stderr.count(needle), 1,
            f"Expected exactly 1 warning for the forwarded repeat, got:\n{result.stderr}",
        )

    def test_late_flag_after_non_flag_still_warns_exactly_once(self):
        # General case: -p after a non-flag token
        result = self._run_cli("chat", "-p", "alpha", "-n")
        needle = "'-p alpha' was passed to agy, not agydra"
        self.assertIn(needle, result.stderr)
        self.assertEqual(
            result.stderr.count(needle), 1,
            f"Expected exactly 1 warning for late -p, got:\n{result.stderr}",
        )

    def test_consume_launch_flags_returns_swallowed_flag(self):
        import cli

        flags_no_swallow = cli._consume_launch_flags(["-p", "alpha", "chat"])
        self.assertFalse(flags_no_swallow.swallowed)
        self.assertIsNone(flags_no_swallow.swallowed_index)
        values, rest = flags_no_swallow
        self.assertEqual(values["profile"], "alpha")
        self.assertEqual(rest, ["chat"])

        flags_swallowed = cli._consume_launch_flags(["-p", "alpha", "-p", "beta", "-n"])
        self.assertTrue(flags_swallowed.swallowed)
        self.assertEqual(flags_swallowed.swallowed_index, 2)
        values, rest = flags_swallowed
        self.assertEqual(values["profile"], "alpha")
        self.assertEqual(rest, ["-p", "beta", "-n"])


class TestLateFlagsDashDashOrder(BaseCase):
    """_warn_late_flags must evaluate tokens in command-line order and break on --,
    never early-returning on -- and missing flags before it."""

    def _warn(self, raw, include_booleans=False):
        import cli
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            cli._warn_late_flags(
                {k: None for k in cli._LAUNCH_FLAGS},
                raw,
                include_booleans=include_booleans,
            )
        return buf.getvalue()

    def test_flag_before_dash_dash_warns(self):
        out = self._warn(["chat", "-b", "/opt/fake", "--", "prompt"])
        self.assertIn("'-b /opt/fake' was passed to agy, not agydra", out)

    def test_flag_after_dash_dash_is_ignored(self):
        out = self._warn(["chat", "--", "-p", "work"])
        self.assertEqual(out, "")


class TestReadOnlyRandomDryRun(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("cc", engine="claude")
        os.environ["AGYDRA_CLAUDE_BIN"] = sys.executable

    def test_random_claude_dry_run_uses_read_only_plan_without_native_probe(self):
        import cli

        original_build_plan = cli.runner.build_plan
        with mock.patch.object(
            cli.account,
            "claude_auth_status",
            side_effect=AssertionError("random dry-run must not probe native auth"),
        ) as native_probe, mock.patch.object(
            cli.runner, "build_plan", wraps=original_build_plan
        ) as build_plan, mock.patch.object(
            cli.runner, "run", return_value=0
        ) as run:
            code = cli.main(["--random", "--engine", "claude", "--dry-run"])

        self.assertEqual(code, 0)
        native_probe.assert_not_called()
        self.assertTrue(build_plan.call_args.kwargs["read_only"])
        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertEqual(run.call_args.args[0].profile, "cc")


class TestStatusDryRunIsReadOnly(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("work")

    def test_status_dry_run_does_not_recover_a_pending_rename_journal(self):
        import cli

        journal = self.store.rename_journal_path
        journal_bytes = b"pending rename journal must be preserved\x00\xff"
        journal.write_bytes(journal_bytes)
        args = argparse.Namespace(dry_run=True, ref="work", engine=None)

        with mock.patch.object(
            Store,
            "_recover_pending_rename",
            side_effect=AssertionError("status --dry-run must not recover state"),
        ) as recover, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.cmd_status(self.store, args)

        self.assertEqual(code, 1)
        self.assertIn("recovery is pending", strip_ansi(err.getvalue()))
        recover.assert_not_called()
        self.assertEqual(journal.read_bytes(), journal_bytes)

    def test_status_dry_run_plans_with_the_read_only_flag(self):
        import cli

        args = argparse.Namespace(dry_run=True, ref="work", engine=None)
        with mock.patch.object(
            cli.runner, "build_plan", wraps=cli.runner.build_plan
        ) as build_plan, contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_status(self.store, args)

        self.assertEqual(code, 0)
        self.assertTrue(build_plan.call_args.kwargs["read_only"])


class TestAssertFreeActionContext(BaseCase):
    """_assert_free should customize the error message according to the action."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("busy-prof")

    def test_assert_free_messages(self):
        import cli
        import locks
        handle = locks.try_lock(self.store, "busy-prof")
        try:
            with self.assertRaises(StoreError) as ctx:
                cli._assert_free(self.store, "busy-prof", "logging in")
            self.assertIn("before logging in", str(ctx.exception))

            with self.assertRaises(StoreError) as ctx:
                cli._assert_free(self.store, "busy-prof", "sharing config")
            self.assertIn("before sharing config", str(ctx.exception))

            with self.assertRaises(StoreError) as ctx:
                cli._assert_free(self.store, "busy-prof", "deleting the profile")
            self.assertIn("before deleting the profile", str(ctx.exception))
        finally:
            handle.release()


class TestDeleteTOCTOUGuard(BaseCase):
    """_finish_delete must assert the profile is free right before deleting."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("victim")

    def test_finish_delete_refuses_busy_profile(self):
        import cli
        import locks
        handle = locks.try_lock(self.store, "victim")
        try:
            with self.assertRaises(StoreError) as ctx:
                cli._finish_delete(self.store, "victim", no_backup=True)
            self.assertIn("holder state could not be verified", str(ctx.exception))
            self.assertNotIn("kill", str(ctx.exception))
            self.assertIn("before deleting the profile", str(ctx.exception))
        finally:
            handle.release()

    def test_busy_lock_without_a_verified_lease_never_suggests_kill(self):
        import cli
        import locks
        locks.lock_path(self.store, "victim").write_text("12345", encoding="utf-8")
        handle = locks.try_lock(self.store, "victim")
        try:
            with self.assertRaises(StoreError) as ctx:
                cli._assert_free(self.store, "victim", "deleting the profile")
            message = str(ctx.exception)
            self.assertNotIn("kill", message)
            self.assertNotIn("12345", message)
            self.assertIn("could not be verified", message)
        finally:
            handle.release()


class TestShareConfigDeduplication(BaseCase):
    """_share_config must deduplicate duplicate targets in args.targets."""

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("src")
        self.store.create("target")
        src_data = self.store.profile_data_dir("src")
        src_data.mkdir(parents=True, exist_ok=True)
        (src_data / "settings.json").write_text("{}", encoding="utf-8")

    def test_duplicate_targets_copied_once(self):
        import cli
        copied = cli._share_config(self.store, "src", ["target", "target", "target"])
        self.assertEqual(copied, ["target/settings.json"])

    def test_all_profile_locks_are_held_in_sorted_order_during_copies(self):
        import cli
        import locks
        from unittest import mock

        self.store.create("zeta")
        original_try_lock = locks.try_mutation_lock
        original_copy = cli.atomic_copy
        acquired_names = []
        lock_checks = []

        def observe_try_lock(store, name):
            handle = original_try_lock(store, name)
            if handle is not None:
                acquired_names.append(name)
            return handle

        def inspect_copy(source, destination):
            available = []
            for name in ("src", "target", "zeta"):
                handle = original_try_lock(self.store, name)
                available.append(handle is not None)
                if handle is not None:
                    handle.release()
            lock_checks.append(available)
            return original_copy(source, destination)

        with mock.patch.object(locks, "try_mutation_lock", side_effect=observe_try_lock), mock.patch.object(
            cli, "atomic_copy", side_effect=inspect_copy
        ):
            copied = cli._share_config(self.store, "src", ["zeta", "target"])

        self.assertEqual(copied, ["zeta/settings.json", "target/settings.json"])
        self.assertEqual(acquired_names, ["src", "target", "zeta"])
        self.assertEqual(lock_checks, [[False, False, False], [False, False, False]])

    def test_changed_target_is_revalidated_before_any_copy(self):
        import cli
        import locks
        import shutil
        from unittest import mock

        self.store.create("zeta")
        target_file = self.store.profile_data_dir("target") / "settings.json"
        target_file.write_text("existing", encoding="utf-8")
        original_try_lock = locks.try_mutation_lock
        removed = []

        def remove_last_target_after_lock(store, name):
            handle = original_try_lock(store, name)
            if name == "zeta" and handle is not None:
                shutil.rmtree(store.profile_dir("zeta"))
                removed.append(name)
            return handle

        with mock.patch.object(
            locks, "try_mutation_lock", side_effect=remove_last_target_after_lock
        ), mock.patch.object(cli, "atomic_copy") as copy_file:
            with self.assertRaises(StoreError):
                cli._share_config(self.store, "src", ["target", "zeta"])

        self.assertEqual(removed, ["zeta"])
        copy_file.assert_not_called()
        self.assertEqual(target_file.read_text(encoding="utf-8"), "existing")

    def test_busy_source_or_target_leaves_data_untouched(self):
        import cli
        import locks
        from unittest import mock

        target_file = self.store.profile_data_dir("target") / "settings.json"
        target_file.write_text("existing", encoding="utf-8")
        for busy_name in ("src", "target"):
            handle = locks.try_lock(self.store, busy_name)
            try:
                with mock.patch.object(cli, "atomic_copy") as copy_file:
                    with self.assertRaises(StoreError):
                        cli._share_config(self.store, "src", ["target"])
            finally:
                handle.release()

            copy_file.assert_not_called()
            self.assertEqual(target_file.read_text(encoding="utf-8"), "existing")

    def test_copy_output_order_is_stable_across_process_hash_seeds(self):
        for name in ("mcp.json", "config.toml"):
            (self.store.profile_data_dir("src") / name).write_text(
                "{}", encoding="utf-8"
            )

        outputs = []
        old_seed = os.environ.get("PYTHONHASHSEED")
        try:
            for seed in ("1", "2"):
                os.environ["PYTHONHASHSEED"] = seed
                result = self._run_cli("share-config", "src", "target")
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(
                    [
                        line for line in result.stdout.splitlines()
                        if line.startswith("copied:")
                    ]
                )
        finally:
            if old_seed is None:
                os.environ.pop("PYTHONHASHSEED", None)
            else:
                os.environ["PYTHONHASHSEED"] = old_seed

        expected = [
            "copied: target/settings.json",
            "copied: target/mcp.json",
            "copied: target/config.toml",
        ]
        self.assertEqual(outputs, [expected, expected])


class TestCliCreateWithDanglingDefault(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        config = self.store.load_config()
        config.default_profile = "missing"
        self.store.save_config(config)

    def test_create_succeeds_when_default_profile_is_missing(self):
        result = self._run_cli("create", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("created profile: fresh", result.stdout)
        self.assertTrue(self.store.exists("fresh"))


class TestGlobalLanguageOptionDelimiter(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha")
        os.environ.pop("AGYDRA_LANG", None)
        config = self.store.load_config()
        config.settings["lang"] = "en"
        self.store.save_config(config)

    def test_language_option_after_delimiter_is_forwarded_unchanged(self):
        result = self._run_cli("-n", "--", "--lang", "es")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--lang es", result.stdout)
        self.assertEqual(self.store.load_config().settings.get("lang"), "en")


class TestDoctorFixDeclinedExitCode(BaseCase):
    """Declining confirmation in cmd_doctor --fix must return exit code 1."""

    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_declined_confirm_returns_exit_1(self):
        import cli
        class Args:
            fix = True
            force = False

        from unittest import mock
        with mock.patch("doctor.run_checks", return_value=0), \
             mock.patch("doctor._preview_fixables", return_value=["orphan overlay"]), \
             mock.patch("cli._confirm", return_value=False):
            code = cli.cmd_doctor(self.store, Args())
            self.assertEqual(code, 1)


class TestRenameKeepsProfileLockedThroughKeychainMigration(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("old")

    def test_new_profile_lock_blocks_execution_during_slot_migration(self):
        import cli
        import keychain
        import locks
        from unittest import mock

        if keychain.fcntl is None:
            self.skipTest("swap.lock requires POSIX flock")

        old_slot = keychain.slot_backup_path(self.store, "old")
        old_slot.parent.mkdir(parents=True, exist_ok=True)
        old_slot.write_bytes(b"credential")
        original_rename = keychain.rename_profile_slot
        original_serialize = keychain.serialized_access
        callback_calls = []
        profile_locks_at_callback = []
        locks_at_swap = []

        def inspect_migration(
            store,
            old_name,
            new_name,
            *,
            source_present=None,
            strict=False,
        ):
            callback_calls.append((old_name, new_name))
            acquired = []
            for name in (old_name, new_name):
                handle = locks.try_lock(store, name)
                acquired.append(handle is not None)
                if handle is not None:
                    handle.release()
            profile_locks_at_callback.append(acquired)
            original_rename(
                store,
                old_name,
                new_name,
                source_present=source_present,
                strict=strict,
            )

        @contextlib.contextmanager
        def inspect_swap_lock(store):
            with original_serialize(store):
                profile_handles = [locks.try_lock(store, name) for name in ("old", "new")]
                sequence_handle = locks.try_sequence_lock(store)
                locks_at_swap.append(
                    [handle is None for handle in profile_handles],
                )
                locks_at_swap.append(sequence_handle is not None)
                for handle in profile_handles:
                    if handle is not None:
                        handle.release()
                if sequence_handle is not None:
                    sequence_handle.release()
                yield

        class Args:
            old = "old"
            new = "new"

        with mock.patch.object(
            cli.keychain, "rename_profile_slot", side_effect=inspect_migration
        ), mock.patch.object(cli.keychain, "supported", return_value=True), mock.patch.object(
            cli.keychain, "serialized_access", side_effect=inspect_swap_lock
        ), mock.patch.object(
            cli.keychain, "_ensure_target_keychain", return_value=self.fake_home
        ), mock.patch.object(cli.keychain, "delete_slot"):
            result = cli.cmd_rename(self.store, Args())

        self.assertEqual(result, 0)
        self.assertEqual(callback_calls, [("old", "new")])
        self.assertEqual(profile_locks_at_callback, [[False, False]])
        self.assertEqual(locks_at_swap, [[True, True], True])
        self.assertFalse(old_slot.exists())
        self.assertEqual(
            keychain.load_profile_slot(self.store, "new"), b"credential"
        )


class TestRecoverableKeychainRename(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("old")

    def _crash_during_cli_rename(self, root, point, native_calls):
        child = """import os, sys
from pathlib import Path
import cli
import keychain
from store import Store

root = Path(sys.argv[1])
point = sys.argv[2]
native_calls = Path(sys.argv[3])
store = Store(root=root)
keychain.supported = lambda: True
keychain._ensure_target_keychain = lambda store: Path('/tmp/fake.keychain')
def delete_slot(service, target=None):
    with native_calls.open('a', encoding='utf-8') as output:
        output.write(service + '\\n')
keychain.delete_slot = delete_slot

if point == 'before-callback':
    def crash_before_callback(*args, **kwargs):
        os._exit(71)
    keychain.rename_profile_slot = crash_before_callback
elif point == 'during-migration':
    original_write = keychain.atomic_write_bytes
    def crash_after_target_write(path, data):
        original_write(path, data)
        if Path(path).name == 'new.secret':
            os._exit(72)
    keychain.atomic_write_bytes = crash_after_target_write
elif point == 'after-callback':
    def crash_before_journal_remove(self):
        os._exit(73)
    Store._remove_rename_journal = crash_before_journal_remove

class Args:
    old = 'old'
    new = 'new'

cli.cmd_rename(store, Args())
os._exit(0)
"""
        return subprocess.run(
            [sys.executable, "-c", child, str(root), point, str(native_calls)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _recover_through_cli(self, root, swap_events):
        import cli
        import keychain
        import locks
        from unittest import mock

        store = Store(root=root)
        original_try_lock = locks.try_mutation_lock
        original_try_sequence_lock = locks.try_sequence_lock
        original_serialize_lock = keychain._serialize_lock
        recovery_events = []
        native_calls = []

        def observe_profile_lock(store_arg, name):
            recovery_events.append(("profile", name))
            return original_try_lock(store_arg, name)

        def observe_sequence_lock(store_arg):
            recovery_events.append(("sequence", None))
            return original_try_sequence_lock(store_arg)

        def observe_swap_lock(store_arg):
            recovery_events.append(("swap", None))
            return original_serialize_lock(store_arg)

        with mock.patch.object(cli, "Store", return_value=store), mock.patch.object(
            keychain, "supported", return_value=True
        ), mock.patch.object(
            keychain, "_ensure_target_keychain", return_value=self.fake_home
        ), mock.patch.object(
            keychain, "delete_slot", side_effect=lambda service, target=None: native_calls.append(service)
        ), mock.patch.object(
            keychain, "_serialize_lock", side_effect=observe_swap_lock
        ), mock.patch.object(
            locks, "try_mutation_lock", side_effect=observe_profile_lock
        ), mock.patch.object(
            locks, "try_sequence_lock", side_effect=observe_sequence_lock
        ), contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(["list"])

        self.assertEqual(result, 0)
        expected_lock_order = [
            ("profile", "new"),
            ("profile", "old"),
            ("sequence", None),
        ]
        if keychain.fcntl is not None:
            expected_lock_order.append(("swap", None))
        self.assertEqual(recovery_events[:len(expected_lock_order)], expected_lock_order)
        self.assertEqual(native_calls, [keychain.profile_slot("old"), keychain.profile_slot("new")])
        swap_events.extend(recovery_events)
        return store

    def test_process_crashes_recover_keychain_rename_at_each_boundary(self):
        import keychain

        crash_points = (
            ("before-callback", 71),
            ("during-migration", 72),
            ("after-callback", 73),
        )
        for point, exit_code in crash_points:
            with self.subTest(point=point):
                root = self._tmp / point
                original = Store(root=root)
                original.create("old")
                old_secret = keychain.slot_backup_path(original, "old")
                target_secret = keychain.slot_backup_path(original, "new")
                old_secret.parent.mkdir(parents=True, exist_ok=True)
                old_secret.write_bytes(b"source-credential")
                target_secret.write_bytes(b"stale-target-credential")
                native_log = root / "native-calls.log"
                result = self._crash_during_cli_rename(root, point, native_log)

                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertTrue(original.rename_journal_path.is_file())
                self.assertEqual(
                    read_json_object(original.profile_meta_path("new"))["name"],
                    "new",
                )
                self.assertEqual(original.load_config().default_profile, "new")
                journal = original._read_rename_journal()
                self.assertEqual(journal["recovery_action"]["data"], {"source_present": True})
                if point == "before-callback":
                    self.assertEqual(old_secret.read_bytes(), b"source-credential")
                    self.assertEqual(target_secret.read_bytes(), b"stale-target-credential")
                    self.assertFalse(native_log.exists())
                elif point == "during-migration":
                    self.assertEqual(old_secret.read_bytes(), b"source-credential")
                    self.assertEqual(target_secret.read_bytes(), b"source-credential")
                    self.assertFalse(native_log.exists())
                else:
                    self.assertFalse(old_secret.exists())
                    self.assertEqual(target_secret.read_bytes(), b"source-credential")
                    self.assertEqual(
                        native_log.read_text(encoding="utf-8").splitlines(),
                        [keychain.profile_slot("old"), keychain.profile_slot("new")],
                    )

                observed_locks = []
                reopened = self._recover_through_cli(root, observed_locks)
                self.assertEqual(
                    [(profile.name, profile.seq) for profile in reopened.list()],
                    [("new", 1)],
                )
                self.assertEqual(reopened.default_name(), "new")
                self.assertFalse(reopened.rename_journal_path.exists())
                self.assertFalse(old_secret.exists())
                self.assertEqual(target_secret.read_bytes(), b"source-credential")
                self.assertNotEqual(target_secret.read_bytes(), b"stale-target-credential")

    def test_recovery_purges_stale_target_when_original_slot_was_absent(self):
        import keychain

        root = self._tmp / "absent-source"
        original = Store(root=root)
        original.create("old")
        target_secret = keychain.slot_backup_path(original, "new")
        target_secret.parent.mkdir(parents=True, exist_ok=True)
        target_secret.write_bytes(b"stale-target-credential")
        native_log = root / "native-calls.log"
        result = self._crash_during_cli_rename(root, "before-callback", native_log)

        self.assertEqual(result.returncode, 71, result.stderr)
        journal = original._read_rename_journal()
        self.assertEqual(journal["recovery_action"]["data"], {"source_present": False})
        self.assertEqual(target_secret.read_bytes(), b"stale-target-credential")

        reopened = self._recover_through_cli(root, [])
        self.assertEqual(
            read_json_object(reopened.profile_meta_path("new"))["name"], "new"
        )
        self.assertEqual(reopened.default_name(), "new")
        self.assertFalse(reopened.rename_journal_path.exists())
        self.assertFalse(keychain.slot_backup_path(reopened, "old").exists())
        self.assertFalse(target_secret.exists())

    def test_required_keychain_failures_keep_the_rename_intent(self):
        import cli
        import keychain
        from unittest import mock

        failures = (
            ("swap-lock", "durable-write")
            if keychain.fcntl is not None
            else ("durable-write",)
        )
        for failure in failures:
            with self.subTest(failure=failure):
                root = self._tmp / failure
                store = Store(root=root)
                store.create("old")
                old_slot = keychain.slot_backup_path(store, "old")
                new_slot = keychain.slot_backup_path(store, "new")
                old_slot.parent.mkdir(parents=True, exist_ok=True)
                old_slot.write_bytes(b"source-credential")
                new_slot.write_bytes(b"stale-target-credential")

                class Args:
                    old = "old"
                    new = "new"

                if failure == "swap-lock":
                    keychain_patches = (
                        mock.patch.object(keychain, "supported", return_value=True),
                        mock.patch.object(
                            keychain,
                            "_serialize_lock",
                            side_effect=PermissionError("swap lock unavailable"),
                        ),
                    )
                else:
                    keychain_patches = (
                        mock.patch.object(keychain, "supported", return_value=False),
                        mock.patch.object(
                            keychain,
                            "atomic_write_bytes",
                            side_effect=OSError("slot write unavailable"),
                        ),
                    )

                with keychain_patches[0], keychain_patches[1]:
                    with self.assertRaisesRegex(StoreError, "journal retained"):
                        cli.cmd_rename(store, Args())
                    self.assertTrue(store.rename_journal_path.is_file())
                    self.assertTrue(old_slot.is_file())
                    self.assertEqual(old_slot.read_bytes(), b"source-credential")
                    self.assertEqual(new_slot.read_bytes(), b"stale-target-credential")

                    with self.assertRaisesRegex(StoreError, "journal retained"):
                        store.list()
                    self.assertTrue(store.rename_journal_path.is_file())

                with mock.patch.object(keychain, "supported", return_value=True), \
                        mock.patch.object(
                            keychain, "_ensure_target_keychain", return_value=self.fake_home
                        ), \
                        mock.patch.object(keychain, "delete_slot"):
                    self.assertEqual([profile.name for profile in store.list()], ["new"])

                self.assertFalse(store.rename_journal_path.exists())
                self.assertFalse(old_slot.exists())
                self.assertEqual(new_slot.read_bytes(), b"source-credential")


class TestHelpFormattingRegression(BaseCase):
    def test_colored_help_keeps_plain_text_spacing(self):
        import ui

        old_force = os.environ.get("FORCE_COLOR")
        old_no_color = os.environ.get("NO_COLOR")
        try:
            os.environ["FORCE_COLOR"] = "0"
            os.environ.pop("NO_COLOR", None)
            plain = self._run_cli("help")
            os.environ["FORCE_COLOR"] = "1"
            colored = self._run_cli("help")
        finally:
            if old_force is None:
                os.environ.pop("FORCE_COLOR", None)
            else:
                os.environ["FORCE_COLOR"] = old_force
            if old_no_color is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = old_no_color

        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(colored.returncode, 0, colored.stderr)
        self.assertIn("\x1b[", colored.stdout)
        self.assertEqual(ui.strip_ansi(colored.stdout), plain.stdout)

    def test_doctor_fix_help_does_not_claim_lock_sentinels_are_removed(self):
        result = self._run_cli("doctor", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        flat = " ".join(result.stdout.split())
        self.assertIn("(overlays and keychain files)", flat)
        self.assertIn("backup ZIPs are never scanned or deleted", flat)
        self.assertNotIn("backups)", flat)
        self.assertNotIn("overlays/locks/keychain", flat)


class TestImportRuntimeExclusion(BaseCase):
    """Import must skip engine runtime artifacts (sockets, tmp trees).

    A live ``~/.codex`` holds unix sockets (``ipc/ipc.sock``,
    ``app-server-daemon/*.sock``) and ephemeral ``tmp/arg0`` wrapper trees
    that vanish mid-copy. Without exclusion the import fails with an
    unreadable ``shutil.Error`` tuple wall (or copies dead sockets into
    the profile); with it, only portable configuration lands.
    """

    def setUp(self):
        super().setUp()
        self.store = Store()
        self.source = self.fake_home / ".codex"
        (self.source / "ipc").mkdir(parents=True)
        (self.source / "ipc" / "ipc.sock").write_bytes(b"")
        (self.source / "tmp" / "path" / "codex-arg0XYZ").mkdir(parents=True)
        (self.source / "tmp" / "path" / "codex-arg0XYZ" / "apply_patch").write_bytes(b"")
        (self.source / "app-server-daemon").mkdir()
        (self.source / "app-server-daemon" / "daemon-updater.sock").write_bytes(b"")
        (self.source / "auth.json").write_text("{}", encoding="utf-8")

    def test_codex_runtime_artifacts_are_excluded(self):
        import cli

        self.store.create("cx", engine="codex")

        class Args:
            ref = "cx"
            source = None

        rc = cli.cmd_import(self.store, Args())
        self.assertEqual(rc, 0)
        data = self.store.profile_data_dir("cx", engine="codex")
        self.assertTrue((data / "auth.json").is_file())
        self.assertFalse((data / "ipc").exists())
        self.assertFalse((data / "tmp").exists())
        self.assertFalse((data / "app-server-daemon").exists())

    def test_copytree_failure_is_summarized_not_dumped(self):
        import cli
        from unittest import mock

        self.store.create("cx2", engine="codex")

        class Args:
            ref = "cx2"
            source = None

        err = shutil_mod.Error(
            [("/src/a", "/dst/a", "[Errno 13] denied"), ("/src/b", "/dst/b", "[Errno 2] gone")]
        )
        with mock.patch.object(cli.shutil, "copytree", side_effect=err):
            with self.assertRaises(StoreError) as ctx:
                cli.cmd_import(self.store, Args())
        message = str(ctx.exception)
        self.assertIn("import failed", message)
        self.assertIn("2 entries", message)
        self.assertIn("/src/a", message)
        self.assertNotIn("/src/b", message)
        parent = self.store.profile_data_dir("cx2", engine="codex").parent
        self.assertFalse(
            [p for p in parent.iterdir() if p.name.startswith(".import-")],
            "failed import left temp litter",
        )


class TestSummarizeImportFailure(BaseCase):
    def test_non_list_or_empty_args_are_summarized_without_crashing(self):
        import cli

        for error in (shutil_mod.Error(), shutil_mod.Error("plain reason"), shutil_mod.Error([])):
            with self.subTest(args=error.args):
                message = cli._summarize_import_failure(error, Path("/src"))
                self.assertIn("0 entries could not be copied from /src", message)



class TestLeaseAwareProfileLocks(BaseCase):
    def setUp(self):
        super().setUp()
        import json

        import locks
        import platforms

        self.json = json
        self.locks = locks
        self.platforms = platforms
        self.store = Store()
        self.store.create("work")
        self.store.create("other")
        self.source = self._tmp / "generic-source"
        self.source.mkdir()
        (self.source / "imported.txt").write_text("sandbox", encoding="utf-8")
        (self.store.profile_data_dir("work") / "settings.json").write_text(
            '{"from": "work"}', encoding="utf-8"
        )
        self.holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        self.addCleanup(self._stop_holder)

    def _stop_holder(self):
        self.holder.kill()
        self.holder.wait()

    def _register_live_holder(self, name):
        path = self.locks.lock_path(self.store, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "pid": self.holder.pid,
            "start": self.platforms.process_start_token(self.holder.pid),
        }
        path.write_text(self.json.dumps({"holders": [entry]}), encoding="utf-8")
        return path

    def _run(self, argv):
        import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(argv)
        return status, out.getvalue(), err.getvalue()

    def _assert_lease_intact(self, name, path, before):
        self.assertEqual(path.read_bytes(), before)
        holders = self.locks.lease_holders(self.store, name)
        self.assertEqual([h.pid for h in holders], [self.holder.pid])
        self.assertTrue(self.locks.is_locked(self.store, name))

    def test_import_into_a_profile_with_a_live_holder_is_refused_and_keeps_the_lease(self):
        lease = self._register_live_holder("work")
        before = lease.read_bytes()
        data_dir = self.store.profile_data_dir("work")
        for leftover in list(data_dir.iterdir()):
            leftover.unlink()

        status, out, err = self._run(["import", "work", "--source", str(self.source)])

        self.assertEqual(status, 1, err)
        self.assertIn("live session", err)
        self.assertIn(f"PID {self.holder.pid}", err)
        self.assertNotIn("imported generic data", out)
        self.assertEqual(list(data_dir.iterdir()), [])
        self._assert_lease_intact("work", lease, before)

    def test_share_config_with_a_live_holder_at_the_source_is_refused(self):
        lease = self._register_live_holder("work")
        before = lease.read_bytes()

        status, out, err = self._run(["share-config", "work", "other"])

        self.assertEqual(status, 1, err)
        self.assertIn("live session", err)
        self.assertFalse((self.store.profile_data_dir("other") / "settings.json").exists())
        self._assert_lease_intact("work", lease, before)

    def test_share_config_with_a_live_holder_at_the_destination_is_refused(self):
        lease = self._register_live_holder("other")
        before = lease.read_bytes()

        status, out, err = self._run(["share-config", "work", "other"])

        self.assertEqual(status, 1, err)
        self.assertIn("live session", err)
        self.assertFalse((self.store.profile_data_dir("other") / "settings.json").exists())
        self._assert_lease_intact("other", lease, before)

    def test_share_config_with_live_holders_at_source_and_destination_keeps_both_leases(self):
        source_lease = self._register_live_holder("work")
        target_lease = self._register_live_holder("other")
        source_before, target_before = source_lease.read_bytes(), target_lease.read_bytes()

        status, out, err = self._run(["share-config", "work", "other"])

        self.assertEqual(status, 1, err)
        self.assertIn("live session", err)
        self._assert_lease_intact("work", source_lease, source_before)
        self._assert_lease_intact("other", target_lease, target_before)

    def test_idle_profiles_still_import_and_share(self):
        status, out, err = self._run(["share-config", "work", "other"])
        self.assertEqual(status, 0, err)
        self.assertTrue((self.store.profile_data_dir("other") / "settings.json").is_file())
        for leftover in list(self.store.profile_data_dir("work").iterdir()):
            leftover.unlink()
        status, out, err = self._run(["import", "work", "--source", str(self.source)])
        self.assertEqual(status, 0, err)
        self.assertTrue((self.store.profile_data_dir("work") / "imported.txt").is_file())

    def test_create_succeeds_without_copying_settings_from_a_busy_default(self):
        lease = self._register_live_holder("work")
        before = lease.read_bytes()

        status, out, err = self._run(["create", "fresh"])

        self.assertEqual(status, 0, err)
        self.assertIn("created profile: fresh", out)
        self.assertIn("settings not copied from default profile 'work'", err)
        self.assertIn("live session", err)
        self.assertFalse((self.store.profile_data_dir("fresh") / "settings.json").exists())
        self._assert_lease_intact("work", lease, before)

    def test_create_does_not_mask_a_copy_failure_that_is_not_a_busy_profile(self):
        import cli

        with mock.patch.object(
            cli, "atomic_copy", side_effect=OSError("disk full")
        ), self.assertRaises(OSError):
            cli.cmd_create(self.store, argparse.Namespace(name="fresh", engine="agy", description=""))

    def test_create_does_not_mask_a_non_busy_store_error_from_the_copy(self):
        import cli

        with mock.patch.object(
            cli, "_share_config", side_effect=StoreError("cannot safely share config")
        ), self.assertRaises(StoreError):
            cli.cmd_create(self.store, argparse.Namespace(name="fresh", engine="agy", description=""))

    def test_an_unreadable_lease_registry_is_reported_as_unverified_and_still_refuses(self):
        import cli

        path = self.locks.lock_path(self.store, "work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not json")
        before = path.read_bytes()

        with self.assertRaises(cli.ProfileBusyError) as ctx:
            cli._assert_free(self.store, "work", "importing into it")

        message = str(ctx.exception)
        self.assertIn("could not be verified", message)
        self.assertNotIn("a live session", message)
        self.assertEqual(path.read_bytes(), before)

    def test_assert_free_names_a_pid_only_for_verified_holders(self):
        import cli
        from types import SimpleNamespace

        def holder(pid, start):
            return SimpleNamespace(pid=pid, start=start)

        cases = (
            ([holder(11, "t1"), holder(22, "t2")], ["2 live sessions", "first PID 11", "kill 11"], []),
            ([holder(11, None), holder(22, None)], ["2 live sessions", "unverified"], ["kill", "PID"]),
            ([holder(11, None)], ["a live session", "unverified"], ["kill", "PID"]),
        )
        for holders, expected, forbidden in cases:
            with self.subTest(holders=[(h.pid, h.start) for h in holders]):
                with mock.patch.object(
                    cli.locks, "is_locked", return_value=True
                ), mock.patch.object(cli.locks, "lease_holders", return_value=holders):
                    with self.assertRaises(cli.ProfileBusyError) as ctx:
                        cli._assert_free(self.store, "work", "importing into it")
                message = str(ctx.exception)
                for text in expected:
                    self.assertIn(text, message)
                for text in forbidden:
                    self.assertNotIn(text, message)

    def test_profile_lock_helper_reports_a_lock_error_as_unsafe_to_proceed(self):
        import cli

        with mock.patch.object(
            cli.locks, "try_mutation_lock", side_effect=cli.locks.LockError("registry unreadable")
        ):
            with self.assertRaisesRegex(StoreError, "cannot safely proceed") as ctx:
                cli._acquire_profile_lock(self.store, "work", "importing into it")
        self.assertNotIsInstance(ctx.exception, cli.ProfileBusyError)
        self.assertIn("registry unreadable", str(ctx.exception))

    def test_profile_lock_helper_uses_the_lease_aware_mutation_lock(self):
        import cli

        with mock.patch.object(
            cli.locks, "try_lock", side_effect=AssertionError("legacy lock")
        ), mock.patch.object(
            cli.locks, "try_mutation_lock", return_value=mock.Mock()
        ) as mutation:
            handle = cli._acquire_profile_lock(self.store, "work", "importing into it")
        mutation.assert_called_once_with(self.store, "work")
        self.assertIsNotNone(handle)
