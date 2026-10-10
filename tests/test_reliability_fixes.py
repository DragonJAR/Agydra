"""Regression tests for the multi-platform reliability fixes.

Each test pins one defect found during the cross-platform audit:
- keychain.delete_slot raised TypeError ((0,) | set) on every call
- resolver markers written by PowerShell 5.1 (`>` = UTF-16LE) or Notepad
  (UTF-8 BOM) crashed the launch with a raw traceback or a bogus ref
- Store.create used check-then-act: two concurrent creates of the same
  name both succeeded, the last write silently winning
- Profile.from_dict passed last_used/email through unvalidated, letting
  hand-edited metadata explode later inside resolver's min() key
"""
import os
import sys
import unittest
from pathlib import Path

from conftest import isolated_store_env

import keychain
import models
import resolver
import runner
from store import Store, StoreError


class _FakeCompleted:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestDeleteSlotCodes(unittest.TestCase):
    def test_not_found_code_is_accepted_not_crashing(self):
        """rc=44 (item not found) must return silently, not raise TypeError."""
        real_run = keychain._run

        def fake_run(args, input_bytes=None):
            return _FakeCompleted(44)

        keychain._run = fake_run
        try:
            keychain.delete_slot("gemini")
        finally:
            keychain._run = real_run

    def test_success_code_is_accepted(self):
        real_run = keychain._run
        keychain._run = lambda args, input_bytes=None: _FakeCompleted(0)
        try:
            keychain.delete_slot("gemini")
        finally:
            keychain._run = real_run

    def test_other_codes_raise_keychainerror(self):
        real_run = keychain._run
        keychain._run = lambda args, input_bytes=None: _FakeCompleted(1, stderr=b"boom")
        try:
            with self.assertRaises(keychain.KeychainError):
                keychain.delete_slot("gemini")
        finally:
            keychain._run = real_run

    def test_not_found_codes_are_a_plain_set(self):
        self.assertIsInstance(keychain.NOT_FOUND_CODES, set)


class TestMarkerEncoding(unittest.TestCase):
    def _store_with_profile(self, root: str) -> tuple:
        store = Store()
        store.create("work")
        return store, Path(root)

    def test_bom_marker_resolves_cleanly(self):
        with isolated_store_env() as root:
            store, tmp = self._store_with_profile(root)
            (tmp / ".agydra").write_bytes(b"\xef\xbb\xbf" + "work\n".encode("utf-8"))
            res = resolver.resolve(store, cwd=tmp, env={})
            self.assertEqual(res.name, "work")

    def test_utf16_marker_raises_actionable_storeerror(self):
        with isolated_store_env() as root:
            store, tmp = self._store_with_profile(root)
            (tmp / ".agydra").write_bytes("work\n".encode("utf-16"))
            with self.assertRaises(StoreError) as ctx:
                resolver.resolve(store, cwd=tmp, env={})
            self.assertIn("not valid UTF-8", str(ctx.exception))

    def test_oserror_marker_raises_cannot_read_not_utf8(self):
        from unittest import mock

        with isolated_store_env() as root:
            store, tmp = self._store_with_profile(root)
            marker = tmp / ".agydra"
            marker.write_text("work\n", encoding="utf-8")
            with mock.patch.object(
                Path, "read_text", side_effect=PermissionError("denied")
            ):
                with self.assertRaises(StoreError) as ctx:
                    resolver.resolve(store, cwd=tmp, env={})
            msg = str(ctx.exception)
            self.assertIn("cannot read project marker", msg)
            self.assertNotIn("not valid UTF-8", msg)


