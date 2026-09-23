"""Bug-driven regressions for two invariants:

1. ``agydra -nr -p lab`` must select profile ``lab``. Before the bundle fix,
   the boolean bundle ``-nr`` was not recognized, so the extractor stopped
   there and forwarded ``-p lab`` to agy (agy itself uses ``-p`` for
   ``--print``), launching the default profile silently.

2. Profile names that collide with a subcommand or alias (``status``,
   ``ls``, ``mv``, ...) must be refused at create/rename time: bare
   ``agydra <name> ...`` always runs the subcommand, so such a profile
   would be permanently shadowed in the shell.
"""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import (
    _consume_launch_flags,
    _match_flag,
    _warn_late_flags,
)
from store import Store, StoreError

from conftest import BaseCase


class TestShortFlagBundles(unittest.TestCase):
    """The matcher's bundle path must mirror getopt: bools chain, a value
    flag at the end of the bundle takes the next token, inline values still
    work, and any unknown letter rejects the WHOLE token."""

    def test_boolean_bundle_only(self):
        m = _match_flag("-nr")
        self.assertIsNotNone(m)
        keys = [k for k, _, _ in m]
        self.assertEqual(keys, ["dry-run", "random"])

    def test_boolean_bundle_then_value_next_token(self):
        m = _match_flag("-rp")
        self.assertIsNotNone(m)
        self.assertEqual(len(m), 2)
        self.assertEqual(m[0], ("random", True, 1))
        self.assertEqual(m[1], ("profile", None, 2))

    def test_value_flag_inline_still_works(self):
        m = _match_flag("-pwork")
        self.assertEqual(m, [("profile", "work", 1)])

    def test_unknown_letter_rejects_whole_token(self):
        self.assertIsNone(_match_flag("-rx"))
        self.assertIsNone(_match_flag("-nrx"))

    def test_dash_alone_is_not_a_bundle(self):
        self.assertIsNone(_match_flag("-"))
        self.assertIsNone(_match_flag("--"))


class TestConsumeBundles(unittest.TestCase):
    """End-to-end extractor behavior for the original bug and its siblings."""

    def test_nr_with_late_p_picks_named_profile(self):
        values, rest = _consume_launch_flags(["-nr", "-p", "lab", "chat"])
        self.assertEqual(values["dry-run"], True)
        self.assertEqual(values["random"], True)
        self.assertEqual(values["profile"], "lab")
        self.assertEqual(rest, ["chat"])

    def test_nr_p_value_inline(self):
        values, rest = _consume_launch_flags(["-nrp", "lab", "chat"])
        self.assertEqual(values["dry-run"], True)
        self.assertEqual(values["random"], True)
        self.assertEqual(values["profile"], "lab")
        self.assertEqual(rest, ["chat"])

    def test_unknown_bundle_forwards_to_agy(self):
        values, rest = _consume_launch_flags(["-x", "foo"])
        self.assertEqual(values["profile"], None)
        self.assertEqual(rest, ["-x", "foo"])

    def test_repeat_of_bundle_flag_forwards_whole_token(self):
        values, rest = _consume_launch_flags(["-p", "lab", "-np", "x"])
        self.assertEqual(values["profile"], "lab")
        self.assertEqual(values["dry-run"], None)
        self.assertEqual(rest, ["-np", "x"])

    def test_repeat_as_second_letter_leaks_no_earlier_mutation(self):
        """``-fr`` repeats ``random`` (already consumed by a prior ``-r``) as
        its SECOND letter; the whole token must forward untouched, so
        ``force`` (its first letter) must stay unset rather than leaking a
        partial mutation applied before the repeat was detected."""
        values, rest = _consume_launch_flags(["-r", "-fr", "-n"])
        self.assertEqual(values["random"], True)
        self.assertEqual(values["force"], None)
        self.assertEqual(values["dry-run"], None)
        self.assertEqual(rest, ["-fr", "-n"])

    def test_repeat_as_first_letter_forwards_whole_token(self):
        """Symmetric case: ``-rf`` repeats ``random`` as its FIRST letter;
        ``force`` (its second letter) must also stay unset."""
        values, rest = _consume_launch_flags(["-r", "-rf", "-n"])
        self.assertEqual(values["random"], True)
        self.assertEqual(values["force"], None)
        self.assertEqual(values["dry-run"], None)
        self.assertEqual(rest, ["-rf", "-n"])

    def test_bundle_missing_value_raises(self):
        with self.assertRaises(StoreError):
            _consume_launch_flags(["-np"])

    def test_letter_repeated_inside_one_bundle_is_opaque(self):
        self.assertIsNone(_match_flag("-rnr"))
        self.assertIsNone(_match_flag("-rr"))
        values, rest = _consume_launch_flags(["-rnr", "chat"])
        self.assertEqual(values["random"], None)
        self.assertEqual(values["dry-run"], None)
        self.assertEqual(rest, ["-rnr", "chat"])


