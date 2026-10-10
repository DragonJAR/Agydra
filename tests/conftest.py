"""Shared test fixtures: fake home, fake agy binary, isolated store."""
from __future__ import annotations

import base64
import contextlib
import faulthandler
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["AGYDRA_NO_KEYCHAIN"] = "1"

_HANG_DUMP_SECONDS = os.environ.get("AGYDRA_TEST_HANG_DUMP_SECONDS", "")
if _HANG_DUMP_SECONDS.isdigit() and int(_HANG_DUMP_SECONDS) > 0:
    faulthandler.dump_traceback_later(int(_HANG_DUMP_SECONDS), exit=True)

os.environ["LC_ALL"] = "C.UTF-8"
os.environ.pop("LC_MESSAGES", None)


def _refuse_console_control_kill(real_kill):
    """Fail loudly instead of letting os.kill broadcast a Windows console event.

    On Windows ``signal.CTRL_C_EVENT == 0``, so a POSIX liveness probe such as
    ``os.kill(pid, 0)`` reached by a test that patches ``is_windows`` to False
    sends a real Ctrl+C to every process on the console and interrupts an
    unrelated later test.
    """
    import signal

    blocked = (signal.CTRL_C_EVENT, signal.CTRL_BREAK_EVENT)

    def guarded_kill(pid, sig):
        if sig in blocked:
            raise AssertionError(
                f"os.kill({pid}, {sig}) would broadcast a console control event on Windows"
            )
        return real_kill(pid, sig)

    return guarded_kill


if sys.platform == "win32":
    os.kill = _refuse_console_control_kill(os.kill)

REPO_ROOT = Path(__file__).resolve().parents[1]

HOST_ENGINE_VARIABLES = (
    "AGYDRA_LANG",
    "AGYDRA_CODEX_BIN",
    "AGYDRA_GROK_BIN",
    "AGYDRA_CLAUDE_BIN",
    "AGYDRA_CLAUDE_USAGE_SEQ",
    "AGYDRA_CLAUDE_USAGE_GENERATION",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "GROK_HOME",
    "GROK_LEADER_SOCKET",
)