class TestCreateAtomicReserve(unittest.TestCase):
    def test_second_create_of_same_name_fails(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            meta = store.profile_meta_path("alpha")
            meta.unlink()
            with self.assertRaises(StoreError) as ctx:
                store.create("alpha")
            self.assertIn("already exists", str(ctx.exception))


class TestIsolatedStoreEnvironment(unittest.TestCase):
    def test_context_sandboxes_and_restores_host_paths(self):
        import os
        import sys

        keys = (
            "AGYDRA_HOME",
            "AGYDRA_REAL_HOME",
            "HOME",
            "LOCALAPPDATA",
            "XDG_DATA_HOME",
        )
        if sys.platform.startswith("win"):
            keys += ("USERPROFILE",)
        original = {key: os.environ.get(key) for key in keys}
        with isolated_store_env() as root:
            root_path = Path(root).resolve()
            for key in keys:
                with self.subTest(key=key):
                    self.assertTrue(
                        Path(os.environ[key]).resolve().is_relative_to(root_path)
                    )
        self.assertEqual({key: os.environ.get(key) for key in keys}, original)


class TestProfileFromDictCoercion(unittest.TestCase):
    def test_non_string_last_used_is_coerced(self):
        raw = {"name": "alpha", "seq": 1, "last_used": 20260101}
        profile = models.Profile.from_dict(raw)
        self.assertEqual(profile.last_used, "20260101")

    def test_non_string_email_is_coerced(self):
        raw = {"name": "alpha", "seq": 1, "email": 12345}
        profile = models.Profile.from_dict(raw)
        self.assertEqual(profile.email, "12345")

    def test_null_fields_stay_none(self):
        raw = {"name": "alpha", "seq": 1}
        profile = models.Profile.from_dict(raw)
        self.assertIsNone(profile.last_used)
        self.assertIsNone(profile.email)


class TestScanSinglePass(unittest.TestCase):
    def test_scan_returns_both_views(self):
        with isolated_store_env():
            store = Store()
            store.create("alpha")
            meta = store.profile_meta_path("alpha")
            meta.write_text("{ not json", encoding="utf-8")
            profiles, unreadable = store.scan()
            self.assertEqual(profiles, [])
            self.assertEqual(unreadable, ["alpha"])


class TestDeleteVerifiesRemoval(unittest.TestCase):
    """F2: rmtree must never silently swallow a failed removal — a dir that
    survives `delete()` must fail loud, not fake success and brick `create`."""

    def test_delete_raises_when_dir_survives(self):
        from unittest import mock

        import store as store_mod

        with isolated_store_env():
            store = Store()
            store.create("alpha")
            with mock.patch.object(store_mod, "rmtree"):
                with self.assertRaises(StoreError):
                    store.delete("alpha")
            self.assertTrue(store.exists("alpha"))


class TestKeychainLoginCapture(unittest.TestCase):
    """Bug guard: the login flow's keychain capture used to be dead — the
    guard's __exit__ restored the pre-launch shared slot, deleting (or
    overwriting with a stale snapshot) the fresh token agy just wrote.
    Fix: launch_guard(capture=True) persists the post-agy token as the
    profile's private slot and leaves the shared slot pointing at it."""

    def test_capture_keeps_fresh_token(self):
        from unittest import mock

        with isolated_store_env():
            store = Store()
            store.create("work")
            fresh = b"fresh-token"
            with mock.patch.object(
                keychain, "supported", return_value=True
            ), mock.patch.object(
                keychain, "_serialize_lock", return_value=mock.MagicMock()
            ), mock.patch.object(
                keychain, "_ensure_target_keychain",
                return_value=Path("/fake/login.keychain-db"),
            ), mock.patch.object(
                keychain, "read_slot", side_effect=[None, fresh, fresh]
            ), mock.patch.object(
                keychain, "write_slot"
            ), mock.patch.object(
                keychain, "save_profile_slot"
            ) as save, mock.patch.object(
                keychain, "fcntl"
            ):
                with keychain.launch_guard(store, "work", capture=True):
                    pass
            save.assert_called_once_with(
                store, "work", keychain.envelope_token_bytes(fresh)
            )


@unittest.skipIf(
    sys.platform == "win32",
    "Windows-keychain semantics differ (no OpenProcess-style launch_guard, "
    "different signal-handling on the subprocess.Popen side). The test's "
    "POSIX assumptions about SIGINT and the kernel-held lock file do not "
    "translate to the Windows runner's cmd.exe shell; a Windows counterpart "
    "would need a different runner design.",
)
class TestKeychainRunHardening(unittest.TestCase):
    """_run must never hang: stdout/stderr go to temp files (the Security
    Agent's grandchild inherits pipe write-ends and blocks communicate()
    forever), the whole process group is killed on timeout, and any failure
    degrades to a CompletedProcess(rc=-1) the caller can fail-open on."""

    def test_timeout_kills_group_and_returns_negative_rc(self):
        import subprocess
        from unittest import mock

        class FakeProc:
            pid = 4242

            def __init__(self):
                self._calls = 0

            def communicate(self, *args, **kwargs):
                self._calls += 1
                if self._calls == 1:
                    raise subprocess.TimeoutExpired(cmd="security", timeout=0.01)
                return None, None

            def kill(self):
                pass

        proc = FakeProc()
        killed = []
        with mock.patch.object(
            keychain.subprocess, "Popen", return_value=proc
        ), mock.patch.object(
            keychain.os, "killpg", side_effect=lambda pid, sig: killed.append(pid)
        ):
            result = keychain._run(["find-generic-password", "-s", "x"])
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, -1)
        self.assertEqual(killed, [4242])

    def test_popen_failure_degrades_to_negative_rc(self):
        from unittest import mock

        with mock.patch.object(
            keychain.subprocess, "Popen", side_effect=OSError("boom")
        ):
            result = keychain._run(["list-keychains"])
        self.assertEqual(result.returncode, -1)

    def test_read_slot_after_timeout_raises_keychainerror_not_attributeerror(self):
        """A `security` call that hangs used to return CompletedProcess with
        stdout/stderr=None; read_slot's `result.stderr.decode(...)` then blew
        up with AttributeError instead of the intended KeychainError."""
        import subprocess
        from unittest import mock

        class FakeProc:
            pid = 4343

            def __init__(self):
                self._calls = 0

            def communicate(self, *args, **kwargs):
                self._calls += 1
                if self._calls == 1:
                    raise subprocess.TimeoutExpired(cmd="security", timeout=0.01)
                return None, None

            def kill(self):
                pass

        with mock.patch.object(
            keychain.subprocess, "Popen", return_value=FakeProc()
        ), mock.patch.object(keychain.os, "killpg", side_effect=OSError):
            with self.assertRaises(keychain.KeychainError):
                keychain.read_slot("gemini")

    def test_supported_gates_on_kill_switch_env(self):
        import os
        from unittest import mock

        with mock.patch.object(keychain.platforms, "is_macos", return_value=True), \
                mock.patch.object(keychain.shutil, "which", return_value="/usr/bin/security"), \
                mock.patch.dict(os.environ):
            os.environ["AGYDRA_NO_KEYCHAIN"] = "1"
            self.assertFalse(keychain.supported())
            os.environ.pop("AGYDRA_NO_KEYCHAIN")
            self.assertTrue(keychain.supported())


