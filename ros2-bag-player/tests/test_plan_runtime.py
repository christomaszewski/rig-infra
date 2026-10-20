"""Optional real ROS acceptance; run in a local fleet-ros image with network disabled.

The test creates its own tiny bags and router. It uses no vehicle hardware or external graph.
Plain host test runs skip this file when ROS is unavailable.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import play_cmd
try:
    import rclpy
    import rosbag2_py
    from rclpy.serialization import serialize_message
    from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
    from std_msgs.msg import String
    from rosgraph_msgs.msg import Clock
    import yaml
except ImportError:
    rclpy = None


@unittest.skipIf(rclpy is None, "ROS runtime acceptance requires fleet-ros")
class Runtime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.children = []
        self.router = subprocess.Popen(["ros2", "run", "rmw_zenoh_cpp", "rmw_zenohd"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_children)
        time.sleep(.5)
        rclpy.init()
        self.node = rclpy.create_node("replay_acceptance")
        self.addCleanup(self.node.destroy_node)
        self.addCleanup(lambda: rclpy.shutdown() if rclpy.ok() else None)

    def stop_children(self):
        for p in [*self.children, self.router]:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    def bag(self, storage):
        path = self.root / storage / "bags/logger/exact"
        path.parent.mkdir(parents=True)
        writer = rosbag2_py.SequentialWriter()
        writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=storage),
                    rosbag2_py.ConverterOptions("", ""))
        from rosbag2_py._storage import QoS
        for i, name in enumerate(("/old", "/old_latched")):
            qos = QoS(1)
            qos.reliable()
            if name == "/old_latched":
                qos.transient_local()
            writer.create_topic(rosbag2_py.TopicMetadata(id=i, name=name, type="std_msgs/msg/String",
                                                        serialization_format="cdr", offered_qos_profiles=[qos]))
        writer.write("/old_latched", serialize_message(String(data="latched")), 100_000_000_000)
        for i in range(31):
            writer.write("/old", serialize_message(String(data=str(i))),
                         100_000_000_000 + i * 100_000_000)
        writer.close()
        return path

    def spin_until(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            rclpy.spin_once(self.node, timeout_sec=.03)
        self.assertTrue(condition(), "timed out waiting for replay output")

    def test_reindex_mcap_and_sqlite_and_remapped_window(self):
        for storage in ("mcap", "sqlite3"):
            bag = self.bag(storage)
            original = {p.name: p.read_bytes() for p in bag.iterdir()}
            derived = self.root / (storage + "-repair")
            shutil.copytree(bag, derived)
            (derived / "metadata.yaml").unlink()
            subprocess.run(["ros2", "bag", "reindex", "--storage", storage, str(derived)],
                           check=True, timeout=30, stdout=subprocess.DEVNULL)
            meta = yaml.safe_load((derived / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
            self.assertEqual(meta["message_count"], 32)
            self.assertEqual(original, {p.name: p.read_bytes() for p in bag.iterdir()})
        bag = self.root / "mcap/bags/logger/exact"
        plan = dict(version=1, source=str(bag.parents[2]),
                    bag={"logger": "logger", "session": "exact",
                         "start_ns": 100_000_000_000, "duration_ns": 3_000_000_000},
                    topics=["/old"], remap={"/old": "/new"}, services=[])
        handoff = self.root / "plan.json"
        handoff.write_text(json.dumps(plan))
        _, _, _, args, _, _ = play_cmd.build_args({}, {"RIG_REPLAY_PLAN": str(handoff),
                                                       "RIG_REPLAY_FROM_S": "1", "RIG_REPLAY_TO_S": "2"})
        received, old = [], []
        self.node.create_subscription(String, "/new", lambda msg: received.append(msg.data), 10)
        self.node.create_subscription(String, "/old", lambda msg: old.append(msg.data), 10)
        p = subprocess.Popen(["ros2", "bag", "play", str(bag), "--delay", "1",
                              "--disable-keyboard-controls", *args],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(p)
        self.spin_until(lambda: p.poll() is not None)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(received)
        self.assertTrue(all(10 <= int(n) < 21 for n in received), received)
        self.assertFalse(old)
        # The real pre-pass reads the same original selector and publishes only at its remapped name.
        latched = []
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         reliability=ReliabilityPolicy.RELIABLE)
        self.node.create_subscription(String, "/new_latched", lambda msg: latched.append(msg.data), qos)
        helper = Path(play_cmd.__file__).with_name("latch_restore.py")
        p = subprocess.Popen([sys.executable, str(helper), str(bag), "--from", "1",
                              "--remap", "/old_latched:=/new_latched", "--topics", "/old_latched"])
        self.children.append(p)
        self.spin_until(lambda: bool(latched))
        self.assertEqual(latched[0], "latched")

    def test_clock_only_publishes_selected_timeline(self):
        clocks = []
        self.node.create_subscription(Clock, "/clock", lambda msg: clocks.append(
            msg.clock.sec * 10**9 + msg.clock.nanosec),
            QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
        helper = Path(play_cmd.__file__).with_name("clock_only.py")
        p = subprocess.Popen([sys.executable, str(helper), "--start-ns", "100000000000",
                              "--from", "2", "--to", "3", "--at", str(time.time() + 1)])
        self.children.append(p)
        self.spin_until(lambda: p.poll() is not None)
        self.assertEqual(p.returncode, 0)
        self.assertGreater(len(clocks), 10)
        self.assertGreaterEqual(min(clocks), 102_000_000_000)
        self.assertLessEqual(max(clocks), 103_000_000_000)


if __name__ == "__main__":
    unittest.main()
