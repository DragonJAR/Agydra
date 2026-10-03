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


class TestWindowsProcessHandleSignatures(unittest.TestCase):
    def test_pointer_sized_handle_flows_through_windows_process_apis(self) -> None:
        import ctypes
        import ctypes.wintypes

        if ctypes.sizeof(ctypes.c_void_p) < 8:
            self.skipTest("a HANDLE value above 32 bits requires 64-bit pointers")

        handle = 0x1_23456789
        kernel32 = unittest.mock.Mock()
        kernel32.OpenProcess = unittest.mock.Mock(return_value=handle)

        def set_exit_code(_handle, exit_code_pointer):
            exit_code = getattr(exit_code_pointer, "_obj", None)
            self.assertIs(type(exit_code), ctypes.c_uint32)
            self.assertEqual(ctypes.sizeof(exit_code), 4)
            self.assertIs(
                kernel32.GetExitCodeProcess.argtypes[1]._type_,
                ctypes.c_uint32,
            )
            exit_code.value = 259
            return 1

        def set_process_times(
            _handle,
            creation_pointer,
            exit_pointer,
            kernel_pointer,
            user_pointer,
        ):
            targets = [
                getattr(pointer, "_obj", None)
                for pointer in (
                    creation_pointer, exit_pointer, kernel_pointer, user_pointer
                )
            ]
            for target in targets:
                self.assertIs(type(target), ctypes.wintypes.FILETIME)
                self.assertEqual(
                    ctypes.sizeof(target), ctypes.sizeof(ctypes.wintypes.FILETIME)
                )
            self.assertEqual(len({id(target) for target in targets}), 4)
            creation = targets[0]
            creation.dwHighDateTime = 2
            creation.dwLowDateTime = 3
            return 1

        kernel32.GetExitCodeProcess = unittest.mock.Mock(
            side_effect=set_exit_code
        )
        kernel32.GetProcessTimes = unittest.mock.Mock(
            side_effect=set_process_times
        )
        kernel32.CloseHandle = unittest.mock.Mock(return_value=1)
        load_library = unittest.mock.Mock(return_value=kernel32)

        with unittest.mock.patch.object(
            platforms, "is_windows", return_value=True
        ), unittest.mock.patch.object(
            ctypes, "WinDLL", load_library, create=True
        ):
            self.assertTrue(platforms.process_alive(1234))
            self.assertEqual(platforms.process_start_token(1234), "8589934595")

        for call in load_library.call_args_list:
            self.assertEqual(call.args, ("kernel32",))
            self.assertEqual(call.kwargs, {"use_last_error": True})
        self.assertEqual(kernel32.OpenProcess.call_count, 2)
        self.assertEqual(kernel32.OpenProcess.call_args.args, (0x1000, False, 1234))
        self.assertEqual(kernel32.GetExitCodeProcess.call_args.args[0], handle)
        self.assertEqual(kernel32.GetProcessTimes.call_args.args[0], handle)
        self.assertEqual(
            [call.args for call in kernel32.CloseHandle.call_args_list],
            [(handle,), (handle,)],
        )

        dword_pointer = ctypes.POINTER(ctypes.c_uint32)
        filetime_pointer = ctypes.POINTER(ctypes.wintypes.FILETIME)
        self.assertEqual(
            kernel32.OpenProcess.argtypes,
            (ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32),
        )
        self.assertIs(kernel32.OpenProcess.restype, ctypes.c_void_p)
        self.assertEqual(
            kernel32.GetExitCodeProcess.argtypes,
            (ctypes.c_void_p, dword_pointer),
        )
        self.assertIs(kernel32.GetExitCodeProcess.restype, ctypes.c_int32)
        self.assertEqual(
            kernel32.GetProcessTimes.argtypes,
            (
                ctypes.c_void_p,
                filetime_pointer,
                filetime_pointer,
                filetime_pointer,
                filetime_pointer,
            ),
        )
        self.assertIs(kernel32.GetProcessTimes.restype, ctypes.c_int32)
        self.assertEqual(kernel32.CloseHandle.argtypes, (ctypes.c_void_p,))
        self.assertIs(kernel32.CloseHandle.restype, ctypes.c_int32)


class TestWindowsOpenProcessFailure(unittest.TestCase):
    ACCESS_DENIED = 5
    INVALID_PARAMETER = 87

    def _failing_open(self, error):
        import ctypes

        kernel32 = unittest.mock.Mock()
        kernel32.OpenProcess = unittest.mock.Mock(return_value=None)
        kernel32.GetExitCodeProcess = unittest.mock.Mock()
        kernel32.GetProcessTimes = unittest.mock.Mock()
        kernel32.CloseHandle = unittest.mock.Mock()
        patches = (
            unittest.mock.patch.object(platforms, "is_windows", return_value=True),
            unittest.mock.patch.object(
                ctypes, "WinDLL", unittest.mock.Mock(return_value=kernel32), create=True
            ),
            unittest.mock.patch.object(
                ctypes, "get_last_error", return_value=error, create=True
            ),
        )
        return kernel32, patches

    def _probe(self, error):
        kernel32, patches = self._failing_open(error)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return kernel32, platforms.process_alive(4321), platforms.process_start_token(4321)

    def test_access_denied_counts_as_alive_and_has_no_start_token(self):
        kernel32, alive, token = self._probe(self.ACCESS_DENIED)

        self.assertTrue(alive)
        self.assertIsNone(token)
        kernel32.GetExitCodeProcess.assert_not_called()
        kernel32.GetProcessTimes.assert_not_called()
        kernel32.CloseHandle.assert_not_called()

    def test_invalid_parameter_is_the_only_proof_of_absence(self):
        kernel32, alive, token = self._probe(self.INVALID_PARAMETER)

        self.assertFalse(alive)
        self.assertIsNone(token)
        kernel32.CloseHandle.assert_not_called()

    def test_unclassified_error_counts_conservatively_as_alive(self):
        for error in (0, 1, 31, 1450, 0xFFFFFFFF):
            with self.subTest(error=error):
                kernel32, alive, token = self._probe(error)
                self.assertTrue(alive)
                self.assertIsNone(token)