class TestProfileSlotLifecycle(unittest.TestCase):
    """rename/delete must move/remove the profile's keychain slot backup,
    or a renamed profile loses its token and a deleted one leaks it."""

    def test_rename_moves_slot_file(self):
        with isolated_store_env():
            store = Store()
            keychain.save_profile_slot(store, "old", b"tok")
            keychain.rename_profile_slot(store, "old", "new")
            self.assertIsNone(keychain.load_profile_slot(store, "old"))
            self.assertEqual(keychain.load_profile_slot(store, "new"), b"tok")

    def test_rename_without_slot_is_noop(self):
        with isolated_store_env():
            store = Store()
            keychain.rename_profile_slot(store, "ghost", "new")

    def test_rename_without_source_slot_purges_stale_target_slot(self):
        """A source with no `.secret` must not leave the TARGET's existing
        secret untouched -- the rename target might be reusing a name from
        an earlier deleted profile, and inheriting its stale credential
        would be as wrong as a fresh `create` inheriting one (see
        `Store.create`'s own purge)."""
        with isolated_store_env():
            store = Store()
            keychain.save_profile_slot(store, "new", b"stale-from-a-past-profile")
            keychain.rename_profile_slot(store, "old", "new")
            self.assertIsNone(keychain.load_profile_slot(store, "new"))

    def test_purge_removes_and_tolerates_missing(self):
        with isolated_store_env():
            store = Store()
            keychain.save_profile_slot(store, "gone", b"tok")
            keychain.purge_profile_slot(store, "gone")
            self.assertIsNone(keychain.load_profile_slot(store, "gone"))
            keychain.purge_profile_slot(store, "gone")


