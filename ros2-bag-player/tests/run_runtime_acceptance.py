#!/usr/bin/env python3
"""Run the native ROS replay acceptance gate; missing ROS/skipped tests must fail CI."""
from pathlib import Path
import sys
import unittest


def accepted(result, count):
    return count >= 13 and result.wasSuccessful() and not result.skipped


def main():
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent),
                                               pattern="test_*runtime.py")
    count = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not accepted(result, count):
        print(f"native replay acceptance requires at least 13 passing cases and no skips; "
              f"discovered={count}, skipped={len(result.skipped)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
