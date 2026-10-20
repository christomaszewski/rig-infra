"""The structured replay handoff, including empty data and remapped latch selection."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import play_cmd
import clock_only
import reindex


class ReplayPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bag = self.root / "source/bags/logger/exact"
        self.bag.mkdir(parents=True)
        (self.bag / "bag.mcap").write_bytes(b"test")
        (self.bag / "metadata.yaml").write_text(yaml.safe_dump({
            "rosbag2_bagfile_information": {
                "starting_time": {"nanoseconds_since_epoch": 100_000_000_000},
                "duration": {"nanoseconds": 10_000_000_000}}}))
        self.plan = dict(version=1, source=str(self.root / "source"),
                         bag={"logger": "logger", "session": "exact"}, topics=["/old"],
                         remap={"/old": "/new"}, services=[])
        self.path = self.root / "plan.json"
        self.cfg = {"name": "player", "source": {"logger": "wrong", "session": "latest"},
                    "play": {"topics": ["/wrong"], "services": ["/unintended"], "exclude": [".*"]},
                    "calls": "/unintended.yaml"}

    def build(self, **env):
        self.path.write_text(json.dumps(self.plan))
        return play_cmd.build_args(self.cfg, {"RIG_REPLAY_PLAN": str(self.path), **env})

    def test_plan_pins_bag_and_clears_standalone_calls(self):
        _, source, bag, args, _, extras = self.build()
        self.assertEqual(source, self.plan["source"])
        self.assertEqual(bag, "/replay/bags/logger/exact")
        self.assertEqual(args, ["--remap", "/old:=/new", "--topics", "/old"])
        self.assertEqual(extras["calls"], "")
        self.assertEqual(extras["bag_start_ns"], 100_000_000_000)
        self.assertNotIn("--publish-service-requests", args)

    def test_latch_uses_input_selector_and_output_remap(self):
        _, _, _, args, _, extras = self.build(RIG_REPLAY_FROM_S="2")
        self.assertEqual(extras["latch"], ["--from", "2", "--remap", "/old:=/new", "--topics", "/old"])
        self.assertLess(args.index("--remap"), args.index("--topics"))

    def test_empty_topics_emit_only_timeline(self):
        self.plan.update(topics=[], remap={})
        self.build()
        result = play_cmd.render(self.cfg, {"RIG_REPLAY_PLAN": str(self.path), "RIG_SIM_TIME": "1",
                                           "RIG_REPLAY_START_AT_UNIX_S": "1000",
                                           "RIG_REPLAY_FROM_S": "2", "RIG_REPLAY_TO_S": "5"}, self.root)
        script = Path(result[1]).read_text()
        self.assertIn("exec python3 /clock_only.py", script)
        self.assertNotIn("exec ros2 bag play", script)
        self.assertNotIn("/release_gate.py", script)
        self.assertIn("--from 2.0 --to 5.0", script)
        self.assertEqual(clock_only.stamp(100_000_000_000, 2, .5, 2, 5), 103_000_000_000)
        self.assertEqual(clock_only.stamp(100_000_000_000, 2, 99, 2, 5), 105_000_000_000)

    def test_calls_and_verbatim_services_are_explicit(self):
        self.plan["services"] = ["/control"]
        args = self.build()[3]
        self.assertIn("--publish-service-requests", args)
        self.assertIn("/control", args)
        self.plan["calls"] = "/calls.yaml"
        with self.assertRaisesRegex(SystemExit, "mutually exclusive"):
            self.build()

    def test_conflicts_and_protected_remaps_refuse(self):
        with self.assertRaisesRegex(SystemExit, "conflicts"):
            self.build(RIG_REPLAY_TOPICS="/unexpected")
        with self.assertRaisesRegex(SystemExit, "mismatch"):
            self.build(RIG_REPLAY_SOURCE="/different")
        self.plan["remap"] = {"/old": "/clock"}
        with self.assertRaisesRegex(SystemExit, "plumbing"):
            self.build()
        self.plan.update(topics=["/old", "/new"], remap={"/old": "/new"})
        with self.assertRaisesRegex(SystemExit, "collision"):
            self.build()

    def test_plan_schema_and_changed_bag_timing_refuse(self):
        self.plan["unexpected"] = True
        with self.assertRaisesRegex(SystemExit, "unknown"):
            self.build()
        del self.plan["unexpected"]
        self.plan["bag"]["start_ns"] = 99
        with self.assertRaisesRegex(SystemExit, "metadata changed"):
            self.build()
        self.plan["version"] = True
        with self.assertRaisesRegex(SystemExit, "version"):
            self.build()

    def test_storage_helper_has_no_network_or_pull_and_never_starts_compose(self):
        args = reindex.command(self.bag, {"RIG_BASE_IMAGE": "fleet@sha256:abc"})
        self.assertIn("--network=none", args)
        self.assertIn("--pull=never", args)
        self.assertIn("fleet@sha256:abc", args)
        self.assertNotIn("compose", args)
        self.assertEqual(args[-1], "mcap")


if __name__ == "__main__":
    unittest.main()