class TestLateBundleWarning(unittest.TestCase):
    """Late value flags (after a non-flag token) must be surfaced with the
    most accurate spelling — for bundles that means the bundle token plus
    the consumed value token."""

    def _warn(self, raw):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _warn_late_flags(
                {k: None for k in ("profile", "random", "dry-run", "binary")},
                raw,
            )
        return buf.getvalue()

    def test_late_attached_value_warns(self):
        out = self._warn(["chat", "-pwork"])
        self.assertIn("must come first", out)
        self.assertIn("-pwork", out)

    def test_late_bundled_value_warns_with_next_token(self):
        out = self._warn(["chat", "-rp", "work"])
        self.assertIn("must come first", out)
        self.assertIn("-rp work", out)

    def test_late_unknown_bundle_stays_silent(self):
        self.assertEqual(self._warn(["chat", "-xfoo"]), "")


class TestReservedProfileNames(BaseCase):
    """create/rename must refuse names that collide with subcommands or
    their aliases — the dispatcher always wins in launcher mode."""

    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_canonical_subcommand_name_is_refused(self):
        for name in ("status", "list", "create", "delete", "rename", "doctor"):
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as cm:
                    self.store.create(name)
                self.assertIn("reserved profile name", str(cm.exception))

    def test_alias_is_refused(self):
        for alias in ("ls", "mv", "rm", "c", "in", "imp"):
            with self.subTest(alias=alias):
                with self.assertRaises(StoreError) as cm:
                    self.store.create(alias)
                self.assertIn("reserved profile name", str(cm.exception))

    def test_rename_into_reserved_name_is_refused(self):
        self.store.create("work")
        with self.assertRaises(StoreError):
            self.store.rename("work", "status")

    def test_unrelated_names_still_pass(self):
        self.store.create("personal")
        self.store.create("work-lab_2")

    def test_create_refuses_reserved_subcommand_name(self):
        with self.assertRaises(StoreError):
            self.store.create("status")


class TestWindowsReservedDeviceNames(BaseCase):
    """A store must stay portable: Windows reserved device names (con, prn,
    aux, nul, com1-9, lpt1-9) pass NAME_RE but break mkdir/open on Windows,
    so they are refused on every platform, not just when running on
    Windows."""

    def setUp(self):
        super().setUp()
        self.store = Store()

    def test_device_names_are_refused(self):
        for name in ("con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"):
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as cm:
                    self.store.create(name)
                self.assertIn("reserved Windows device name", str(cm.exception))

    def test_similar_but_distinct_names_still_pass(self):
        self.store.create("console")
        self.store.create("com10")

    def test_rename_into_device_name_is_refused(self):
        self.store.create("work")
        with self.assertRaises(StoreError):
            self.store.rename("work", "nul")


class TestRuntimeBundleLaunch(BaseCase):
    """The original bug end-to-end: `agydra -nr -p lab chat` used to launch
    the DEFAULT profile (exit 0, `-p lab` forwarded to agy where -p means
    print). Now both flags are recognized and the -p/-r mutual-exclusion
    guard fails closed with exit 2 before anything runs."""

    def test_nr_with_late_p_fails_closed(self):
        result = self._run_cli("-nr", "-p", "lab", "chat")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("mutually exclusive", result.stderr)

    def test_plain_bundle_still_launches_named_profile(self):
        self.store = Store()
        self.store.create("lab")
        result = self._run_cli("-n", "-p", "lab", "chat")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile : lab", result.stdout)


class TestRuntimeCreateRefusesReservedName(BaseCase):
    """End-to-end through the real CLI binary in a subprocess."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "agydra", *args],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "AGYDRA_HOME": str(self.store_root)},
        )

    def test_create_status_is_actionable(self):
        result = self._run("create", "status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved profile name", result.stderr)
        self.assertFalse((self.store_root / "profiles" / "status").exists())

    def test_create_alias_is_actionable(self):
        result = self._run("create", "ls")
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved profile name", result.stderr)


if __name__ == "__main__":
    unittest.main()
