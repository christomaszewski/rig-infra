"""The native CI gate must not pass merely because ROS is unavailable."""
from types import SimpleNamespace
import unittest

from run_runtime_acceptance import accepted


class RuntimeAcceptanceGate(unittest.TestCase):
    def test_requires_complete_successful_unskipped_suite(self):
        result = SimpleNamespace(wasSuccessful=lambda: True, skipped=[])
        self.assertTrue(accepted(result, 13))
        self.assertTrue(accepted(result, 14))
        self.assertFalse(accepted(result, 12))
        result.skipped = [("case", "ROS missing")]
        self.assertFalse(accepted(result, 13))
        result.skipped = []
        result.wasSuccessful = lambda: False
        self.assertFalse(accepted(result, 13))


if __name__ == "__main__":
    unittest.main()
