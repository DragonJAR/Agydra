from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
import platforms
import runner
from conftest import BaseCase, _make_jwt
from store import Store


class TestRunnerStartState(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store()
        self.profile = self.store.create("anchor")
        self.profile.email = "alice@example.com"
        self.store.save(self.profile)
        self.data_dir = self.store.profile_data_dir(self.profile.name, engine="agy")
        account.write_agy_token(
            self.data_dir,
            json.dumps(
                {
                    "token": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"},
                    "id_token": _make_jwt({"email": "bob@example.com"}),
                }
            ).encode("utf-8"),
        )
        self.plan = runner.LaunchPlan(
            profile=self.profile.name,
            reason="FYF19 test",
            binary=self.bin_dir / "agy",
            args=[],
            overlay=self.store.overlays_dir / self.profile.name,
            env_home_var="HOME",
            env_home_value=self.store.overlays_dir / self.profile.name,
            use_sandbox=False,
            engine="agy",
        )

    def test_launch_failure_does_not_reanchor_profile_email(self):
        with mock.patch.object(
            platforms, "launch_argv", side_effect=FileNotFoundError("synthetic exec failure")
        ):
            with self.assertRaises(FileNotFoundError):
                runner._run_prepared(self.plan, self.store)

        self.assertEqual(self.store.get(self.profile.name).email, "alice@example.com")

    def test_exec_failure_status_does_not_reanchor_profile_email(self):
        with mock.patch.object(platforms, "launch_argv", return_value=127):
            self.assertEqual(runner._run_prepared(self.plan, self.store), 127)

        self.assertEqual(self.store.get(self.profile.name).email, "alice@example.com")

    def test_started_waited_child_reanchors_profile_email_on_exit(self):
        self.plan.launch_as_child = True
        def start_and_exit(_argv, _env, *, on_started):
            on_started()
            return 0

        with mock.patch.object(platforms, "run_wait", side_effect=start_and_exit):
            with mock.patch.object(platforms, "drain_tty_input"):
                self.assertEqual(runner._run_prepared(self.plan, self.store), 0)

        self.assertEqual(self.store.get(self.profile.name).email, "bob@example.com")

    def test_started_waited_child_exit_127_reanchors_profile_email(self):
        self.plan.launch_as_child = True

        def start_and_exit(_argv, _env, *, on_started):
            on_started()
            return 127

        with mock.patch.object(platforms, "run_wait", side_effect=start_and_exit):
            with mock.patch.object(platforms, "drain_tty_input"):
                self.assertEqual(runner._run_prepared(self.plan, self.store), 127)

        self.assertEqual(self.store.get(self.profile.name).email, "bob@example.com")

    def test_waited_spawn_failure_exit_127_does_not_reanchor_profile_email(self):
        self.plan.launch_as_child = True
        with mock.patch.object(platforms, "run_wait", return_value=127):
            with mock.patch.object(platforms, "drain_tty_input"):
                self.assertEqual(runner._run_prepared(self.plan, self.store), 127)

        self.assertEqual(self.store.get(self.profile.name).email, "alice@example.com")

    def test_rotation_failure_before_launch_does_not_reanchor_profile_email(self):
        class Rotation:
            def config(inner_self):
                return self.store.load_config()

            def commit(inner_self, _profile):
                raise OSError("synthetic rotation persistence failure")

        self.plan.random_pick = True
        with mock.patch.object(
            platforms, "launch_argv", side_effect=AssertionError("child must not start")
        ):
            with self.assertRaisesRegex(OSError, "synthetic rotation persistence failure"):
                runner._run_prepared(self.plan, self.store, Rotation())

        self.assertEqual(self.store.get(self.profile.name).email, "alice@example.com")
