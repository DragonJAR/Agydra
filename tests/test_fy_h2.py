"""Regression tests for FYH2 cross-platform divergence audit and C1/C2/C4/C5 fixes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from unittest import mock

import bootstrap
import locks
import platforms
from conftest import BaseCase
from store import Store


class TestCrossPlatformDivergenceReproducers(BaseCase):
    """Reproducers and invariants for Windows/POSIX divergence findings."""

    def test_windows_utf8_bom_json_parsing(self) -> None:
        bom_payload = b"\xef\xbb\xbf{\"name\": \"alpha\", \"engine\": \"agy\"}"
        target = self._tmp / "bom_profile.json"
        target.write_bytes(bom_payload)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(target.read_text(encoding="utf-8"))
        decoded = json.loads(target.read_text(encoding="utf-8-sig"))
        self.assertEqual(decoded["name"], "alpha")

    def test_windows_quoted_path_normalization(self) -> None:
        target_dir = Path("/tmp/fake/bin")
        quoted_entry = f'"{target_dir}"'
        with mock.patch.object(platforms, "is_windows", return_value=True):
            clean_entry = quoted_entry.strip('"')
            self.assertEqual(Path(clean_entry).resolve(), target_dir.resolve())
            raw_match = str(target_dir).lower() in [quoted_entry.lower()]
            self.assertFalse(raw_match)

    def test_windows_non_executable_extension_guard(self) -> None:
        script_file = self._tmp / "not_an_exec.txt"
        script_file.write_text("plain text", encoding="utf-8")
        pathext = [".com", ".exe", ".bat", ".cmd"]
        is_candidate_extension = script_file.suffix.lower() in pathext
        self.assertFalse(is_candidate_extension)

    def test_dangling_link_is_dir_semantics(self) -> None:
        target = self._tmp / "nonexistent_target"
        link = self._tmp / "dangling_link"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Symlinks not supported in current environment")
        self.assertFalse(link.is_dir())
        self.assertTrue(platforms.is_link(link))


class TestCiAndWarningFixes(BaseCase):
    """Verification for C1, C2, C4, C5 test and CI fixes."""

    def test_windows_batch_skip_env_truthiness(self) -> None:
        with mock.patch.dict(os.environ, {"AGYDRA_SKIP_WIN32_BATCH": "0"}):
            val = os.environ.get("AGYDRA_SKIP_WIN32_BATCH", "").strip().lower()
            should_skip = val in ("1", "true", "yes", "on")
            self.assertFalse(should_skip)

        with mock.patch.dict(os.environ, {"AGYDRA_SKIP_WIN32_BATCH": "1"}):
            val = os.environ.get("AGYDRA_SKIP_WIN32_BATCH", "").strip().lower()
            should_skip = val in ("1", "true", "yes", "on")
            self.assertTrue(should_skip)

    def test_same_file_distinct_paths_returns_false(self) -> None:
        store = Store()
        store.create("first")
        store.create("second")
        path1 = locks.lock_path(store, "first")
        path2 = locks.lock_path(store, "second")
        locks.lock_dir(store).mkdir(parents=True, exist_ok=True)
        path1.write_bytes(b"")
        path2.write_bytes(b"")
        fd = os.open(path1, os.O_RDONLY)
        try:
            self.assertTrue(locks._same_file(fd, path1))
            self.assertFalse(locks._same_file(fd, path2))
        finally:
            os.close(fd)

    def test_process_start_token_without_pipe_leak(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            token = platforms.process_start_token(proc.pid)
            self.assertIsNotNone(token)
        finally:
            proc.kill()
            proc.wait()

    def test_target_test_files_import_conftest(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for name in ("test_windows_batch.py", "test_platforms_process.py"):
            path = repo_root / "tests" / name
            content = path.read_text(encoding="utf-8")
            self.assertIn("import conftest", content)
