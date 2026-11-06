"""The native CI gate must not pass merely because ROS is unavailable."""
from types import SimpleNamespace
import unittest

from run_runtime_acceptance import MINIMUM_CASES, accepted


class RuntimeAcceptanceGate(unittest.TestCase):
    def test_requires_complete_successful_unskipped_suite(self):
        result = SimpleNamespace(wasSuccessful=lambda: True, skipped=[])
        self.assertEqual(MINIMUM_CASES, 17)
        self.assertTrue(accepted(result, 17))
        self.assertTrue(accepted(result, 18))
        self.assertFalse(accepted(result, 16))
        self.assertFalse(accepted(result, 13))
        result.skipped = [("case", "ROS missing")]
        self.assertFalse(accepted(result, 17))
        result.skipped = []
        result.wasSuccessful = lambda: False
        self.assertFalse(accepted(result, 17))


if __name__ == "__main__":
    unittest.main()