class TestRunnerReleasesWaitedChildLock(unittest.TestCase):
    """The waited-child path (login/sandbox) must release the session lock
    in the parent: the child's inherited fd dies with it, but a parent that
    keeps running would leave the profile looking busy."""

    def test_lock_released_after_waited_child(self):
        import os
        import sys
        from unittest import mock

        import locks
        import platforms

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("work")
            plan = runner.build_plan(
                store, [], flag_ref="work", launch_as_child=True
            )
            with mock.patch.object(
                platforms, "run_wait", return_value=0
            ) as run_wait:
                rc = runner.run(plan, store=store)
            self.assertEqual(rc, 0)
            run_wait.assert_called_once()
            self.assertFalse(locks.is_locked(store, "work"))

    def test_drain_tty_after_waited_child_exits(self):
        """The waited-child path (login/sandbox/Windows) must drain the TTY
        input queue after the child exits: a Bubble Tea TUI that exits before
        consuming its Device-Attributes reply leaves ``ESC[?1;2c``-style
        garbage in the input queue, which the next shell prompt echoes."""
        import os
        import sys
        from unittest import mock

        import platforms

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("work")
            plan = runner.build_plan(
                store, [], flag_ref="work", launch_as_child=True
            )
            with mock.patch.object(
                platforms, "run_wait", return_value=0
            ), mock.patch.object(platforms, "drain_tty_input") as drain:
                rc = runner.run(plan, store=store)
            self.assertEqual(rc, 0)
            drain.assert_called_once()

    def test_drain_not_run_when_child_not_waited(self):
        """The exec path (POSIX interactive) cannot drain: ``launch_argv``
        replaces the process. Asserting the non-call keeps the documented
        limitation honest instead of a silent no-op that nobody maintains."""
        import os
        import sys
        from unittest import mock

        import platforms

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            if platforms.is_windows():
                self.skipTest("Windows never uses the exec path")
            store = Store()
            store.create("work")
            plan = runner.build_plan(store, [], flag_ref="work")
            with mock.patch.object(
                platforms, "launch_argv", return_value=0
            ), mock.patch.object(platforms, "drain_tty_input") as drain:
                runner.run(plan, store=store)
            drain.assert_not_called()

    def test_lock_released_after_keychain_guard_exits(self):
        """The profile lease release must happen only AFTER guard.__exit__
        finishes. Earlier than that and a joiner launching the same profile
        would see an empty registry mid-flight, take ownership of the
        shared slot, and clobber a still-running session."""
        import os
        import sys
        from unittest import mock

        import locks
        import platforms

        events: list = []

        class _RecordingGuard:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                events.append("guard-exit")
                return False

        original_release_lease = locks.release_lease

        def recording_release_lease(store, name, **kwargs):
            events.append(f"release_lease:{name}")
            original_release_lease(store, name)

        with isolated_store_env():
            os.environ["AGYDRA_AGY_BIN"] = sys.executable
            try:
                store = Store()
                store.create("work")
                plan = runner.build_plan(
                    store, [], flag_ref="work", launch_as_child=True
                )
                with mock.patch.object(
                    platforms, "run_wait", return_value=0
                ), mock.patch.object(
                    keychain, "launch_guard", return_value=_RecordingGuard()
                ), mock.patch.object(
                    locks, "release_lease", recording_release_lease
                ):
                    rc = runner.run(plan, store=store)
                self.assertEqual(rc, 0)
                self.assertEqual(events, ["guard-exit", "release_lease:work"])
            finally:
                os.environ.pop("AGYDRA_AGY_BIN", None)


@unittest.skipIf(
    sys.platform == "win32",
    "The test mocks platforms.is_windows to False but the runner.run "
    "internal path also queries subprocess.Popen with the runner's "
    "real shell (PowerShell on Windows-2022) which produces different "
    "behaviour than POSIX when launching the shebang script. The test "
    "is a Linux contract; the Windows runner would need an alternative "
    "execution path that doesn't reach subprocess.Popen at all.",
)
class TestLockHolderPidExecPathOnly(unittest.TestCase):
    """The lease registry names a process that is REALLY running for the
    session. On the plain ``execvpe`` path the pid survives the exec, so
    the entry names the engine itself; on ``launch_as_child``/sandboxed
    launches the recorded pid is the live agydra parent that waits on the
    child — accurate as a session marker and released by the runner's
    ``finally`` once the child exits."""

    def test_launch_as_child_path_records_the_live_parent_pid(self):
        import os
        import sys
        from unittest import mock

        import locks
        import platforms

        captured = {}

        def fake_run_wait(argv, env):
            captured["pid_while_held"] = locks.lock_holder_pid(store, "work")
            return 0

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("work")
            plan = runner.build_plan(
                store, [], flag_ref="work", launch_as_child=True
            )
            with mock.patch.object(
                platforms, "run_wait", side_effect=fake_run_wait
            ):
                rc = runner.run(plan, store=store)
            self.assertEqual(rc, 0)
        self.assertIn("pid_while_held", captured)
        self.assertEqual(captured["pid_while_held"], os.getpid())
        self.assertIsNone(locks.lock_holder_pid(store, "work"))

    def test_plain_exec_path_still_records_a_real_pid(self):
        import os
        from unittest import mock

        import locks
        import platforms

        captured = {}

        def fake_launch_argv(*args, **kwargs):
            captured["pid_while_held"] = locks.lock_holder_pid(store, "work")
            return 0

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("work")
            plan = runner.build_plan(store, [], flag_ref="work")
            with mock.patch.object(platforms, "is_windows", return_value=False), \
                    mock.patch.object(
                        platforms, "launch_argv", side_effect=fake_launch_argv
                    ) as launch_argv:
                rc = runner.run(plan, store=store)
            self.assertEqual(rc, 0)
            launch_argv.assert_called_once()
            self.assertEqual(captured.get("pid_while_held"), os.getpid())
            self.assertIsNone(locks.lock_holder_pid(store, "work"))


