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
import contextlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store import Store, StoreError

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
        self.store.create("work")
        lines = self.capture_list()
        header = lines[0]
        self.assertIn("PROFILE  EMAIL", header)

    def test_long_profile_name_still_aligned(self):
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

        self.store.create("held")
        data = self.store.profile_data_dir("held")
        data.mkdir(parents=True, exist_ok=True)
        (data / "leftover.txt").write_text("x", encoding="utf-8")
        handle = locks.try_lock(self.store, "held")
        try:
            class Args:
                ref = "held"
                source = None

            with self.assertRaises(StoreError) as ctx:
                cli.cmd_import(self.store, Args())
        finally:
            handle.release()
        self.assertNotIn("already has data", str(ctx.exception))

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
        # -p alpha consumes profile; -p beta repeats, swallowing -b /opt/fake
        result = self._run_cli("-p", "alpha", "-p", "beta", "-b", "/opt/fake")
        needle = "'-b /opt/fake' was passed to agy, not agydra"
        self.assertIn(needle, result.stderr)
        self.assertEqual(
            result.stderr.count(needle), 1,
            f"Expected exactly 1 warning for late -b, got:\n{result.stderr}",
        )

    def test_repeated_flag_swallowing_boolean_flag_warns_exactly_once(self):
        # -p alpha consumes profile; -p beta repeats, swallowing -n
        # (This was the original bug that launched without dry-run)
        result = self._run_cli("-p", "alpha", "-p", "beta", "-n")
        needle = "'-n' was passed to agy, not agydra"
        self.assertIn(needle, result.stderr)
        self.assertEqual(
            result.stderr.count(needle), 1,
            f"Expected exactly 1 warning for late -n, got:\n{result.stderr}",
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
            self.assertIn("has a live session", str(ctx.exception))
            self.assertIn("before deleting the profile", str(ctx.exception))
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
