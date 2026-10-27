"""Native isolated ROS proof: nothing escapes hold, armed clock, common committed release."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from replay_coordination import _atomic
try:
    import rclpy
    import rosbag2_py
    from rclpy.serialization import serialize_message
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rosgraph_msgs.msg import Clock
    from std_msgs.msg import String
    from std_srvs.srv import SetBool
except ImportError:
    rclpy = None


@unittest.skipIf(rclpy is None, "coordinated acceptance requires fleet-ros")
class CoordinatedRuntime(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.children = []
        self.logs = []
        self.addCleanup(self.cleanup)
        self.launch(["ros2", "run", "rmw_zenoh_cpp", "rmw_zenohd"])
        time.sleep(.4)
        rclpy.init()
        self.addCleanup(lambda: rclpy.shutdown() if rclpy.ok() else None)
        self.node = rclpy.create_node("coordinated_acceptance")
        self.addCleanup(self.node.destroy_node)
        self.received, self.calls, self.clocks = [], [], []
        self.node.create_subscription(String, "/coord/data", lambda msg: self.received.append(
            (time.time(), msg.data)), 10)
        self.node.create_subscription(Clock, "/clock", lambda msg: self.clocks.append(
            (time.time(), msg.clock.sec * 10**9 + msg.clock.nanosec)),
            QoSProfile(depth=100, reliability=ReliabilityPolicy.BEST_EFFORT))
        def serve(request, response):
            self.calls.append((time.time(), request.data))
            response.success = True
            return response
        self.node.create_service(SetBool, "/coord/set_bool", serve)
        self.control = self.root / "control"
        (self.control / "statuses").mkdir(parents=True)
        self.identity = dict(version=1, session_id="c"*32, plan_id="d"*64)
        self.env = dict(os.environ, RIG_REPLAY_CONTROL_DIR=str(self.control),
            RIG_REPLAY_SESSION_ID="c"*32, RIG_REPLAY_PLAN_ID="d"*64,
            RIG_REPLAY_PARTICIPANT="fixture-hyphen", RIG_SIM_TIME="1",
            CALL_INJECTOR_BAG_START_NS="100000000000", RIG_BAG_ROOT=str(self.root),
            CALL_INJECTOR_NAME="fixture")
        self.sequence = 0
        self.command("hold")

    def launch(self, args, env=None):
        log = open(self.root / f"child-{len(self.children)}.log", "w+")
        self.logs.append(log)
        child = subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.children.append(child)
        return child

    def cleanup(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
        for log in self.logs:
            log.close()

    def command(self, op, **extra):
        self.sequence += 1
        _atomic(self.control / "command.json", dict(self.identity, seq=self.sequence, op=op, **extra))

    def status(self, role):
        try:
            return json.loads((self.control / "statuses" / ("fixture-hyphen."+role+".json")).read_text())
        except FileNotFoundError:
            return {}

    def spin(self, until, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rclpy.spin_once(self.node, timeout_sec=.01)
            if until():
                return
        logs = ""
        for log in self.logs:
            log.flush(); log.seek(0); logs += log.read() + "\n"
        self.fail("native coordinated wait timed out\n" + logs)

    def pause(self, seconds):
        until = time.monotonic() + seconds
        self.spin(lambda: time.monotonic() >= until, timeout=seconds+1)

    def configure(self, roles, *, subscriptions=True):
        _atomic(self.control / "session.json", dict(self.identity,
            participants=["fixture-hyphen."+role for role in roles], startup_deadline_unix_s=time.time()+30))
        requirements = dict(self.identity, required_instances=["consumer"] if subscriptions else [],
            subscriptions=[dict(instance="consumer", topic="/coord/data", type="std_msgs/msg/String",
                                node="/coordinated_acceptance")] if subscriptions else [],
            services=[], transports=[], unsupported=[])
        _atomic(self.control / "requirements.json", requirements)

    def release(self, roles, clock=True):
        self.spin(lambda: all(self.status(role).get("state") == "held" for role in roles))
        self.pause(.3)
        self.assertFalse(self.received)
        self.assertFalse(self.calls)
        self.assertFalse(self.clocks)
        self.command("arm")
        self.spin(lambda: all(self.status(role).get("state") == "ready" for role in roles))
        self.pause(.3)
        self.assertFalse(self.received)
        self.assertFalse(self.calls)
        self.assertEqual(bool(self.clocks), clock)
        release = time.time() + .8
        self.command("prepare_release", release_at_unix_s=release)
        self.spin(lambda: all(self.status(role).get("state") == "release_ack" for role in roles))
        self.command("commit", release_at_unix_s=release)
        return release

    def bag(self, unknown=False):
        path = self.root / "bag"
        writer = rosbag2_py.SequentialWriter()
        writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
        from rosbag2_py._storage import QoS
        qos = QoS(10); qos.reliable()
        writer.create_topic(rosbag2_py.TopicMetadata(id=0, name="/coord/raw", type="std_msgs/msg/String",
            serialization_format="cdr", offered_qos_profiles=[qos]))
        for i in range(11):
            writer.write("/coord/raw", serialize_message(String(data=str(i))), 100_000_000_000+i*100_000_000)
        if unknown:
            writer.create_topic(rosbag2_py.TopicMetadata(id=1, name="/coord/unknown",
                type="rig_missing_package/msg/Uninstalled", serialization_format="cdr", offered_qos_profiles=[qos]))
            writer.write("/coord/unknown", serialize_message(String(data="unknown")), 100_000_000_000)
        writer.close()
        return path

    def test_real_player_and_zero_timestamp_script_stay_held(self):
        self.configure(["player", "readiness", "calls"])
        script = self.root / "calls.yaml"
        script.write_text('schema: 1\ncalls:\n- t: 0\n  service: /coord/set_bool\n  type: std_srvs/srv/SetBool\n  request: {data: true}\n')
        injector = self.launch([sys.executable, str(TOOLS / "call_injector.py"), str(script)], self.env)
        player = self.launch([sys.executable, str(TOOLS / "coordinated_player.py"), "--",
            "ros2", "bag", "play", str(self.bag()), "--start-paused", "--clock", "--disable-keyboard-controls",
            "--remap", "/coord/raw:=/coord/data", "--topics", "/coord/raw"], self.env)
        release = self.release(["player", "readiness", "calls"])
        self.spin(lambda: player.poll() is not None and injector.poll() is not None)
        self.assertEqual(player.returncode, 0)
        self.assertEqual(injector.returncode, 0)
        self.assertEqual([value for _, value in self.received], [str(i) for i in range(11)])
        self.assertEqual([value for _, value in self.calls], [True])
        self.assertTrue(all(at >= release for at, _ in self.received+self.calls))
        for role in ("player", "readiness", "calls"):
            self.assertEqual(self.status(role)["state"], "completed")
            self.assertGreaterEqual(self.status(role)["actual_release_at_unix_s"], release)

    def test_wall_script_rate_two_window_and_timestamp_zero(self):
        self.configure(["player", "readiness", "calls"], subscriptions=False)
        env = dict(self.env, RIG_SIM_TIME="", CALL_INJECTOR_RATE="2",
                   RIG_REPLAY_FROM_S="0", RIG_REPLAY_TO_S="1")
        script = self.root / "wall-calls.yaml"
        script.write_text("schema: 1\ncalls:\n" + "".join(
            f"- t: {at}\n  service: /coord/set_bool\n  type: std_srvs/srv/SetBool\n  request: {{data: {value}}}\n"
            for at, value in ((0, "true"), (.6, "false"), (1, "true"))))
        injector = self.launch([sys.executable, str(TOOLS / "call_injector.py"), str(script)], env)
        player = self.launch([sys.executable, str(TOOLS / "clock_only.py"), "--start-ns", "100000000000",
            "--from", "0", "--to", "1", "--rate", "2", "--wall-clock"], env)
        release = self.release(["player", "readiness", "calls"], clock=False)
        self.spin(lambda: injector.poll() is not None and player.poll() is not None)
        self.assertEqual(injector.returncode, 0)
        self.assertEqual(player.returncode, 0)
        self.assertEqual([value for _, value in self.calls], [True, False])
        self.assertTrue(all(at >= release for at, _ in self.calls))
        self.assertLess(self.calls[0][0] - release, .15)
        self.assertAlmostEqual(self.calls[1][0] - release, .3, delta=.15)

    def test_window_without_latched_data_is_a_successful_prepass(self):
        self.configure(["player", "readiness"])
        bag = self.bag()
        latch = [str(bag), "--from", "0.2", "--topics", "/coord/raw"]
        player = self.launch([sys.executable, str(TOOLS / "coordinated_player.py"),
            "--latch", json.dumps(latch), "--", "ros2", "bag", "play", str(bag),
            "--start-paused", "--clock", "--start-offset", "0.2", "--disable-keyboard-controls",
            "--remap", "/coord/raw:=/coord/data", "--topics", "/coord/raw"], self.env)
        release = self.release(["player", "readiness"])
        self.spin(lambda: player.poll() is not None)
        self.assertEqual(player.returncode, 0)
        self.assertEqual([value for _, value in self.received], [str(i) for i in range(2, 11)])
        self.assertTrue(all(at >= release for at, _ in self.received))

    def test_missing_selected_type_fails_before_held(self):
        self.configure(["player", "readiness"])
        player = self.launch([sys.executable, str(TOOLS / "coordinated_player.py"), "--", "ros2", "bag", "play",
            str(self.bag(unknown=True)), "--start-paused", "--clock", "--topics", "/coord/raw", "/coord/unknown"], self.env)
        self.spin(lambda: player.poll() is not None)
        self.assertNotEqual(player.returncode, 0)
        status = self.status("player")
        self.assertEqual(status["state"], "failed")
        self.assertIn("/coord/unknown", status["error"])
        self.assertNotIn("held_at_unix_s", status)
        self.assertFalse(self.clocks)
        self.assertFalse(self.received)

    def test_excluded_unknown_type_does_not_block_selected_known_topic(self):
        self.configure(["player", "readiness"])
        player = self.launch([sys.executable, str(TOOLS / "coordinated_player.py"), "--", "ros2", "bag", "play",
            str(self.bag(unknown=True)), "--start-paused", "--clock", "--disable-keyboard-controls",
            "--remap", "/coord/raw:=/coord/data", "--topics", "/coord/raw"], self.env)
        release = self.release(["player", "readiness"])
        self.spin(lambda: player.poll() is not None)
        self.assertEqual(player.returncode, 0)
        self.assertEqual([value for _, value in self.received], [str(i) for i in range(11)])
        self.assertTrue(all(at >= release for at, _ in self.received))

    def test_clock_only_holds_and_advances_only_after_commit(self):
        self.configure(["player", "readiness"], subscriptions=False)
        player = self.launch([sys.executable, str(TOOLS / "clock_only.py"), "--start-ns", "100000000000",
            "--from", "2", "--to", "3", "--rate", "2"], self.env)
        release = self.release(["player", "readiness"])
        self.assertTrue(all(value == 102_000_000_000 for _, value in self.clocks))
        self.spin(lambda: player.poll() is not None)
        self.assertEqual(player.returncode, 0)
        self.assertTrue(any(value > 102_000_000_000 for _, value in self.clocks))
        self.assertTrue(all(at >= release for at, value in self.clocks if value > 102_000_000_000))


if __name__ == "__main__":
    unittest.main()