class TestRunnerReleasesLockOnSetupException(unittest.TestCase):
    """The lock is acquired before the overlay/env setup runs; a failure in
    that setup (e.g. ``isolation.build_overlay`` raising ``IsolationError``)
    must still release the handle in-process instead of leaking it because
    the failure happened outside the exec/keychain try/finally."""

    def test_build_overlay_error_leaves_profile_unlocked(self):
        import os
        import sys
        from unittest import mock

        import isolation
        import locks

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("work")
            plan = runner.build_plan(store, [], flag_ref="work")
            with mock.patch.object(
                isolation, "build_overlay",
                side_effect=isolation.IsolationError("boom"),
            ):
                with self.assertRaises(isolation.IsolationError):
                    runner.run(plan, store=store)
            self.assertFalse(locks.is_locked(store, "work"))

    def test_store_get_error_leaves_profile_unlocked(self):
        """If store.get raises (e.g. corrupt profile.json) right after
        try_lock succeeds, runner.run must release the lock in-process
        rather than leaking it."""
        import os
        import sys
        from unittest import mock

        import locks
        from store import StoreError

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("work")
            plan = runner.build_plan(store, [], flag_ref="work")
            with mock.patch.object(
                store, "get",
                side_effect=StoreError("corrupted metadata"),
            ):
                with self.assertRaises(StoreError):
                    runner.run(plan, store=store)
            self.assertFalse(locks.is_locked(store, "work"))


class TestRunnerReleasesLockOnExecFailure(unittest.TestCase):
    """``platforms.launch_argv`` deliberately catches ``execvpe`` failures
    (missing/non-executable binary) and returns 126/127 instead of raising,
    for a clean CLI error. On the plain-exec path (no sandbox, not a waited
    child, not Windows) that return must still release the just-acquired
    lock: a genuinely successful ``execvpe`` never returns control to this
    function at all, so releasing unconditionally after ``launch_argv``
    returns is always safe."""

    def test_launch_argv_failure_releases_the_lock(self):
        from unittest import mock

        import locks
        import platforms

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("work")
            plan = runner.build_plan(store, [], flag_ref="work")
            with mock.patch.object(platforms, "is_windows", return_value=False), \
                    mock.patch.object(
                        platforms, "process_alive",
                        side_effect=lambda pid: pid == os.getpid(),
                    ), \
                    mock.patch.object(
                        platforms, "launch_argv", return_value=126
                    ) as launch_argv:
                rc = runner.run(plan, store=store)
            self.assertEqual(rc, 126)
            launch_argv.assert_called_once()
            self.assertFalse(locks.is_locked(store, "work"))


