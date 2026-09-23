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
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store import Store, StoreError  # noqa: E402

from conftest import BaseCase  # noqa: E402


class TestListHeaderNeverFusesColumns(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_short_profile_name_keeps_header_separated(self):
        self.store.create("work")
        lines = self.capture_list()
        header = lines[0]
        # The EMAIL column must start as its own token, not fused onto
        # PROFILE ("PROFILEEMAIL" was the regression).
        self.assertIn("PROFILE  EMAIL", header)

    def test_long_profile_name_still_aligned(self):
        self.store.create("a" * 30)
        lines = self.capture_list()
        header = lines[0]
        # Rows must share the header's column offsets: the EMAIL column
        # starts at the same index in both (the width variable is shared).
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
        # Actionable message names the profile and the escape hatch.
        self.assertIn("already has data", str(ctx.exception))
        self.assertIn("agydra delete populated", str(ctx.exception))
        # Rejection happens before mkdtemp: no tmp litter in profiles/.
        parent = data.parent
        self.assertFalse(
            [p for p in parent.iterdir() if p.name.startswith(".import-")],
            "rejected import left temp litter",
        )

    def test_import_onto_locked_profile_reports_the_live_session(self):
        # Lock precedence: a profile with BOTH a live lock and data must
        # fail on the lock (session owns the profile), not on the data
        # guard — the data message suggests `delete`, which would also be
        # refused while locked.
        import cli, locks

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
        # The empty pre-created data/ was rmdir'd before the rename swap.
        self.assertFalse(
            [p for p in data.parent.iterdir() if p.name.startswith(".import-")],
            "completed import left temp litter",
        )
