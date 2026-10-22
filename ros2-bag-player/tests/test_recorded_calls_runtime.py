"""Real recorded-call acceptance in an existing isolated fleet-ros image; host skips ROS.

Only synthetic temporary bags and a private no-network router/server are used. No script
injector participates: calls must be reconstructed by rosbag2 from service-event contents.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import inspect_calls
import play_cmd
try:
    import rclpy
    import rosbag2_py
    from rclpy.serialization import serialize_message
    from service_msgs.msg import ServiceEventInfo
    from std_msgs.msg import String
    from std_srvs.srv import SetBool
except ImportError:
    rclpy = None


@unittest.skipIf(rclpy is None, "recorded-call acceptance requires fleet-ros")
class RecordedCalls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.children = []
        self.addCleanup(self.stop_children)
        self.children.append(subprocess.Popen(["ros2", "run", "rmw_zenoh_cpp", "rmw_zenohd"],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(.4)
        rclpy.init()
        self.addCleanup(lambda: rclpy.shutdown() if rclpy.ok() else None)
        self.node = rclpy.create_node("recorded_calls_acceptance")
        self.addCleanup(self.node.destroy_node)
        self.calls, self.unselected = [], []

        def serve(request, response):
            self.calls.append(request.data)
            response.success = True
            return response

        def other(request, response):
            self.unselected.append(request.data)
            response.success = True
            return response

        self.node.create_service(SetBool, "/test/set_bool", serve)
        self.node.create_service(SetBool, "/test/other", other)

    def stop_children(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

    def event(self, origin, data=True, sequence=1):
        event = SetBool.Event()
        event.info.event_type = origin
        event.info.stamp.sec = 999  # Deliberately different from the bag's received timestamp.
        event.info.client_gid = [1] * 16
        event.info.sequence_number = sequence
        if data is not None:
            event.request = [SetBool.Request(data=data)]
        return event

    def bag(self, events, storage="mcap", end=3):
        path = self.root / storage / "bags/logger/exact"
        path.parent.mkdir(parents=True)
        writer = rosbag2_py.SequentialWriter()
        writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap" if storage == "corrupt" else storage),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
        from rosbag2_py._storage import QoS
        qos = QoS(10)
        qos.reliable()
        types = {topic: ("std_msgs/msg/String" if isinstance(event, String) else
                         "std_srvs/srv/SetBool_Event") for topic, _, event in events}
        types["/test/marker"] = "std_msgs/msg/String"
        for number, (topic, typ) in enumerate(types.items()):
            writer.create_topic(rosbag2_py.TopicMetadata(id=number, name=topic, type=typ,
                serialization_format="cdr", offered_qos_profiles=[qos]))
        writer.write("/test/marker", serialize_message(String(data="start")), 100_000_000_000)
        for topic, offset, event in sorted(events, key=lambda row: row[1]):
            payload = event if isinstance(event, bytes) else serialize_message(event)
            writer.write(topic, payload, 100_000_000_000 + int(offset * 1e9))
        writer.write("/test/marker", serialize_message(String(data="end")), 100_000_000_000 + int(end * 1e9))
        writer.close()
        return path

    def request(self, bag, services, origin="service", start=.5, stop=1.5):
        return {"version": 1, "session": str(bag), "start_ns": 100_000_000_000,
                "from_s": start, "to_s": stop, "services": services, "source_mode": origin}

    def playback(self, bag, services, origin, rate, sim_time, start=.5, stop=1.5):
        plan = {"version": 1, "source": str(bag.parents[2]),
                "bag": {"logger": "logger", "session": "exact", "start_ns": 100_000_000_000},
                "topics": ["/test/marker"],
                "remap": {}, "services": services, "services_source": origin}
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan))
        _, _, _, args, _, _ = play_cmd.build_args({"play": {"rate": rate}}, {
            "RIG_REPLAY_PLAN": str(path), "RIG_REPLAY_FROM_S": str(start),
            "RIG_REPLAY_TO_S": str(stop), "RIG_SIM_TIME": str(int(sim_time))})
        self.assertEqual("--clock" in args, sim_time)
        before, other_before = len(self.calls), len(self.unselected)
        log_path = self.root / "play.log"
        with log_path.open("w") as log:
            player = subprocess.Popen(["ros2", "bag", "play", str(bag), "--delay", "1",
                                       "--disable-keyboard-controls", *args], stdout=log, stderr=log)
            self.children.append(player)
            deadline = time.monotonic() + 14
            while player.poll() is None and time.monotonic() < deadline:
                rclpy.spin_once(self.node, timeout_sec=.02)
            self.assertIsNotNone(player.poll(), "recorded-call player timed out")
            drain = time.monotonic() + .25
            while time.monotonic() < drain:
                rclpy.spin_once(self.node, timeout_sec=.02)
        self.assertEqual(player.returncode, 0, log_path.read_text())
        self.assertEqual(self.unselected[other_before:], [])
        return self.calls[before:]

    def test_normal_mixed_origins_select_exactly_one_request_with_window_rate_and_clock(self):
        topic = "/test/set_bool/_service_event"
        bag = self.bag([(topic, .25, self.event(1, False, 2)),
                        (topic, 1, self.event(1)), (topic, 1.1, self.event(0)),
                        ("/test/other/_service_event", 1.2, self.event(1, False)),
                        (topic, 1.5, self.event(1, False, 3))])
        for origin, rate, sim_time in (("service", 2, True), ("client", .5, False)):
            with self.subTest(origin=origin, rate=rate, sim_time=sim_time):
                report = inspect_calls.inspect(self.request(bag, ["/test/set_bool"], origin))
                result = report["services"]["/test/set_bool"]
                self.assertEqual((result["request_count"], result["other_origin_request_count"],
                                  result["duplicate_count"]), (1, 1, 0))
                self.assertEqual(self.playback(bag, ["/test/set_bool"], origin, rate, sim_time), [True])
        self.assertEqual(self.playback(bag, [], "service", 2, False), [])

    def test_repeated_selected_origin_is_reported_and_replayed_twice(self):
        topic = "/test/set_bool/_service_event"
        bag = self.bag([(topic, 1, self.event(1)), (topic, 1.1, self.event(1)),
                        (topic, 1.2, self.event(0))])
        result = inspect_calls.inspect(self.request(bag, ["/test/set_bool"]))["services"]["/test/set_bool"]
        self.assertEqual((result["request_count"], result["duplicate_count"]), (2, 1))
        self.assertEqual(self.playback(bag, ["/test/set_bool"], "service", 2, False), [True, True])

    def test_long_fractional_window_matches_inspection_at_both_boundaries(self):
        topic = "/test/set_bool/_service_event"
        start, stop = 3600.000001, 3601.000001
        for storage in ("mcap", "sqlite3"):
            bag = self.bag([(topic, start - .0000005, self.event(1, False, 1)),
                            (topic, start, self.event(1, True, 2)),
                            (topic, stop, self.event(1, False, 3))], storage, end=4000)
            result = inspect_calls.inspect(self.request(bag, ["/test/set_bool"], start=start, stop=stop))
            self.assertEqual(result["services"]["/test/set_bool"]["request_count"], 1)
            self.assertEqual(self.playback(bag, ["/test/set_bool"], "service", 2, False,
                                           start=start, stop=stop), [True])

    def test_window_seek_does_not_read_irrelevant_history_or_tail(self):
        topic = "/test/set_bool/_service_event"
        for storage in ("mcap", "sqlite3"):
            events = [(topic, i / 1000, self.event(1, False, i)) for i in range(1, 400)]
            events += [(topic, 1, self.event(1, True, 500))]
            events += [(topic, 2 + i / 1000, self.event(1, False, 600 + i)) for i in range(400)]
            bag = self.bag(events, storage)
            real_reader = rosbag2_py.SequentialReader
            reads, seeks = [], []

            class CountingReader:
                def __init__(self):
                    self.reader = real_reader()

                def __getattr__(self, name):
                    return getattr(self.reader, name)

                def read_next_ext(self):
                    value = self.reader.read_next_ext()
                    reads.append(value[2])
                    return value

                def seek(self, stamp):
                    seeks.append(stamp)
                    return self.reader.seek(stamp)

            with patch.object(rosbag2_py, "SequentialReader", CountingReader):
                report = inspect_calls.inspect(self.request(bag, ["/test/set_bool"]))
            self.assertEqual(report["services"]["/test/set_bool"]["request_count"], 1)
            self.assertEqual(seeks, [100_500_000_000])
            self.assertLessEqual(len(reads), 2, reads)

    def test_real_reader_classifications_receive_time_bounds_and_technical_failures(self):
        names = ["/metadata", "/responses", "/other", "/empty", "/unsupported", "/missing"]
        events = [("/metadata/_service_event", 1, self.event(1, None)),
                  ("/responses/_service_event", 1, self.event(2, None)),
                  ("/other/_service_event", 1, self.event(0)),
                  ("/empty/_service_event", 1.5, self.event(1)),
                  ("/unsupported/_service_event", 1, String(data="not a service event"))]
        for storage in ("mcap", "sqlite3"):
            bag = self.bag(events, storage)
            snapshot = {path.name: path.read_bytes() for path in bag.iterdir()}
            request = self.request(bag, names)
            report = inspect_calls.inspect(request)
            self.assertEqual([report["services"][name]["status"] for name in names],
                             ["metadata_only", "response_only", "other_origin", "empty_window",
                              "unsupported_type", "missing"])
            self.assertTrue(all(row["request_count"] == 0 for row in report["services"].values()))
            self.assertEqual(snapshot, {path.name: path.read_bytes() for path in bag.iterdir()})
            with self.assertRaisesRegex(ValueError, "starting_time changed"):
                inspect_calls.inspect({**request, "start_ns": 1})
            with self.assertRaises(TimeoutError):
                inspect_calls.inspect(request, budget_s=0)
        corrupt = self.bag([("/bad/_service_event", 1, b"invalid cdr")], "corrupt")
        with self.assertRaises(Exception):
            inspect_calls.inspect(self.request(corrupt, ["/bad"]))


if __name__ == "__main__":
    unittest.main()