class TestRunnerJoinSemantics(unittest.TestCase):
    """The runner JOINS a profile with live holders instead of refusing it:
    concurrent sessions of the same profile are the supported mode (the
    keychain slot lease makes same-profile agy sessions safe; codex/grok
    joins carry a refresh-rotation warning). The old busy-refusal message
    and its -f/--force escape hatch are gone — cross-profile keychain
    contention remains the one hard error, covered by the macOS slot-lease
    tests."""

    def _session_plan(self, launch_as_child: bool = False):
        store = Store()
        store.create("work")
        plan = runner.build_plan(
            store, [], flag_ref="work", launch_as_child=launch_as_child
        )
        return store, plan

    def test_second_session_joins_and_both_release(self):
        import locks
        import platforms
        from unittest import mock

        with isolated_store_env(), mock.patch.dict(
            self if False else {}, {}
        ), mock.patch.object(platforms, "run_wait", return_value=0) as waits:
            import os
            import sys

            os.environ["AGYDRA_AGY_BIN"] = sys.executable
            try:
                store, plan = self._session_plan(launch_as_child=True)
                first = runner.run(plan, store=store)
                second = runner.run(plan, store=store)
                self.assertEqual((first, second), (0, 0))
                self.assertEqual(waits.call_count, 2)
                self.assertFalse(locks.is_locked(store, "work"))
            finally:
                import os

                os.environ.pop("AGYDRA_AGY_BIN", None)

    def test_codex_join_warns_about_refresh_rotation(self):
        import os
        import sys
        from unittest import mock

        import platforms

        warnings: list = []

        with isolated_store_env():
            os.environ["AGYDRA_AGY_BIN"] = sys.executable
            os.environ["AGYDRA_CODEX_BIN"] = sys.executable
            try:
                store = Store()
                store.create("cx", engine="codex")
                plan = runner.build_plan(
                    store, ["--no-daemon"], flag_ref="cx", launch_as_child=True
                )
                import locks

                locks.acquire_lease(store, "cx")
                try:
                    with mock.patch.object(
                        platforms, "run_wait", return_value=0
                    ), mock.patch.object(
                        runner, "warn", side_effect=warnings.append
                    ):
                        rc = runner.run(plan, store=store)
                    self.assertEqual(rc, 0)
                finally:
                    locks.release_lease(store, "cx")
                self.assertTrue(
                    any("refresh-token rotation" in str(w) for w in warnings)
                )
            finally:
                os.environ.pop("AGYDRA_AGY_BIN", None)
                os.environ.pop("AGYDRA_CODEX_BIN", None)


class TestRunnerPickRetryPreservesCwd(unittest.TestCase):
    """When a random-pick collision triggers a retry in runner.run, the
    original plan.cwd must be passed into build_plan so marker/directory
    context is not lost."""

    def test_retry_forwards_plan_cwd(self):
        import os
        import sys
        from unittest import mock

        import keychain
        import locks

        with isolated_store_env(), mock.patch.dict(
            os.environ, {"AGYDRA_AGY_BIN": sys.executable}
        ):
            store = Store()
            store.create("alpha")
            store.create("beta")
            store.create("gamma")
            for name in ("alpha", "beta", "gamma"):
                token_dir = store.profile_data_dir(name) / "antigravity-cli"
                token_dir.mkdir(parents=True, exist_ok=True)
                (token_dir / "antigravity-oauth-token").write_text(
                    '{"token": {"access_token": "mock-token"}}', encoding="utf-8"
                )

            custom_cwd = Path("/custom/project/dir")
            plan = runner.build_plan(store, [], random_pick=True, cwd=custom_cwd)
            self.assertEqual(plan.cwd, custom_cwd)

            orig_build_plan = runner.build_plan
            build_plan_cwds = []

            def tracked_build_plan(*args, **kwargs):
                build_plan_cwds.append(kwargs.get("cwd"))
                return orig_build_plan(*args, **kwargs)

            busy_guard_calls = {"n": 0}

            class _BusyThenOkGuard:
                def __init__(self) -> None:
                    busy_guard_calls["n"] += 1
                    if busy_guard_calls["n"] == 1:
                        self._busy = keychain.KeychainBusyError(
                            "another agydra session is using the shared "
                            "Antigravity keychain slot"
                        )
                    else:
                        self._busy = None

                def __enter__(self):
                    if self._busy is not None:
                        raise self._busy
                    return self

                def __exit__(self, *exc_info):
                    return False

            try:
                with mock.patch.object(runner, "build_plan", side_effect=tracked_build_plan), \
                        mock.patch.object(keychain, "launch_guard", side_effect=lambda *a, **k: _BusyThenOkGuard()), \
                        mock.patch.object(runner.platforms, "launch_argv", return_value=0):
                    rc = runner.run(plan, store=store)
                self.assertEqual(rc, 0)
                self.assertIn(custom_cwd, build_plan_cwds)
            finally:
                for name in ("alpha", "beta", "gamma"):
                    locks.release_lease(store, name)


if __name__ == "__main__":
    unittest.main()
