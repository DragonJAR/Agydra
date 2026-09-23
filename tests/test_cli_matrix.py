"""Exhaustive matrix validation for all CLI parameters, flags, bundles, and subcommands."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import BaseCase
from store import Store


class TestCliParameterMatrix(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.store.create("alpha", description="first profile")
        self.store.create("beta", description="second profile")
        self.store.create("gamma", description="third profile")
        for name in ("alpha", "beta", "gamma"):
            token_dir = self.store.profile_data_dir(name) / "antigravity-cli"
            token_dir.mkdir(parents=True, exist_ok=True)
            (token_dir / "antigravity-oauth-token").write_text(
                '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
            )

    def test_launcher_profile_flag_variants(self):
        cases = [
            (["-p", "alpha", "-n"], 0, "profile : alpha"),
            (["--profile", "beta", "-n"], 0, "profile : beta"),
            (["-pgamma", "-n"], 0, "profile : gamma"),
            (["--profile=alpha", "-n"], 0, "profile : alpha"),
            (["-p", "1", "-n"], 0, "profile : alpha"),
            (["-p", "#2", "-n"], 0, "profile : beta"),
            (["-p", "nonexistent", "-n"], 1, "unknown profile"),
            (["-p", "99", "-n"], 1, "out of range"),
            (["-p", "", "-n"], 1, "unknown profile"),
            (["--profile=", "-n"], 1, "unknown profile"),
        ]
        for args, expected_rc, needle in cases:
            with self.subTest(args=args):
                res = self._run_cli(*args)
                self.assertEqual(res.returncode, expected_rc, res.stderr)
                output = res.stdout if expected_rc == 0 else res.stderr
                self.assertIn(needle, output)

    def test_launcher_random_flag_variants(self):
        for flag in ("-r", "--random"):
            with self.subTest(flag=flag):
                res = self._run_cli(flag, "-n")
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("profile :", res.stdout)
                self.assertIn("(-r)", res.stdout)

    def test_launcher_dry_run_flag_alone(self):
        for flag in ("-n", "--dry-run"):
            with self.subTest(flag=flag):
                res = self._run_cli(flag)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("profile : alpha", res.stdout)
                self.assertIn("default", res.stdout)

    def test_launcher_binary_flag_variants(self):
        cases = [
            (["-b", str(self.agy_bin), "-n"], 0, str(self.agy_bin)),
            (["--binary", str(self.agy_bin), "-n"], 0, str(self.agy_bin)),
            ([f"-b{self.agy_bin}", "-n"], 0, str(self.agy_bin)),
            ([f"--binary={self.agy_bin}", "-n"], 0, str(self.agy_bin)),
            (["-b", "/nonexistent/path/agy", "-n"], 1, "could not find"),
        ]
        for args, expected_rc, needle in cases:
            with self.subTest(args=args):
                res = self._run_cli(*args)
                self.assertEqual(res.returncode, expected_rc, res.stderr)
                output = res.stdout if expected_rc == 0 else res.stderr
                self.assertIn(needle, output)

    def test_profile_and_random_conflict_exits_2(self):
        cases = [
            ["-p", "alpha", "-r"],
            ["-r", "-p", "alpha"],
            ["--random", "--profile", "beta"],
            ["--profile=beta", "--random"],
            ["-rp", "alpha"],
            ["-nrp", "alpha"],
            ["-nr", "-p", "alpha"],
        ]
        for args in cases:
            with self.subTest(args=args):
                res = self._run_cli(*args)
                self.assertEqual(res.returncode, 2, res.stderr)
                self.assertIn("mutually exclusive", res.stderr)

    def test_short_flag_bundles_and_combinations(self):
        cases = [
            (["-nr"], 0, ["(-r)", "profile :"]),
            (["-rn"], 0, ["(-r)", "profile :"]),
            (["-np", "beta"], 0, ["profile : beta"]),
            (["-npbeta"], 0, ["profile : beta"]),
            (["-nb", str(self.agy_bin)], 0, [str(self.agy_bin)]),
            (["-n", "-b", str(self.agy_bin), "-p", "beta"], 0, ["profile : beta", str(self.agy_bin)]),
            (["-np", "beta", "-b", str(self.agy_bin)], 0, ["profile : beta", str(self.agy_bin)]),
            (["-nr", "-b", str(self.agy_bin)], 0, ["(-r)", str(self.agy_bin)]),
        ]
        for args, expected_rc, needles in cases:
            with self.subTest(args=args):
                res = self._run_cli(*args)
                self.assertEqual(res.returncode, expected_rc, res.stderr)
                for needle in needles:
                    self.assertIn(needle, res.stdout)

    def test_forwarding_and_delimiters(self):
        res = self._run_cli("-n", "-p", "alpha", "chat", "--foo", "bar")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("chat --foo bar", res.stdout)

        res_delim = self._run_cli("-n", "--", "-p", "alpha")
        self.assertEqual(res_delim.returncode, 0, res_delim.stderr)
        self.assertIn("-p alpha", res_delim.stdout)

        res_opaque = self._run_cli("-n", "-rx", "forwarded")
        self.assertEqual(res_opaque.returncode, 0, res_opaque.stderr)
        self.assertIn("-rx forwarded", res_opaque.stdout)

        res_repeat = self._run_cli("-n", "-rr", "chat")
        self.assertEqual(res_repeat.returncode, 0, res_repeat.stderr)
        self.assertIn("-rr chat", res_repeat.stdout)

        res_late = self._run_cli("chat", "-p", "alpha", "-n")
        self.assertEqual(res_late.returncode, 0, res_late.stderr)
        self.assertIn("must come first", res_late.stderr)

    def test_subcommand_aliases_and_operations(self):
        for cmd in ("list", "ls", "l"):
            with self.subTest(cmd=cmd):
                res = self._run_cli(cmd)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("alpha", res.stdout)

        res_c = self._run_cli("c", "delta", "-d", "fourth profile")
        self.assertEqual(res_c.returncode, 0, res_c.stderr)
        self.assertIn("created profile: delta", res_c.stdout)

        res_login_dry = self._run_cli("login", "delta", "-n")
        self.assertEqual(res_login_dry.returncode, 0, res_login_dry.stderr)
        self.assertIn("profile : delta", res_login_dry.stdout)

        res_in_alias = self._run_cli("in", "-n")
        self.assertEqual(res_in_alias.returncode, 0, res_in_alias.stderr)
        self.assertIn("profile : alpha", res_in_alias.stdout)

        res_default_get = self._run_cli("default")
        self.assertEqual(res_default_get.returncode, 0, res_default_get.stderr)
        self.assertIn("alpha", res_default_get.stdout)

        res_d_set = self._run_cli("d", "beta")
        self.assertEqual(res_d_set.returncode, 0, res_d_set.stderr)
        self.assertIn("default profile: beta", res_d_set.stdout)

        for cmd in ("status", "st"):
            with self.subTest(cmd=cmd):
                res = self._run_cli(cmd, "-n")
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("profile : beta", res.stdout)

        (self.store.profile_data_dir("alpha") / "settings.json").write_text("{}", encoding="utf-8")
        res_share = self._run_cli("share", "alpha", "beta")
        self.assertEqual(res_share.returncode, 0, res_share.stderr)
        self.assertIn("copied", res_share.stdout)

        res_mv = self._run_cli("mv", "delta", "epsilon")
        self.assertEqual(res_mv.returncode, 0, res_mv.stderr)
        self.assertIn("renamed 'delta' -> 'epsilon'", res_mv.stdout)

        res_rm = self._run_cli("rm", "epsilon", "-f", "--no-backup")
        self.assertEqual(res_rm.returncode, 0, res_rm.stderr)
        self.assertIn("deleted profile: epsilon", res_rm.stdout)

        for cmd in ("doctor", "doc"):
            with self.subTest(cmd=cmd):
                res = self._run_cli(cmd)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("result: healthy", res.stdout)

        for cmd in ("setup", "install"):
            with self.subTest(cmd=cmd):
                res = self._run_cli(cmd, "-n")
                self.assertEqual(res.returncode, 0, res.stderr)

        for cmd in ("help", "-h", "--help"):
            with self.subTest(cmd=cmd):
                res = self._run_cli(cmd)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("usage: agydra", res.stdout)

        for cmd in ("version", "--version"):
            with self.subTest(cmd=cmd):
                res = self._run_cli(cmd)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("agydra 1.2.0", res.stdout)


if __name__ == "__main__":
    unittest.main()
