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
    _LAUNCH_FLAGS,
    _check_no_launcher_long_flag_collision,
    _check_no_launcher_short_flag_collision,
    _consume_launch_flags,
    _is_all_launcher_letters,
    _LAUNCHER_LONG_NAMES,
    _LAUNCHER_SHORT_LETTERS,
    _match_flag,
    _resolve_subcommand,
    _warn_late_flags,
)
from store import Store, StoreError

import vocab
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

    def test_repeat_swallowing_later_flag_warns_on_stderr(self):
        """Regression: ``-p alpha -p beta -n`` — the second ``-p`` is a
        repeat of an already-consumed flag, so (by contract) everything
        from that token onward, including the later ``-n``, is dumped into
        ``rest`` untouched. Without a diagnostic this is silent and
        order-dependent: the user asked for a dry run and got a real
        launch instead. The extractor must warn on stderr that ``-n`` was
        forwarded to agy instead of being consumed."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            values, rest = _consume_launch_flags(
                ["-p", "alpha", "-p", "beta", "-n", "chat"]
            )
        self.assertEqual(values["profile"], "alpha")
        self.assertIsNone(values["dry-run"])
        self.assertEqual(rest, ["-p", "beta", "-n", "chat"])
        out = buf.getvalue()
        self.assertIn("-n", out)
        self.assertIn("must come first", out)

    def test_reverse_order_control_still_consumes_dry_run(self):
        """Control case: same flags, ``-n`` placed BEFORE the repeat. This
        already worked (order-independence was the bug) and must keep
        working, with no spurious warning since nothing is swallowed."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            values, rest = _consume_launch_flags(
                ["-n", "-p", "alpha", "-p", "beta"]
            )
        self.assertTrue(values["dry-run"])
        self.assertEqual(values["profile"], "alpha")
        self.assertEqual(rest, ["-p", "beta"])
        self.assertEqual(buf.getvalue(), "")


