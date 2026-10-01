"""Cross-platform process identity primitives used by the lease registry.

These primitives feed the holders registry in ``locks.py``: a profile's
live-ness is derived from ``process_alive(pid)`` plus a per-process
identity token from ``process_start_token(pid)`` that survives PID reuse.
Both must be cheap (called per holder on every prune), never raise on a
missing pid, and agree across Windows, macOS and Linux.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import platforms


class TestProcessAlive(unittest.TestCase):
    def test_current_process_alive(self) -> None:
        self.assertTrue(platforms.process_alive(os.getpid()))

    def test_zero_is_dead(self) -> None:
        self.assertFalse(platforms.process_alive(0))

    def test_negative_is_dead(self) -> None:
        self.assertFalse(platforms.process_alive(-1))

    def test_nonexistent_pid_is_dead(self) -> None:
        if platforms.is_windows():
            with unittest.mock.patch.object(
                platforms, "_windows_process_handle", return_value=None
            ):
                self.assertFalse(platforms.process_alive(424242))
        else:
            def fake_kill(pid, sig):
                raise ProcessLookupError(pid)
            with unittest.mock.patch("os.kill", fake_kill):
                self.assertFalse(platforms.process_alive(424242))


class TestProcessStartToken(unittest.TestCase):
    def test_current_process_token_is_non_none(self) -> None:
        self.assertIsNotNone(platforms.process_start_token(os.getpid()))

    def test_token_is_stable_across_calls(self) -> None:
        first = platforms.process_start_token(os.getpid())
        second = platforms.process_start_token(os.getpid())
        self.assertEqual(first, second)

    def test_zero_returns_none(self) -> None:
        self.assertIsNone(platforms.process_start_token(0))

    def test_negative_returns_none(self) -> None:
        self.assertIsNone(platforms.process_start_token(-1))

    def test_token_of_alive_child_is_non_none_and_stable(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; time.sleep(0.3)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            first = platforms.process_start_token(proc.pid)
            self.assertIsNotNone(first)
            self.assertEqual(
                first, platforms.process_start_token(proc.pid)
            )
        finally:
            proc.kill()
            proc.wait()
