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

from agydra.cli import (  # noqa: E402
    _consume_launch_flags,
    _match_flag,
    _warn_late_flags,
)
from agydra.store import Store, StoreError  # noqa: E402

from conftest import BaseCase  # noqa: E402


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
        # The value flag at the tail of the bundle demands the next argv
        # token (width 2, inline None).
        self.assertEqual(m[1], ("profile", None, 2))

    def test_value_flag_inline_still_works(self):
        m = _match_flag("-pwork")
        self.assertEqual(m, [("profile", "work", 1)])

    def test_unknown_letter_rejects_whole_token(self):
        # A bundle we don't own is forwarded to agy verbatim; never partial.
        self.assertIsNone(_match_flag("-rx"))
        # Even a familiar letter followed by an unknown one rejects the lot.
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
        # First -p consumes; the repeated -p inside a bundle forwards the
        # bundle token and everything after to agy.
        values, rest = _consume_launch_flags(["-p", "lab", "-np", "x"])
        self.assertEqual(values["profile"], "lab")
        self.assertEqual(rest, ["-np", "x"])

    def test_bundle_missing_value_raises(self):
        with self.assertRaises(StoreError):
            _consume_launch_flags(["-np"])


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
        # The warning only triggers for flags the matcher knows; agy-only
        # bundles like ``-x`` are agy's business.
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
        # Regression guard: the regex check must run first; the reserved
        # check must not regress on valid, non-shadowing names.
        self.store.create("personal")
        self.store.create("work-lab_2")

    def test_legacy_profile_reachable_via_p_flag(self):
        # Pre-existing reserved names are NOT retroactively refused: seed
        # a `status` profile on disk (as an older agydra would have left)
        # and verify it stays visible, resolvable and creatable-never.
        import json

        legacy = self.store.profiles_dir / "status"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "data").mkdir(exist_ok=True)
        (legacy / "profile.json").write_text(
            json.dumps({"name": "status", "seq": 1, "created": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        # Visible in list() / names() — not silently dropped as unreadable.
        self.assertIn("status", self.store.names())
        self.assertEqual(self.store.unreadable_profiles(), [])
        # Resolvable: the -p escape hatch keeps the profile usable.
        self.assertEqual(self.store.resolve_ref("status"), "status")
        # But creating a NEW one with the same (still reserved) name fails.
        with self.assertRaises(StoreError):
            self.store.create("status")


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
        # And the profile was not created.
        self.assertFalse((self.store_root / "profiles" / "status").exists())

    def test_create_alias_is_actionable(self):
        result = self._run("create", "ls")
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved profile name", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