class TestLateBundleWarning(unittest.TestCase):
    """Late value flags (after a non-flag token) must be surfaced with the
    most accurate spelling — for bundles that means the bundle token plus
    the consumed value token."""

    def _warn(self, raw):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _warn_late_flags(
                {k: None for k in _LAUNCH_FLAGS},
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
        for name in ("status", "list", "create", "delete", "rename", "doctor", "usage"):
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as cm:
                    self.store.create(name)
                self.assertIn("reserved profile name", str(cm.exception))

    def test_alias_is_refused(self):
        for alias in ("ls", "mv", "rm", "c", "in", "imp", "us", "h"):
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


class TestLauncherShortFlagCollisionGuard(unittest.TestCase):
    """A single-dash subcommand token (``agydra -<name>``) is indistinguishable
    from a launcher-mode short-flag bundle when every character of ``<name>``
    is itself one of the launcher's own short-flag letters -- ``-nr`` must
    always mean ``-n -r``, never a hypothetical subcommand named "nr". This
    collision must never silently reappear as either vocabulary grows, so
    it is checked at import time against the real tables AND unit-tested
    here directly against crafted, hypothetical vocabularies -- not just
    today's real data."""

    def test_real_vocabulary_has_no_collision_today(self):
        _check_no_launcher_short_flag_collision(
            vocab.CANONICAL, _LAUNCHER_SHORT_LETTERS
        )

    def test_is_all_launcher_letters_true_for_full_overlap(self):
        self.assertTrue(_is_all_launcher_letters("nr", {"n", "r"}))
        self.assertTrue(_is_all_launcher_letters("prnbf", {"p", "r", "n", "b", "f"}))

    def test_is_all_launcher_letters_false_for_partial_or_no_overlap(self):
        self.assertFalse(_is_all_launcher_letters("rm", {"p", "r", "n", "b", "f"}))
        self.assertFalse(_is_all_launcher_letters("us", {"p", "r", "n", "b", "f"}))

    def test_is_all_launcher_letters_false_for_empty_token(self):
        self.assertFalse(_is_all_launcher_letters("", {"p", "r", "n", "b", "f"}))

    def test_guard_raises_for_a_hypothetical_colliding_alias(self):
        hypothetical_canonical = {"list": "list", "nr": "list"}
        with self.assertRaises(RuntimeError) as cm:
            _check_no_launcher_short_flag_collision(
                hypothetical_canonical, {"p", "r", "n", "b", "f"}
            )
        self.assertIn("nr", str(cm.exception))

    def test_guard_passes_for_a_non_colliding_hypothetical_vocabulary(self):
        hypothetical_canonical = {"list": "list", "status": "status"}
        _check_no_launcher_short_flag_collision(
            hypothetical_canonical, {"p", "r", "n", "b", "f"}
        )

    def test_guard_detects_collision_if_a_new_launcher_letter_is_added(self):
        """Simulates the launcher vocabulary growing a new short flag that
        happens to spell out an existing subcommand alias entirely."""
        hypothetical_canonical = {"doctor": "doctor", "doc": "doctor"}
        with self.assertRaises(RuntimeError):
            _check_no_launcher_short_flag_collision(
                hypothetical_canonical, {"d", "o", "c"}
            )


class TestLauncherLongFlagCollisionGuard(unittest.TestCase):
    """A double-dash subcommand token (``agydra --<name>``) is resolved by
    ``_resolve_subcommand`` BEFORE the launcher's own argparse-based flag
    parsing ever runs, so a subcommand named exactly like one of
    ``_LAUNCH_FLAGS``'s long names (e.g. a hypothetical subcommand
    ``force``) would silently shadow that launcher flag (``agydra --force``
    would dispatch to the subcommand instead of setting the force flag).
    This is the second, narrower collision class from the short-flag
    bundle one above; checked at import time against the real tables AND
    unit-tested here directly against crafted, hypothetical vocabularies."""

    def test_real_vocabulary_has_no_collision_today(self):
        _check_no_launcher_long_flag_collision(
            vocab.CANONICAL, _LAUNCHER_LONG_NAMES
        )

    def test_guard_raises_for_a_hypothetical_colliding_alias(self):
        hypothetical_canonical = {"list": "list", "force": "list"}
        with self.assertRaises(RuntimeError) as cm:
            _check_no_launcher_long_flag_collision(
                hypothetical_canonical, {"profile", "random", "dry-run", "binary", "force"}
            )
        self.assertIn("force", str(cm.exception))

    def test_guard_passes_for_a_non_colliding_hypothetical_vocabulary(self):
        hypothetical_canonical = {"list": "list", "status": "status"}
        _check_no_launcher_long_flag_collision(
            hypothetical_canonical, {"profile", "random", "dry-run", "binary", "force"}
        )

    def test_guard_detects_collision_if_a_new_launcher_long_name_is_added(self):
        """Simulates the launcher vocabulary growing a new long flag that
        happens to exactly spell out an existing subcommand alias."""
        hypothetical_canonical = {"doctor": "doctor", "doc": "doctor"}
        with self.assertRaises(RuntimeError):
            _check_no_launcher_long_flag_collision(
                hypothetical_canonical, {"doc"}
            )

    def test_guard_covers_the_random_long_alias(self):
        self.assertEqual(_match_flag("--rotate"), [("random", True, 1)])
        with self.assertRaises(RuntimeError):
            _check_no_launcher_long_flag_collision(
                {"rotate": "random"}, _LAUNCHER_LONG_NAMES
            )


class TestResolveSubcommandSingleDash(unittest.TestCase):
    """Direct unit coverage of the single-dash resolution rule, independent
    of subprocess end-to-end tests."""

    def test_single_dash_resolves_every_canonical_name_and_alias(self):
        for canonical, aliases in vocab.SUBCOMMAND_ALIASES.items():
            for name in (canonical, *aliases):
                with self.subTest(name=name):
                    self.assertEqual(_resolve_subcommand("-" + name), canonical)

    def test_double_dash_still_resolves_every_canonical_name_and_alias(self):
        for canonical, aliases in vocab.SUBCOMMAND_ALIASES.items():
            for name in (canonical, *aliases):
                with self.subTest(name=name):
                    self.assertEqual(_resolve_subcommand("--" + name), canonical)

    def test_bare_still_resolves_every_canonical_name_and_alias(self):
        for canonical, aliases in vocab.SUBCOMMAND_ALIASES.items():
            for name in (canonical, *aliases):
                with self.subTest(name=name):
                    self.assertEqual(_resolve_subcommand(name), canonical)

    def test_launcher_bundles_never_resolve_to_a_subcommand(self):
        for token in ("-n", "-r", "-nr", "-rp", "-rn", "-b/opt/agy", "-nrf", "-", "--"):
            with self.subTest(token=token):
                self.assertIsNone(_resolve_subcommand(token))

    def test_unrelated_dashed_token_resolves_to_none(self):
        self.assertIsNone(_resolve_subcommand("-xyz"))
        self.assertIsNone(_resolve_subcommand("--xyz"))

    def test_partial_letter_overlap_alias_dispatches_as_subcommand(self):
        """``-rm``/``--rm`` are INTENTIONALLY subcommand dispatch (``delete``
        via its ``rm`` alias), not a launcher-mode short-flag bundle.

        Only tokens FULLY composed of the launcher's own short-flag letters
        (``p``/``r``/``n``/``b``/``f``) are protected from single-dash
        dispatch by ``_check_no_launcher_short_flag_collision`` and stay
        bundles (see ``TestLauncherShortFlagCollisionGuard`` and
        ``test_launcher_bundles_never_resolve_to_a_subcommand`` above).
        ``rm`` only PARTIALLY overlaps those letters (``r`` is one, ``m`` is
        not), so it is not fully-launcher-letters and dispatches as a
        subcommand instead -- a consistent extension of the pre-existing
        ``--rm`` -> delete precedent (which predates single-dash support),
        not a new special case. A direct, real-world consequence: a token
        like ``agydra -rm alpha`` deletes profile ``alpha`` instead of being
        forwarded to ``agy`` as opaque args.
        """
        self.assertEqual(_resolve_subcommand("-rm"), "delete")
        self.assertEqual(_resolve_subcommand("--rm"), "delete")


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
