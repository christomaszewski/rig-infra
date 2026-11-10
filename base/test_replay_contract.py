"""Host-side guards for the build-verified player capability declaration."""
import json
from pathlib import Path
import re
import subprocess
import unittest

import check_replay_runtime as check


ROOT = Path(__file__).resolve().parent.parent
LABELS = ('LABEL io.rig.rosbag2.replay="1"', 'LABEL io.rig.rosbag2.multi_input="1"',
          'LABEL io.rig.replay.coordination="1"')
MARKER = "/replay-gate/replay-runtime-checked"


def dockerfile_stages(text):
    """Stage name -> (parent, instructions), comments dropped and continuation lines joined."""
    stages, name, parts = {}, None, []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts.append(line.strip().rstrip("\\").strip())
        if line.rstrip().endswith("\\"):
            continue
        instruction, parts = " ".join(parts), []
        start = re.fullmatch(r"FROM\s+(\S+)\s+AS\s+(\S+)", instruction)
        if start:
            name = start[2]
            stages[name] = (start[1], [])
        elif name:
            stages[name][1].append(instruction)
    return stages


class ReplayContract(unittest.TestCase):
    def setUp(self):
        self.stages = dockerfile_stages((ROOT / "base/Dockerfile").read_text())

    def gate_commands(self):
        parent, steps = self.stages["replay-gate"]
        (run,) = [step for step in steps if step.startswith("RUN ")]
        return parent, run, [command.strip() for command in run.split("&&")]

    def test_required_cli_flags_are_complete_tokens(self):
        help_text = "\n".join(sorted(check.PLAY_FLAGS))
        check.require_flags(help_text, check.PLAY_FLAGS)
        with self.assertRaisesRegex(RuntimeError, "--service-requests-source"):
            check.require_flags(help_text.replace("--service-requests-source", "--service-requests-source-other"),
                                check.PLAY_FLAGS)

    def test_image_label_follows_runtime_validation_before_stage_forks(self):
        # install -> check -> marker, in the gate; marker -> labels, in the stage the targets fork from.
        installed = "\n".join(self.stages["packages"][1])
        self.assertIn("ros-${ROS_DISTRO}-rosbag2-storage-mcap", installed)
        parent, run, commands = self.gate_commands()
        self.assertEqual(parent, "packages")  # the gate sees exactly the packages the image ships
        # Every command is chained with &&: a failed check cannot reach the marker.
        self.assertNotRegex(run, r";|\|\|")
        self.assertLess(commands.index("python3 /check_replay_runtime.py"),
                        next(i for i, command in enumerate(commands) if command.endswith("> " + MARKER)))
        parent, steps = self.stages["runtime-deps"]
        self.assertEqual(parent, "packages")
        self.assertRegex(steps[0], rf"^COPY --from=replay-gate {MARKER} \S+$")
        self.assertEqual([step for step in steps if step.startswith("LABEL io.rig.")], list(LABELS))
        # Only that stage is labelled, and every target is built from it.
        for name, (parent, steps) in self.stages.items():
            if name not in ("packages", "replay-gate", "runtime-deps"):
                self.assertEqual(parent, "runtime-deps", name)
                self.assertFalse([step for step in steps if step.startswith("LABEL io.rig.")], name)
        self.assertIn("runtime", self.stages)

    def test_check_script_edits_cannot_change_the_image(self):
        # The script reaches the build as a bind mount of that one file, in a stage no image is
        # built from; what the image receives is a literal the script does not influence. An edit
        # of the script then re-runs the gate and leaves every layer of the image cached, so the
        # images built FROM the base are not rebuilt.
        _, run, commands = self.gate_commands()
        self.assertEqual(re.findall(r"--mount=(\S+)", run),
                         ["type=bind,source=check_replay_runtime.py,target=/check_replay_runtime.py"])
        (marker,) = [command for command in commands if MARKER in command]
        self.assertRegex(marker, rf"^printf '[^'$`]*' > {MARKER}$")
        for name, (parent, steps) in self.stages.items():
            self.assertNotEqual(parent, "replay-gate", name)
            for step in steps:
                if step.startswith(("COPY ", "ADD ")):
                    self.assertNotIn("check_replay_runtime", step, name)
                if name != "replay-gate" and "replay-gate" in step:
                    self.assertEqual((name, step), ("runtime-deps", self.stages["runtime-deps"][1][0]))

    def test_launcher_declares_required_label_before_any_config_or_docker_access(self):
        result = subprocess.run([str(ROOT / "ros2-bag-player/ros2-bag-player-up"),
                                 "/does-not-exist.yaml", "replay-capabilities"],
                                capture_output=True, text=True, check=True)
        capabilities = json.loads(result.stdout)
        self.assertEqual(capabilities["image_labels"], {"bag-player": {"io.rig.rosbag2.replay": "1"}})
        for capability in ("plan", "remap", "clock_only", "reindex"):
            self.assertTrue(capabilities[capability])
        self.assertEqual(capabilities["inspect_calls"], 2)
        self.assertEqual(capabilities["plan"], 2)
        self.assertEqual(capabilities["multi_input"], 1)
        self.assertEqual(capabilities["multi_input_image_labels"],
                         {"bag-player": {"io.rig.rosbag2.multi_input": "1"}})
        self.assertEqual(capabilities["coordination"], {"version": 1,
            "components": ["player", "calls", "readiness"],
            "image_labels": {"bag-player": {"io.rig.replay.coordination": "1"}}})


if __name__ == "__main__":
    unittest.main()