def cli_environment(extra: dict = None) -> dict:
    """Environment for ``python -m agydra`` children, independent of the cwd.

    The repository root is prepended to ``PYTHONPATH`` (an existing value is
    kept after it), so one helper serves pytest and unittest started from any
    directory.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        os.pathsep.join([str(REPO_ROOT), existing]) if existing else str(REPO_ROOT)
    )
    env.update(extra or {})
    return env


CLI_ENTRY = "import sys; from cli import main; sys.exit(main())"


def cli_command(*args) -> list:
    """Argv running the CLI exactly like the installed console script.

    ``python -m agydra`` goes through ``agydra.py``, which needs the repo's
    ``.venv`` and runs the real bootstrap (venv, pip, PATH shim) when it is
    missing or built for another OS. Importing ``cli.main`` from the
    ``PYTHONPATH`` set by ``cli_environment`` has neither dependency.
    """
    return [sys.executable, "-c", CLI_ENTRY, *args]


def run_cli(*args, cwd, extra_env: dict = None, timeout: float = 60):
    import subprocess

    return subprocess.run(
        cli_command(*args),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=cli_environment(extra_env),
    )


def run_python_child(source: str, *args, timeout: float = 10) -> subprocess.CompletedProcess:
    """Run an inline Python snippet in a child process using temporary files for output to avoid pipe-reader threads on Windows."""
    cmd = [sys.executable, "-c", source, *(str(arg) for arg in args)]
    with tempfile.TemporaryFile() as out_fh, tempfile.TemporaryFile() as err_fh:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=out_fh,
            stderr=err_fh,
            timeout=timeout,
        )
        out_fh.seek(0)
        err_fh.seek(0)
        out = out_fh.read().decode("utf-8", "replace")
        err = err_fh.read().decode("utf-8", "replace")
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        out,
        err,
    )


@contextlib.contextmanager
def held_cli_session(*args, cwd, extra_env: dict = None, ready_timeout: float = 120):
    """Run ``agydra <args> --hold`` and yield once the fake agy is running.

    The fake agy prints ``HOLDING`` only after agydra took the session lock
    and exec-ed it, so a blocking pipe read is the readiness signal (no
    polling of lock files). Closing the session's stdin releases the fake;
    a watchdog kills a child that never becomes ready.
    """
    import subprocess
    import tempfile
    import threading

    with tempfile.TemporaryFile() as diagnostics:
        process = spawn_cli(
            *args, "--hold",
            cwd=cwd, extra_env=extra_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics, text=True,
        )
        watchdog = threading.Timer(ready_timeout, process.kill)
        watchdog.start()
        try:
            first_line = process.stdout.readline()
            watchdog.cancel()
            if first_line.strip() != "HOLDING":
                process.kill()
                process.wait()
                diagnostics.seek(0)
                raise AssertionError(
                    f"session never became ready: {first_line!r} {diagnostics.read()!r}"
                )
            yield process
        finally:
            watchdog.cancel()
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()


def spawn_cli(*args, cwd, extra_env: dict = None, **popen_kwargs):
    import subprocess

    return subprocess.Popen(
        cli_command(*args),
        cwd=str(cwd),
        env=cli_environment(extra_env),
        **popen_kwargs,
    )


class LiveHolders:
    """Registers N real, live foreign processes as lease holders."""

    def __init__(self, store, name, count):
        import subprocess

        self.store = store
        self.name = name
        self.processes = [
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            for _ in range(count)
        ]

    def __enter__(self):
        import locks
        import platforms

        holders = [
            {"pid": process.pid, "start": platforms.process_start_token(process.pid)}
            for process in self.processes
        ]
        path = locks.lock_path(self.store, self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"holders": holders}), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        for process in self.processes:
            process.kill()
            process.wait()


def _make_jwt(claims: dict) -> str:
    """Minimal unsigned JWT: only the payload segment is ever decoded."""
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg(claims)}.sig"


def authenticate_agy_profile(store, name: str) -> Path:
    """Publish a private, modern synthetic token for launch tests."""
    import account

    path = store.profile_data_dir(name, engine="agy") / account.AGY_CLI_DIR / account.TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"token":{"access_token":"mock-token"}}', encoding="utf-8")
    path.chmod(0o600)
    return path


_FAKE_AGY_HEARTBEAT_CHILD = (
    "import sys, time\n"
    "path = sys.argv[1]\n"
    "i = 0\n"
    "while True:\n"
    "    with open(path, 'a') as fh:\n"
    "        fh.write(str(i) + '\\n')\n"
    "    i += 1\n"
    "    time.sleep(0.05)\n"
)


_FAKE_AGY_SOURCE = (
    "import os, sys, time\n"
    "if '--version' in sys.argv:\n"
    "    print('fake-agy 1.0')\n"
    "    raise SystemExit(0)\n"
    "if '--print' in sys.argv:\n"
    "    profile = os.environ.get('AGYDRA_PROFILE', '')\n"
    "    tag = profile.upper()\n"
    "    child_log = os.environ.get('FAKE_AGY_USAGE_SPAWN_CHILD_LOG')\n"
    "    if child_log:\n"
    "        import subprocess\n"
    "        heartbeat = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'heartbeat_child.py')\n"
    "        subprocess.Popen([sys.executable, heartbeat, child_log])\n"
    "    sleep_s = (\n"
    "        os.environ.get('FAKE_AGY_USAGE_SLEEP_' + tag)\n"
    "        or os.environ.get('FAKE_AGY_USAGE_SLEEP')\n"
    "    )\n"
    "    if sleep_s:\n"
    "        time.sleep(float(sleep_s))\n"
    "    log_path = os.environ.get('FAKE_AGY_USAGE_LOG')\n"
    "    if log_path:\n"
    "        hold = float(os.environ.get('FAKE_AGY_USAGE_HOLD', '0'))\n"
    "        with open(log_path, 'a') as fh:\n"
    "            fh.write(profile + ' start\\n')\n"
    "        time.sleep(hold)\n"
    "        with open(log_path, 'a') as fh:\n"
    "            fh.write(profile + ' end\\n')\n"
    "    response_file = (\n"
    "        os.environ.get('FAKE_AGY_USAGE_RESPONSE_FILE_' + tag)\n"
    "        or os.environ.get('FAKE_AGY_USAGE_RESPONSE_FILE')\n"
    "    )\n"
    "    exit_code = int(\n"
    "        os.environ.get('FAKE_AGY_USAGE_EXIT_' + tag)\n"
    "        or os.environ.get('FAKE_AGY_USAGE_EXIT', '0')\n"
    "    )\n"
    "    if response_file:\n"
    "        with open(response_file) as fh:\n"
    "            sys.stdout.write(fh.read())\n"
    "    raise SystemExit(exit_code)\n"
    "if '--hold' in sys.argv:\n"
    "    print('HOLDING', flush=True)\n"
    "    gate = os.environ.get('FAKE_AGY_GATE')\n"
    "    if gate:\n"
    "        while not os.path.exists(gate):\n"
    "            time.sleep(0.05)\n"
    "    else:\n"
    "        sys.stdin.readline()\n"
    "home = os.environ.get('USERPROFILE' if os.name == 'nt' else 'HOME', '/')\n"
    "data = os.path.join(home, '.gemini')\n"
    "os.makedirs(data, exist_ok=True)\n"
    "with open(os.path.join(data, 'fake-agy-wrote'), 'w') as fh:\n"
    "    fh.write('wrote')\n"
    "print('PROFILE=' + os.environ.get('AGYDRA_PROFILE', '-'))\n"
    "print('HOME=' + home)\n"
)


def write_fake_agy(path: Path) -> Path:
    """Create a fake `agy` that mimics the real one's home-derived data dir.

    Returns the launcher actually written: the executable Python script
    ``agy`` on POSIX, or ``agy.cmd`` on Windows. Both run the same Python
    source (``agy-fake.py`` on Windows) so output, exit codes and data-dir
    behaviour are identical on every OS; the ``.cmd`` only forwards its
    arguments and the exit code to ``sys.executable``. The fake:
    - resolves its data dir from HOME (POSIX) / USERPROFILE (Windows),
    - writes a sentinel file under <home>/.gemini/fake-agy-wrote,
    - supports `--version`, `--print` (usage) and `--hold` like the real CLI,
    - prints the profile env var so tests can assert redirection happened,
    - with FAKE_AGY_USAGE_SPAWN_CHILD_LOG spawns a grandchild
      (``heartbeat_child.py``) that keeps logging until something kills it:
      proves a timeout kills the whole process group.

    The Windows launcher is limited to arguments without cmd.exe
    metacharacters (``& | < > ^ %``); the tests only pass plain tokens.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "heartbeat_child.py").write_text(_FAKE_AGY_HEARTBEAT_CHILD, encoding="utf-8")
    if sys.platform.startswith("win"):
        program = path.with_name("agy-fake.py")
        program.write_text(_FAKE_AGY_SOURCE, encoding="utf-8")
        launcher = path.with_suffix(".cmd")
        launcher.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{program}" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
        return launcher
    path.write_text("#!/usr/bin/env python3\n" + _FAKE_AGY_SOURCE, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class BaseCase(unittest.TestCase):
    """Isolated environment: fake home + agydra store + fake agy binary.

    The working directory is deliberately NOT moved: ``resolver`` walks the
    cwd and every parent looking for the ``.agydra`` project pin, so a real
    pin in the checkout must stay observable and be handled per test. Run the
    suite from a directory outside the checkout (with ``PYTHONPATH`` pointing
    at it) so no pin leaks in.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="agydra-test-"))
        self.fake_home = self._tmp / "home"
        self.store_root = self._tmp / "store"
        self.bin_dir = self._tmp / "bin"
        self.local_app_data = self._tmp / "localappdata"
        self.xdg_data_home = self._tmp / "xdg-data"
        for d in (
            self.fake_home,
            self.store_root,
            self.bin_dir,
            self.local_app_data,
            self.xdg_data_home,
        ):
            d.mkdir(parents=True)

        self._old_env = dict(os.environ)
        os.environ["AGYDRA_HOME"] = str(self.store_root)
        os.environ["HOME"] = str(self.fake_home)
        os.environ["AGYDRA_REAL_HOME"] = str(self.fake_home)
        os.environ.pop("AGYDRA_PROFILE", None)
        for variable in HOST_ENGINE_VARIABLES:
            os.environ.pop(variable, None)
        os.environ["XDG_DATA_HOME"] = str(self.xdg_data_home)
        os.environ["XDG_CONFIG_HOME"] = str(self._tmp / "xdg-config")
        os.environ["XDG_CACHE_HOME"] = str(self._tmp / "xdg-cache")
        os.environ["XDG_STATE_HOME"] = str(self._tmp / "xdg-state")
        os.environ["LOCALAPPDATA"] = str(self.local_app_data)
        os.environ["USERPROFILE"] = str(self.fake_home)

        generic = self.fake_home / ".gemini" / "antigravity-cli"
        generic.mkdir(parents=True)
        (generic / "antigravity-oauth-token").write_text(
            '{"access_token": "generic-token", "refresh_token": "generic-r"}',
            encoding="utf-8",
        )
        (self.fake_home / ".gemini" / "google_accounts.json").write_text(
            '{"active": "generic@example.com"}', encoding="utf-8"
        )
        (self.fake_home / ".gitconfig").write_text("[user]\n", encoding="utf-8")

        self.agy_bin = write_fake_agy(self.bin_dir / "agy")
        os.environ["AGYDRA_AGY_BIN"] = str(self.agy_bin)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_cli(self, *args, cwd=None):
        """Run agydra's CLI in a subprocess (execvpe replaces the process)."""
        return run_cli(*args, cwd=cwd or self._tmp)


@contextlib.contextmanager
def isolated_store_env():
    """Pin AGYDRA_HOME to a fresh temp dir and restore the old value after.

    Shared by tests that build a bare Store against a throwaway root; keeps
    the save/restore dance in exactly one place (DRY)."""
    env_keys = (
        "AGYDRA_HOME",
        "AGYDRA_REAL_HOME",
        "HOME",
        "LOCALAPPDATA",
        "XDG_DATA_HOME",
    )
    if sys.platform.startswith("win"):
        env_keys += ("USERPROFILE",)
    old = {key: os.environ.get(key) for key in env_keys}
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    home = root / "home"
    local_app_data = root / "localappdata"
    xdg_data_home = root / "xdg-data"
    for directory in (home, local_app_data, xdg_data_home):
        directory.mkdir()
    try:
        os.environ["AGYDRA_HOME"] = td.name
        os.environ["AGYDRA_REAL_HOME"] = str(home)
        os.environ["HOME"] = str(home)
        os.environ["LOCALAPPDATA"] = str(local_app_data)
        os.environ["XDG_DATA_HOME"] = str(xdg_data_home)
        if sys.platform.startswith("win"):
            os.environ["USERPROFILE"] = str(home)
        yield td.name
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        td.cleanup()


@contextlib.contextmanager
def simulated_macos_keychain():
    """Simulate a macOS environment with an available Keychain bridge."""
    from unittest import mock

    import keychain
    import platforms

    with mock.patch.object(platforms, "is_macos", return_value=True), \
            mock.patch.object(keychain, "supported", return_value=True):
        yield
