"""Tests for xAI Grok CLI engine integration."""
import base64
import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import account
import cli
import engines
import isolation
import platforms
import store
import usage


def _make_dummy_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{header}.{body}."


class TestGrokEngine(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="agydra-grok-test-"))
        self.home = self._tmp / "home"
        self.home.mkdir()
        self.store_root = self._tmp / "store"
        self.store = store.Store(self.store_root)

        self._orig_env = dict(os.environ)
        os.environ["HOME"] = str(self.home)
        os.environ["AGYDRA_HOME"] = str(self.store_root)
        os.environ["AGYDRA_REAL_HOME"] = str(self.home)
        os.environ.pop(platforms.GROK_BIN_ENV, None)
        os.environ.pop("GROK_HOME", None)
        os.environ.pop("GROK_LEADER_SOCKET", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_engine_driver_metadata(self):
        driver = engines.get_engine("grok")
        self.assertEqual(driver.name, "grok")
        self.assertEqual(driver.binary_name, "grok")
        self.assertEqual(driver.data_dir_name, ".grok")
        self.assertEqual(driver.env_home_var, "GROK_HOME")
        self.assertEqual(driver.env_bin_var, platforms.GROK_BIN_ENV)
        self.assertFalse(driver.needs_keychain)
        self.assertEqual(driver.login_args, ("login",))

    def test_binary_resolution(self):
        # 1. Explicit path
        fake_bin = self._tmp / "fake_grok"
        fake_bin.touch(mode=0o755)
        self.assertEqual(platforms.resolve_grok_binary(str(fake_bin)), fake_bin)

        # 2. Environment variable
        os.environ[platforms.GROK_BIN_ENV] = str(fake_bin)
        self.assertEqual(platforms.resolve_grok_binary(), fake_bin)

    def test_auth_detection_oidc_tier(self):
        self.store.create("grok-user", engine="grok")
        data_dir = self.store.profile_data_dir("grok-user", engine="grok")
        jwt = _make_dummy_jwt({"email": "elon@x.ai", "tier": 4})
        auth_payload = {
            "https://auth.x.ai::client_123": {
                "email": "elon@x.ai",
                "key": jwt,
                "refresh_token": "refresh_xyz",
            }
        }
        (data_dir / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")

        self.assertEqual(account.inspect_grok_auth(data_dir)["email"], "elon@x.ai")
        self.assertEqual(account.detect_grok_email(data_dir), "elon@x.ai")
        self.assertEqual(account.detect_grok_plan(data_dir), "SuperGrok")
        self.assertEqual(account.auth_state(data_dir, self.store, "grok-user", engine="grok"), "authenticated")

    def test_auth_detection_api_key(self):
        self.store.create("grok-api", engine="grok")
        data_dir = self.store.profile_data_dir("grok-api", engine="grok")
        auth_payload = {"XAI_API_KEY": "xai-test-key-12345"}
        (data_dir / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")

        auth_info = account.inspect_grok_auth(data_dir)
        self.assertEqual(auth_info["auth_type"], "api_key")
        self.assertIsNone(account.detect_grok_email(data_dir))
        self.assertEqual(account.detect_grok_plan(data_dir), "xAI API Key")
        self.assertEqual(account.auth_state(data_dir, self.store, "grok-api", engine="grok"), "authenticated")

    def test_auth_state_not_authenticated(self):
        self.store.create("grok-empty", engine="grok")
        data_dir = self.store.profile_data_dir("grok-empty", engine="grok")
        self.assertEqual(account.auth_state(data_dir, self.store, "grok-empty", engine="grok"), "not-authenticated")
        self.assertIsNone(account.detect_grok_email(data_dir))
        self.assertIsNone(account.detect_grok_plan(data_dir))

    def test_overlay_creation_and_env(self):
        self.store.create("gk", engine="grok")
        data_dir = self.store.profile_data_dir("gk", engine="grok")
        (data_dir / "config.toml").write_text("[ui]\nscreen_mode = 'minimal'\n", encoding="utf-8")

        overlay = isolation.build_overlay("gk", data_dir, self.store.root, engine="grok")
        grok_link = overlay / ".grok"
        self.assertTrue(grok_link.exists())
        self.assertTrue(isolation.link_points_to(grok_link, data_dir))

        env = isolation.isolated_env(overlay, {}, engine="grok")
        self.assertEqual(env.get("GROK_HOME"), str(overlay / ".grok"))
        preferred = str(overlay / ".grok" / "leader.sock")
        sock = env.get("GROK_LEADER_SOCKET")
        # macOS sun_path holds 103 usable bytes. A temp store under
        # /var/folders already exceeds that; the socket must still bind.
        if platforms.is_windows() or len(os.fsencode(preferred)) <= 103:
            self.assertEqual(sock, preferred)
        else:
            self.assertNotEqual(sock, preferred)
            self.assertLessEqual(len(os.fsencode(sock)), 103)
            self.assertTrue(sock.startswith(f"/tmp/agydra-{os.getuid()}/"))
            self.assertFalse(sock.startswith(str(self.home)))

    def test_grok_leader_socket_stays_on_overlay_when_it_fits(self):
        if platforms.is_windows():
            self.skipTest("sun_path limit is POSIX-only")
        overlay = Path("/tmp/agy-ov/gk")
        preferred = str(overlay / ".grok" / "leader.sock")
        self.assertLessEqual(len(os.fsencode(preferred)), 103)
        env = isolation.isolated_env(overlay, {}, engine="grok")
        self.assertEqual(env["GROK_HOME"], str(overlay / ".grok"))
        self.assertEqual(env["GROK_LEADER_SOCKET"], preferred)

    def test_grok_leader_socket_shortens_when_overlay_path_exceeds_sun_path(self):
        if platforms.is_windows():
            self.skipTest("sun_path limit is POSIX-only")
        overlay = Path("/" + ("p" * 90) + "/work")
        other = Path("/" + ("p" * 90) + "/lab")
        preferred = str(overlay / ".grok" / "leader.sock")
        self.assertGreater(len(os.fsencode(preferred)), 103)
        env = isolation.isolated_env(overlay, {}, engine="grok")
        other_env = isolation.isolated_env(other, {}, engine="grok")
        sock = env["GROK_LEADER_SOCKET"]
        self.assertNotEqual(other_env["GROK_LEADER_SOCKET"], sock)
        self.assertNotEqual(sock, preferred)
        self.assertLessEqual(len(os.fsencode(sock)), 103)
        self.assertTrue(sock.startswith(f"/tmp/agydra-{os.getuid()}/"))
        self.assertFalse(sock.startswith(str(self.home)))
        self.assertNotIn(str(overlay), sock)
        parent = Path(sock).parent
        st = parent.lstat()
        self.assertFalse(stat.S_ISLNK(st.st_mode))
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)
        self.assertEqual(st.st_uid, os.getuid())
        # Backlog must hold isolated_env's probe connect plus the assertion.
        bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            bound.bind(sock)
            bound.listen(5)
            again = isolation.isolated_env(overlay, {}, engine="grok")
            self.assertEqual(again["GROK_LEADER_SOCKET"], sock)
            client.settimeout(1.0)
            client.connect(sock)
        finally:
            client.close()
            bound.close()
            try:
                os.unlink(sock)
            except OSError:
                pass

    def test_windows_keeps_long_grok_leader_socket_in_overlay(self):
        overlay = Path("C:/Users") / ("n" * 90) / "overlays" / "gk"
        preferred = str(overlay / ".grok" / "leader.sock")
        self.assertGreater(len(preferred), 103)
        with mock.patch.object(platforms, "is_windows", return_value=True):
            env = isolation.isolated_env(overlay, {}, engine="grok")
        self.assertEqual(env["GROK_LEADER_SOCKET"], preferred)
        self.assertEqual(env["GROK_HOME"], str(overlay / ".grok"))

    def test_usage_query(self):
        self.store.create("grok-query", engine="grok")
        data_dir = self.store.profile_data_dir("grok-query", engine="grok")

        # Not authenticated
        res = usage.query_profile_usage(self.store, "grok-query")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "not authenticated")
        self.assertEqual(res.engine, "grok")

        # Authenticated with JWT tier 3 (mocked network payload)
        jwt = _make_dummy_jwt({"email": "dev@x.ai", "tier": 3})
        (data_dir / "auth.json").write_text(
            json.dumps({"https://auth.x.ai::1": {"email": "dev@x.ai", "key": jwt}}),
            encoding="utf-8",
        )
        with unittest.mock.patch("usage.fetch_grok_billing_payload", return_value=None), \
             unittest.mock.patch("usage.fetch_grok_settings_payload", return_value=None):
            res_auth = usage.query_profile_usage(self.store, "grok-query")
            self.assertTrue(res_auth.ok)
            self.assertEqual(res_auth.engine, "grok")
            self.assertEqual(res_auth.email, "dev@x.ai")
            self.assertEqual(res_auth.plan, "Grok Pro")

    def test_cli_create_and_status(self):
        # Create profile via CLI
        ret = cli.main(["create", "gk-test", "-e", "grok", "-d", "test xAI profile"])
        self.assertEqual(ret, 0)
        p = self.store.get("gk-test")
        self.assertEqual(p.engine, "grok")
        self.assertEqual(p.description, "test xAI profile")

        # Status dry-run
        ret_status = cli.main(["status", "gk-test", "-n"])
        self.assertEqual(ret_status, 0)

    def test_cli_import_grok(self):
        host_grok = self.home / ".grok"
        host_grok.mkdir()
        (host_grok / "auth.json").write_text('{"XAI_API_KEY":"secret"}', encoding="utf-8")

        self.store.create("gk-imported", engine="grok")
        ret = cli.main(["import", "gk-imported"])
        self.assertEqual(ret, 0)

        imported_data = self.store.profile_data_dir("gk-imported", engine="grok")
        self.assertTrue((imported_data / "auth.json").is_file())
        self.assertEqual((imported_data / "auth.json").read_text(encoding="utf-8"), '{"XAI_API_KEY":"secret"}')

    def test_parse_grok_billing_payload(self):
        # 1. Standard creditUsagePercent + currentPeriod.end ISO string
        payload = {
            "config": {
                "creditUsagePercent": 2.0,
                "currentPeriod": {"end": "2026-04-03T00:00:00Z"},
            }
        }
        groups, reset_dt = usage.parse_grok_billing_payload(payload)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, "xAI Grok")
        self.assertEqual(len(groups[0].buckets), 1)
        bucket = groups[0].buckets[0]
        self.assertEqual(bucket.window, "weekly")
        self.assertAlmostEqual(bucket.remaining_fraction, 0.98, places=4)
        self.assertIsNotNone(reset_dt)
        self.assertEqual(reset_dt.year, 2026)
        self.assertEqual(reset_dt.month, 4)
        self.assertEqual(reset_dt.day, 3)

        # 2. OnDemand calculation fallback + billingPeriodEnd unix timestamp
        payload2 = {
            "config": {
                "onDemandUsed": {"val": 10},
                "onDemandCap": {"val": 50},
                "billingPeriodEnd": 1775174400,
            }
        }
        groups2, reset_dt2 = usage.parse_grok_billing_payload(payload2)
        self.assertEqual(len(groups2), 1)
        self.assertAlmostEqual(groups2[0].buckets[0].remaining_fraction, 0.80, places=4)
        self.assertIsNotNone(reset_dt2)

        # 3. Invalid payload
        self.assertEqual(usage.parse_grok_billing_payload({}), ([], None))
        self.assertEqual(usage.parse_grok_billing_payload(None), ([], None))

    def test_extract_model_summary_grok(self):
        from datetime import datetime, timezone

        reset = datetime(2026, 4, 3, 0, 0, tzinfo=timezone.utc)
        groups = [
            usage.UsageGroup(
                name="xAI Grok",
                buckets=[
                    usage.UsageBucket(
                        id="grok-weekly",
                        name="Weekly",
                        window="weekly",
                        remaining_fraction=0.75,
                        reset_time=reset,
                    )
                ],
            )
        ]
        summary = usage.extract_model_summary(groups)
        self.assertIn("grok", summary)
        self.assertEqual(summary["grok"]["weekly"], 0.75)
        self.assertEqual(summary["grok"]["available"], 0.75)
        self.assertEqual(summary["grok"]["reset_time"], reset)

    def test_query_grok_usage_mocked_network(self):
        import urllib.error
        from unittest.mock import patch

        self.store.create("grok-mock", engine="grok")
        data_dir = self.store.profile_data_dir("grok-mock", engine="grok")
        jwt = _make_dummy_jwt({"email": "dev@x.ai", "tier": 4})
        (data_dir / "auth.json").write_text(
            json.dumps({"https://auth.x.ai::1": {"email": "dev@x.ai", "key": jwt}}),
            encoding="utf-8",
        )

        mock_billing = {
            "config": {
                "creditUsagePercent": 5.0,
                "currentPeriod": {"end": "2026-04-03T00:00:00Z"},
            }
        }
        mock_settings = {"subscription_tier_display": "SuperGrok"}

        # 1. Success with billing and settings enrichment
        with patch("usage.fetch_grok_billing_payload", return_value=mock_billing), \
             patch("usage.fetch_grok_settings_payload", return_value=mock_settings):
            res = usage.query_grok_usage(data_dir, "grok-mock")
            self.assertTrue(res.ok)
            self.assertEqual(res.plan, "SuperGrok")
            self.assertEqual(res.email, "dev@x.ai")
            self.assertEqual(len(res.groups), 1)
            self.assertAlmostEqual(res.groups[0].buckets[0].remaining_fraction, 0.95, places=4)

        # 2. Offline fallback
        with patch("usage.fetch_grok_billing_payload", side_effect=urllib.error.URLError("Network down")), \
             patch("usage.fetch_grok_settings_payload", return_value=None):
            res_offline = usage.query_grok_usage(data_dir, "grok-mock")
            self.assertTrue(res_offline.ok)
            self.assertEqual(res_offline.groups, [])
            self.assertIn("offline", res_offline.error)

        # 3. 401 session expired. The error body must be closed: urllib's
        # HTTPError warns at GC when the caller leaves it open.
        expired = urllib.error.HTTPError("http://...", 401, "Unauthorized", {}, None)
        closed: list[bool] = []

        def _close() -> None:
            closed.append(True)
            urllib.error.HTTPError.close(expired)

        expired.close = _close  # type: ignore[method-assign]
        with patch("usage.fetch_grok_billing_payload", side_effect=expired):
            res_401 = usage.query_grok_usage(data_dir, "grok-mock")
            self.assertFalse(res_401.ok)
            self.assertEqual(res_401.error, "session expired (401)")
        self.assertEqual(closed, [True])

    def test_cli_usage_grok_table_and_recs(self):
        import io
        from unittest.mock import patch

        self.store.create("grok-active", engine="grok")
        data_dir = self.store.profile_data_dir("grok-active", engine="grok")
        jwt = _make_dummy_jwt({"email": "active@x.ai", "tier": 4})
        (data_dir / "auth.json").write_text(
            json.dumps({"https://auth.x.ai::1": {"email": "active@x.ai", "key": jwt}}),
            encoding="utf-8",
        )

        mock_billing = {
            "config": {
                "creditUsagePercent": 10.0,
                "currentPeriod": {"end": "2026-04-03T00:00:00Z"},
            }
        }
        mock_settings = {"subscription_tier_display": "SuperGrok"}

        with patch("usage.fetch_grok_billing_payload", return_value=mock_billing), \
             patch("usage.fetch_grok_settings_payload", return_value=mock_settings), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = cli.main(["usage"])
            self.assertEqual(ret, 0)
            out = mock_out.getvalue()
            self.assertIn("XAI GROK", out)
            self.assertIn("grok-active", out)
            self.assertIn("SuperGrok", out)
            self.assertIn("90", out)  # 90% available
            self.assertIn("Grok → grok-active 90%", out)


if __name__ == "__main__":
    unittest.main()
