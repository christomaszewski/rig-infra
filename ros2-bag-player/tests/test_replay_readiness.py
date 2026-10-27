"""Endpoint identity, timing, and coordination regressions without ROS or Docker."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import replay_readiness as probe


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Readiness(unittest.TestCase):
    def setUp(self):
        self.request = dict(version=1, subscriptions=[dict(instance="nav", topic="/input",
            type="example/msg/Value", node="/nav/worker")], services=[], transports=[],
            required_instances=["nav"], unsupported=[])
        self.good = dict(nodes={"/nav/worker": 1}, subscriptions=[dict(topic="/input",
                         type="example/msg/Value", node="/nav/worker")])
        self.clock = Clock()

    def wait(self, observe, **kwargs):
        return probe.wait_ready(self.request, observe, timeout_s=1, stable_s=.2,
                                clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_schema_rejects_guessed_nodes_types_and_empty_consumer_proofs(self):
        for change in ({"version": True}, {"required_instances": ["missing"]},
                       {"required_instances": ["nav", "nav"]}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                probe.validate_requirements({**self.request, **change})
        for field, value in (("node", "/nav/*"), ("node", "nav"), ("type", "?"),
                             ("type", "example/srv/Value"), ("topic", "/bad//topic")):
            request = copy.deepcopy(self.request)
            request["subscriptions"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                probe.validate_requirements(request)

    def test_reader_rejects_oversize_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            path.write_text(json.dumps(self.request))
            self.assertEqual(probe.load_requirements(path), self.request)
            path.write_bytes(b" " * (probe.MAX_REQUEST_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "256 KiB"):
                probe.load_requirements(path)

    def test_session_binding_rejects_stale_foreign_and_missing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            binding = dict(session_id="a" * 32, plan_id="b" * 64)
            path.write_text(json.dumps(dict(self.request, **binding)))
            self.assertEqual(probe.load_requirements(path, **binding), self.request)
            with self.assertRaises(ValueError):
                probe.load_requirements(path)  # Standalone cannot ignore coordinated identity.
            with self.assertRaisesRegex(ValueError, "another replay"):
                probe.load_requirements(path, session_id="c" * 32, plan_id="b" * 64)
            path.write_text(json.dumps(self.request))
            with self.assertRaisesRegex(ValueError, "another replay"):
                probe.load_requirements(path, **binding)

    def test_wrong_type_node_and_harness_never_count_as_ready(self):
        cases = [dict(self.good, nodes={"/nav/worker": 0}), dict(self.good, nodes={"/nav/worker": 2}),
                 dict(self.good, excluded_nodes=["/nav/worker"])]
        for field, value in (("node", "/player"), ("type", "other/msg/Value"), ("topic", "/wrong")):
            value_dict = copy.deepcopy(self.good)
            value_dict["subscriptions"][0][field] = value
            cases.append(value_dict)
        for observed in cases:
            with self.subTest(observed=observed):
                self.assertFalse(probe.evaluate(self.request, observed)["ready"])

    def test_service_requires_exact_server_node_type_and_client_readiness(self):
        self.request["subscriptions"] = []
        entry = dict(instance="nav", service="/reset", type="example/srv/Reset", node="/nav/worker")
        self.request["services"] = [entry]
        for available in (False, True):
            observation = dict(nodes={"/nav/worker": 1}, services=[dict(entry, available=available)])
            self.assertEqual(probe.evaluate(self.request, observation)["ready"], available)
        observation["services"][0]["node"] = "/different/server"
        self.assertFalse(probe.evaluate(self.request, observation)["ready"])

    def test_slow_consumer_is_ready_only_after_stable_observations(self):
        result = self.wait(lambda remaining: self.good if self.clock.now >= .3 else {})
        self.assertEqual(result["state"], "ready")
        self.assertGreaterEqual(result["elapsed_s"], .5)

    def test_lost_endpoint_resets_stable_interval(self):
        result = self.wait(lambda remaining: {} if .1 <= self.clock.now < .3 else self.good)
        self.assertEqual(result["state"], "ready")
        self.assertGreaterEqual(result["elapsed_s"], .5)

    def test_timeout_overrun_and_cancel_do_not_release(self):
        result = self.wait(lambda remaining: {})
        self.assertEqual(result["state"], "timeout")
        self.assertAlmostEqual(result["elapsed_s"], 1)
        self.clock.now = 0
        def slow(remaining):
            self.assertLessEqual(remaining, 1)
            self.clock.now += remaining
            return self.good
        self.assertEqual(self.wait(slow)["state"], "timeout")
        self.assertEqual(self.wait(lambda remaining: self.good, cancelled=lambda: True)["state"], "aborted")

    def test_unsupported_consumer_fails_without_observation(self):
        self.request["unsupported"] = [dict(instance="nav", reason="no recorded nodes")]
        result = self.wait(lambda remaining: self.fail("unsupported consumer cannot be observed as ready"))
        self.assertEqual(result["state"], "unsupported")
        self.assertIn("no recorded nodes", result["missing"][0]["reason"])

    def test_observer_failure_is_explicit_error(self):
        def broken(remaining):
            raise RuntimeError("ROS context disappeared")
        self.assertEqual(self.wait(broken)["state"], "error")

    def test_transport_checks_are_independent_and_forbid_unbounded_dns(self):
        self.request["transports"] = [dict(instance="router", host="127.0.0.1", port=7447)]
        self.assertFalse(probe.evaluate(self.request, self.good)["ready"])
        self.good["transports"] = [dict(host="127.0.0.1", port=7447, available=True)]
        self.assertTrue(probe.evaluate(self.request, self.good)["ready"])
        for host in ("router.local", "0.0.0.0", "::", "239.1.1.1"):
            self.request["transports"][0]["host"] = host
            with self.subTest(host=host), self.assertRaises(ValueError):
                probe.validate_requirements(self.request)

    def test_coordinated_probe_prepares_before_subscribers_then_rechecks_readiness(self):
        owner = self
        class Participant:
            def wait_release(self, *, prepared_check, ready_check, on_tick):
                self.started_flag = self.completed_flag = False
                owner.good["subscriptions"] = []
                self.assert_prepared = prepared_check()
                owner.assertTrue(self.assert_prepared)
                owner.assertFalse(ready_check())
                owner.good["subscriptions"] = [dict(owner.request["subscriptions"][0])]
                owner.assertFalse(ready_check())
                owner.clock.sleep(.25)
                owner.assertTrue(ready_check())
                owner.good["subscriptions"] = []
                owner.assertFalse(ready_check())
                owner.good["subscriptions"] = [dict(owner.request["subscriptions"][0])]
                owner.assertFalse(ready_check())
                owner.clock.sleep(.25)
                owner.assertTrue(ready_check())
                on_tick()
                owner.assertFalse(self.started_flag)
            def started(self):
                self.started_flag = True
            def completed(self):
                self.completed_flag = True
        participant = Participant()
        result = probe.coordinated(self.request, participant, lambda remaining: self.good,
                                    timeout_s=1, stable_s=.2, clock=self.clock)
        self.assertEqual(result["state"], "ready")
        self.assertTrue(participant.started_flag and participant.completed_flag)

    def test_ros_adapter_discovers_exact_endpoints_without_sending_service_calls(self):
        self.request["services"] = [dict(instance="nav", service="/reset",
                                          type="example/srv/Reset", node="/nav/worker")]
        self.request["transports"] = [dict(instance="router", host="127.0.0.1", port=7447)]
        node, client = Mock(), Mock()
        node.get_name.return_value, node.get_namespace.return_value = "probe", "/"
        node.get_node_names_and_namespaces.return_value = [("worker", "/nav"), ("probe", "/")]
        node.get_subscriptions_info_by_topic.return_value = [SimpleNamespace(
            node_name="worker", node_namespace="/nav", topic_type="example/msg/Value")]
        node.get_service_names_and_types_by_node.return_value = [("/reset", ["example/srv/Reset"])]
        node.create_client.return_value = client
        client.service_is_ready.return_value = True
        rclpy = Mock()
        rclpy.create_node.return_value = node
        utilities = Mock()
        with patch.dict(sys.modules, {"rclpy": rclpy, "rosidl_runtime_py": Mock(),
                                     "rosidl_runtime_py.utilities": utilities}), patch.object(
                probe.socket, "create_connection") as connect:
            connect.return_value.__enter__ = Mock()
            connect.return_value.__exit__ = Mock()
            observer = probe.RosObserver(self.request)
            observation = observer(.1)
            self.assertTrue(probe.evaluate(self.request, observation)["ready"])
            self.assertIn("/probe", observation["excluded_nodes"])
            node.get_service_names_and_types_by_node.assert_called_once_with("worker", "/nav")
            client.call.assert_not_called()
            client.call_async.assert_not_called()
            node.create_subscription.assert_not_called()
            node.create_publisher.assert_not_called()
            self.assertLessEqual(connect.call_args.kwargs["timeout"], .1)
            observer.close()
            node.destroy_node.assert_called_once()
            rclpy.try_shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
