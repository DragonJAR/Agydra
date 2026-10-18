"""Regressions for the MEJORA wave: shared HTTP budget, probe cleanup
discipline, doctor warning consolidation and repair error frontiers."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_usage
import doctor
import i18n
import store as store_mod
import usage
from conftest import BaseCase
from store import PersistentStateDeclaration, Store


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, size=-1):
        if size is not None and size >= 0:
            return self._payload[:size]
        return self._payload


class TestSharedHttpBudget(unittest.TestCase):
    def test_oversized_json_response_is_refused_without_parsing(self):
        payload = b"{" + b" " * (usage.MAX_USAGE_RESPONSE_BYTES + 10)
        with self.assertRaises(usage.UsageResponseError) as raised:
            usage._read_json_object_response(
                _FakeResponse(payload), "usage test source"
            )
        self.assertIn(
            i18n.t("usage.response_too_large", source="usage test source"),
            str(raised.exception),
        )

    def test_response_exactly_at_the_budget_still_parses(self):
        pad = usage.MAX_USAGE_RESPONSE_BYTES - len(b'{"pad": ""}')
        payload = b'{"pad": "' + b"x" * pad + b'"}'
        self.assertEqual(len(payload), usage.MAX_USAGE_RESPONSE_BYTES)
        parsed = usage._read_json_object_response(
            _FakeResponse(payload), "usage test source"
        )
        self.assertEqual(parsed, {"pad": "x" * pad})

    def test_small_invalid_json_keeps_the_invalid_response_error(self):
        with self.assertRaises(usage.UsageResponseError) as raised:
            usage._read_json_object_response(
                _FakeResponse(b"{invalid"), "usage test source"
            )
        self.assertIn(
            i18n.t("usage.response_invalid_json", source="usage test source"),
            str(raised.exception),
        )


class TestStoreProbeCleanup(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)

    def test_probe_unlink_runs_even_when_the_descriptor_close_fails(self):
        """The probe temp is always unlink-attempted when close fails.

        Windows cannot remove a file whose descriptor is still open, so a
        genuinely failed close keeps the probe until process exit there;
        POSIX removes it, and both platforms still report the close error
        as the store-check failure.
        """
        with mock.patch.object(
            doctor.os, "close", side_effect=OSError("close denied")
        ):
            status, message = doctor._check_store(self.store, None)
        self.assertEqual(status, doctor.FAIL)
        self.assertIn(
            i18n.t(
                "doctor.store.fail",
                path=self.store.root,
                error=OSError("close denied"),
            ),
            message,
        )
        leftovers = list(self.store.root.glob(".write-probe.*"))
        if sys.platform == "win32":
            self.assertEqual(len(leftovers), 1)
        else:
            self.assertEqual(leftovers, [])

    def test_probe_unlink_retries_once_through_a_transient_holder(self):
        original = Path.unlink
        attempts = []

        def flaky(self_path, missing_ok=False):
            if self_path.name.startswith(".write-probe."):
                attempts.append(self_path.name)
                if len(attempts) == 1:
                    raise OSError("transient holder")
                return original(self_path, missing_ok=missing_ok)
            return original(self_path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", flaky):
            status, message = doctor._check_store(self.store, None)
        self.assertEqual(status, doctor.OK)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(list(self.store.root.glob(".write-probe.*")), [])

    def test_healthy_store_probe_leaves_no_probe_artifacts(self):
        status, message = doctor._check_store(self.store, None)
        self.assertEqual(status, doctor.OK)
        self.assertEqual(list(self.store.root.glob(".write-probe.*")), [])


class TestDoctorIsolationConsolidation(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)

    def test_recoverable_overlays_are_reported_alongside_claude_orphans(self):
        self.store.create("agy-profile", engine="agy")
        overlay = self.store.overlays_dir / "agy-profile"
        (overlay / ".gemini").mkdir(parents=True, exist_ok=True)
        self.store.claude_config_root.mkdir(parents=True, exist_ok=True)
        (self.store.claude_config_root / "777").mkdir()
        ctx = doctor._DoctorContext(
            scan=self.store.scan(), names=self.store.names()
        )
        status, message = doctor._check_isolation(self.store, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertIn(
            i18n.t("doctor.isolation.recoverable", names="agy-profile"), message
        )
        self.assertIn(
            i18n.t(
                "doctor.isolation.claude_orphans",
                entries=store_mod.store_relative_path("claude-config", "777"),
            ),
            message,
        )

    def test_full_pending_overlays_are_reported_alongside_claude_orphans(self):
        self.store.create("fresh", engine="agy")
        self.store.claude_config_root.mkdir(parents=True, exist_ok=True)
        (self.store.claude_config_root / "888").mkdir()
        ctx = doctor._DoctorContext(
            scan=self.store.scan(), names=self.store.names()
        )
        status, message = doctor._check_isolation(self.store, ctx)
        self.assertEqual(status, doctor.WARN)
        self.assertIn(
            i18n.t("doctor.isolation.pending", names="fresh"), message
        )
        self.assertIn(
            i18n.t(
                "doctor.isolation.claude_orphans",
                entries=store_mod.store_relative_path("claude-config", "888"),
            ),
            message,
        )


class TestDoctorRepairErrorFrontier(BaseCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.store_root)
        self.ctx = doctor._DoctorContext(
            scan=self.store.scan(), names=self.store.names()
        )

    def _declaration(self):
        return PersistentStateDeclaration(
            name="lease registry",
            path=self.store_root / "locks" / "x.lock",
            recovery="quarantine",
            validate=lambda: "unreadable",
            repair=lambda fd=None, replacement=None: True,
        )

    def test_programming_errors_in_repair_propagate_instead_of_warning(self):
        declaration = self._declaration()
        with mock.patch.object(
            self.store,
            "persistent_state_issues",
            return_value=[(declaration, "unreadable")],
        ), mock.patch.object(
            self.store, "repair_persistent_state", side_effect=TypeError("bug")
        ):
            with self.assertRaises(TypeError):
                doctor._apply_fixes(self.store, self.ctx)

    def test_disk_failures_in_repair_warn_and_keep_fixing(self):
        declaration = self._declaration()
        with mock.patch.object(
            self.store,
            "persistent_state_issues",
            return_value=[(declaration, "unreadable")],
        ), mock.patch.object(
            self.store, "repair_persistent_state", side_effect=OSError("disk full")
        ), mock.patch.object(doctor, "warn") as warn:
            doctor._apply_fixes(self.store, self.ctx)
        expected_name = i18n.t(
            "doctor.persistent.name.lease_registry", default="lease registry"
        )
        self.assertTrue(any(
            expected_name in str(call.args[0])
            and str(declaration.path) in str(call.args[0])
            and "disk full" in str(call.args[0])
            for call in warn.call_args_list
        ))


class TestUsageResetZoneFrontier(unittest.TestCase):
    def test_unknown_zone_degrades_to_an_unknown_reset(self):
        text = "Current session: 50% used · resets 5:30pm (Nowhere/Zone)"
        windows = claude_usage.parse_usage_text(
            text, datetime(2026, 10, 18, tzinfo=timezone.utc)
        )
        self.assertEqual(
            windows["rate_limits"]["five_hour"]["resets_at"], None
        )

    def test_builtin_utc_zone_still_resolves(self):
        text = "Current session: 50% used · resets 5:30pm (UTC)"
        windows = claude_usage.parse_usage_text(
            text, datetime(2026, 10, 18, tzinfo=timezone.utc)
        )
        reset = windows["rate_limits"]["five_hour"]["resets_at"]
        self.assertIsNotNone(reset)
        self.assertIsNotNone(usage.parse_iso_utc(reset))


if __name__ == "__main__":
    unittest.main()
