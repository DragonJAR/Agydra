"""Shared test fixtures: fake home, fake agy binary, isolated store."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


def write_fake_agy(path: Path, data_dir_env_hint: Path) -> Path:
    """Create a fake `agy` that mimics the real one's home-derived data dir.

    Returns the path of the artifact actually written (``agy`` on POSIX,
    ``agy.cmd`` on Windows).

    The fake binary:
    - resolves its data dir from HOME (POSIX) / USERPROFILE (Windows),
    - writes a sentinel file under <home>/.gemini/fake-agy-wrote,
    - supports `--version` and `-p <text>` like the real CLI,
    - prints the profile env var so tests can assert redirection happened.
    """
    if sys.platform.startswith("win"):
        # .cmd batch file (agy on Windows is commonly agy.cmd via PATHEXT)
        script = path.with_suffix(".cmd")
        script.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"--version\" ( echo fake-agy 1.0 & exit /b 0 )\r\n"
            "echo PROFILE=%AGYDRA_PROFILE% USERPROFILE=%USERPROFILE%\r\n"
            "mkdir \"%USERPROFILE%\\.gemini\" 2>nul\r\n"
            "echo wrote>%USERPROFILE%\\.gemini\\fake-agy-wrote\r\n",
            encoding="utf-8",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_name("agy.cmd").write_bytes(script.read_bytes())
        return script

    script = path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('fake-agy 1.0')\n"
        "    raise SystemExit(0)\n"
        "if '--hold' in sys.argv:\n"
        "    # Hold the session open until the test removes the gate file:\n"
        "    # exercises the -r free-profile selection under live locks.\n"
        "    gate = os.environ.get('FAKE_AGY_GATE')\n"
        "    while gate and not os.path.exists(gate):\n"
        "        time.sleep(0.05)\n"
        "home = os.environ.get('HOME', '/')\n"
        "data = os.path.join(home, '.gemini')\n"
        "os.makedirs(data, exist_ok=True)\n"
        "with open(os.path.join(data, 'fake-agy-wrote'), 'w') as fh:\n"
        "    fh.write('wrote')\n"
        "print('PROFILE=' + os.environ.get('AGYDRA_PROFILE', '-'))\n"
        "print('HOME=' + home)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


class BaseCase(unittest.TestCase):
    """Isolated environment: fake home + agydra store + fake agy binary."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="agydra-test-"))
        self.fake_home = self._tmp / "home"
        self.store_root = self._tmp / "store"
        self.bin_dir = self._tmp / "bin"
        for d in (self.fake_home, self.store_root, self.bin_dir):
            d.mkdir(parents=True)

        self._old_env = dict(os.environ)
        os.environ["AGYDRA_HOME"] = str(self.store_root)
        os.environ["HOME"] = str(self.fake_home)
        os.environ.pop("AGYDRA_PROFILE", None)
        os.environ.pop("XDG_DATA_HOME", None)
        if sys.platform.startswith("win"):
            os.environ["USERPROFILE"] = str(self.fake_home)

        # Seed a generic ~/.gemini like a real logged-in user would have.
        (self.fake_home / ".gemini").mkdir()
        (self.fake_home / ".gemini" / "oauth_creds.json").write_text(
            '{"access_token": "generic-token"}', encoding="utf-8"
        )
        (self.fake_home / ".gemini" / "google_accounts.json").write_text(
            '{"active": "generic@example.com"}', encoding="utf-8"
        )
        # Unrelated home entry that must be mirrored into overlays.
        (self.fake_home / ".gitconfig").write_text("[user]\n", encoding="utf-8")

        self.agy_bin = write_fake_agy(self.bin_dir / "agy", self.fake_home)
        # Point the override at the artifact that actually exists on this OS
        # (agy.cmd on Windows), not the extensionless POSIX path.
        os.environ["AGYDRA_AGY_BIN"] = str(self.agy_bin)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self._tmp, ignore_errors=True)
