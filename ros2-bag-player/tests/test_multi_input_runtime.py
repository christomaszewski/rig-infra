"""Native mixed-storage replay: duplicates, global windows, calls and one clock."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bag_inputs
import coordinated_player
import export_calls
import inspect_calls
import latch_restore
import play_cmd
try:
    import rclpy
    import rosbag2_py
    from rclpy.serialization import serialize_message, deserialize_message
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rosgraph_msgs.msg import Clock
    from std_msgs.msg import String
    from std_srvs.srv import SetBool
except ImportError:
    rclpy = None


@unittest.skipIf(rclpy is None, "multi-input acceptance requires fleet-ros")
class MultiInputRuntime(unittest.TestCase):
    def test_received_merge_preserves_duplicates_and_one_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bags = []
            payload = serialize_message(String(data="duplicate"))
            event = SetBool.Event()
            event.info.event_type = 1
            event.info.client_gid = [1] * 16
            event.info.sequence_number = 1
            event.request = [SetBool.Request(data=True)]
            for logger, storage, values in (("a", "mcap", [(1, "a"), (2, "duplicate")]),
                                            ("b", "sqlite3", [(2, "duplicate"), (3, "b")])):
                path = root / "bags" / logger / "exact"
                path.parent.mkdir(parents=True)
                writer = rosbag2_py.SequentialWriter()
                writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=storage),
                            rosbag2_py.ConverterOptions("cdr", "cdr"))
                from rosbag2_py._storage import QoS
                qos = QoS(10)
                qos.reliable()
                for number, (topic, typ) in enumerate((("/multi/value", "std_msgs/msg/String"),
                        ("/multi/latched", "std_msgs/msg/String"), ("/multi/marker", "std_msgs/msg/String"),
                        ("/multi/control/_service_event", "std_srvs/srv/SetBool_Event"))):
                    writer.create_topic(rosbag2_py.TopicMetadata(id=number, name=topic, type=typ,
                        serialization_format="cdr", offered_qos_profiles=[qos]))
                base = 100_000_000_000
                writer.write("/multi/latched", serialize_message(String(data=logger)), base + (0 if logger == "a" else 10**9))
                for offset, value in values:
                    writer.write("/multi/value", payload if value == "duplicate" else serialize_message(String(data=value)),
                                 base + offset * 10**9)
                writer.write("/multi/control/_service_event", serialize_message(event), base + 2 * 10**9)
                writer.write("/multi/marker", serialize_message(String(data="end")), base + 4 * 10**9)
                writer.close()
                bags.append(dict(logger=logger, session="exact", relative_path=f"bags/{logger}/exact", storage=storage))

            inputs = bag_inputs.resolved_inputs(str(root), bags)
            token = bag_inputs.selection_token(inputs, base)
            last = latch_restore.read_last(token, ["/multi/latched"], base + 2 * 10**9)
            self.assertEqual(deserialize_message(last["/multi/latched"][1], String).data, "b")
            request = dict(version=2, source=str(root), bags=bags, start_ns=base,
                           from_s=.5, to_s=3.5, services=["/multi/control"], source_mode="service")
            inspected = inspect_calls.inspect(request)["services"]["/multi/control"]
            self.assertEqual((inspected["request_count"], inspected["duplicate_count"]), (2, 1))
            calls, missing = export_calls.read_events(token, "service")
            self.assertEqual([call[0] for call in calls], [2.0, 2.0])
            self.assertEqual(missing, {})

            plan = dict(version=2, source=str(root), bags=bags, timeline=dict(start_ns=base, duration_ns=4 * 10**9),
                        topics=["/multi/value"], services=["/multi/control"], remap={})
            handoff = root / "plan.json"
            handoff.write_text(json.dumps(plan))
            _, _, _, args, _, _ = play_cmd.build_args({"play": {"rate": 2}}, {
                "RIG_REPLAY_PLAN": str(handoff), "RIG_REPLAY_FROM_S": ".5", "RIG_REPLAY_TO_S": "3.5", "RIG_SIM_TIME": "1"})
            command = ["ros2", "bag", "play", *[arg for item in inputs for arg in
                       ("--input", item["path"], item["storage"])], "--delay", "1", "--disable-keyboard-controls", *args]
            coordinated_player.validate_selected_types(command)
            router = subprocess.Popen(["ros2", "run", "rmw_zenoh_cpp", "rmw_zenohd"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            player = node = None
            try:
                time.sleep(.5)
                rclpy.init()
                node = rclpy.create_node("multi_input_acceptance")
                received, clocks, calls_received, publishers = [], [], [], []
                node.create_subscription(String, "/multi/value", lambda msg: received.append(msg.data), 10)
                node.create_subscription(Clock, "/clock", lambda msg: clocks.append(msg.clock.sec * 10**9 + msg.clock.nanosec),
                                         QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT))
                def serve(request, response):
                    calls_received.append(request.data)
                    response.success = True
                    return response
                node.create_service(SetBool, "/multi/control", serve)
                with (root / "player.log").open("w") as log:
                    player = subprocess.Popen(command, stdout=log, stderr=log)
                    deadline = time.monotonic() + 15
                    while player.poll() is None and time.monotonic() < deadline:
                        rclpy.spin_once(node, timeout_sec=.02)
                        publishers.append(node.count_publishers("/clock"))
                    self.assertEqual(player.poll(), 0, (root / "player.log").read_text())
                    until = time.monotonic() + .3
                    while time.monotonic() < until:
                        rclpy.spin_once(node, timeout_sec=.02)
                self.assertEqual(received, ["a", "duplicate", "duplicate", "b"])
                self.assertEqual(calls_received, [True, True])
                self.assertTrue(clocks)
                self.assertEqual(clocks, sorted(clocks))
                self.assertEqual(max(publishers), 1)
            finally:
                for child in (player, router):
                    if child is not None and child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                if node is not None:
                    node.destroy_node()
                if rclpy.ok():
                    rclpy.shutdown()


if __name__ == "__main__":
    unittest.main()
