"""Host-side guards for the build-verified player capability declaration."""
import json
from pathlib import Path
import subprocess
import unittest

import check_replay_runtime as check


ROOT = Path(__file__).resolve().parent.parent


class ReplayContract(unittest.TestCase):
    def test_required_cli_flags_are_complete_tokens(self):
        help_text = "\n".join(sorted(check.PLAY_FLAGS))
        check.require_flags(help_text, check.PLAY_FLAGS)
        with self.assertRaisesRegex(RuntimeError, "--service-requests-source"):
            check.require_flags(help_text.replace("--service-requests-source", "--service-requests-source-other"),
                                check.PLAY_FLAGS)

    def test_image_label_follows_runtime_validation_before_stage_forks(self):
        dockerfile = (ROOT / "base/Dockerfile").read_text()
        install = dockerfile.index("ros-${ROS_DISTRO}-rosbag2-storage-mcap")
        copied = dockerfile.index("COPY check_replay_runtime.py")
        checked = dockerfile.index("python3 /usr/local/lib/rig/check_replay_runtime.py")
        label = dockerfile.index('LABEL io.rig.rosbag2.replay="1"')
        self.assertLess(install, copied)
        self.assertLess(copied, checked)
        self.assertLess(checked, label)
        self.assertLess(checked, dockerfile.index('LABEL io.rig.replay.coordination="1"'))
        # A runtime-only base and an optional SDK-stage build both support this contract.
        if "FROM runtime-deps AS sdk" in dockerfile:
            self.assertLess(label, dockerfile.index("FROM runtime-deps AS sdk"))

    def test_launcher_declares_required_label_before_any_config_or_docker_access(self):
        result = subprocess.run([str(ROOT / "ros2-bag-player/ros2-bag-player-up"),
                                 "/does-not-exist.yaml", "replay-capabilities"],
                                capture_output=True, text=True, check=True)
        capabilities = json.loads(result.stdout)
        self.assertEqual(capabilities["image_labels"], {"bag-player": {"io.rig.rosbag2.replay": "1"}})
        for capability in ("plan", "remap", "clock_only", "reindex"):
            self.assertTrue(capabilities[capability])
        self.assertEqual(capabilities["inspect_calls"], 1)
        self.assertEqual(capabilities["coordination"], {"version": 1,
            "components": ["player", "calls", "readiness"],
            "image_labels": {"bag-player": {"io.rig.replay.coordination": "1"}}})


if __name__ == "__main__":
    unittest.main()
