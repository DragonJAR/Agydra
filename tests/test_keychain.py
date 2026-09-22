"""Lightweight checks for the keychain bridge naming and descriptor shape."""
import unittest

from agydra import keychain


class TestKeychainNames(unittest.TestCase):
    def test_profile_slot_naming(self):
        self.assertEqual(keychain.profile_slot("alpha"), "gemini/agydra/alpha")
        self.assertEqual(keychain.shared_slot(), "gemini")

    def test_describe_supported_flag_shape(self):
        report = keychain.describe(None)  # type: ignore[arg-type]
        self.assertIn("supported", report)
        self.assertIsInstance(report["supported"], bool)


if __name__ == "__main__":
    unittest.main()
