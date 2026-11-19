"""Endpoint discovery provenance, bounded work, and legacy topology compatibility."""
import enum
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import endpoint_snapshot as ep
import graph_snapshot as graph


class Policy(enum.IntEnum):
    SYSTEM_DEFAULT = 0
    RELIABLE = 1


class TypeHash:
    def __init__(self, version=1):
        self.version = version

    def __str__(self):
        return "RIHS01_" + "a" * 64


def info(gid=1, node="producer", hash_version=1):
    return NS(node_name=node, node_namespace="/vehicle", topic_type="std_msgs/msg/String",
              endpoint_gid=[gid] * 24, topic_type_hash=TypeHash(hash_version),
              qos_profile=NS(reliability=Policy.RELIABLE, durability=Policy.SYSTEM_DEFAULT,
                             history=Policy.SYSTEM_DEFAULT, depth=10, deadline=NS(nanoseconds=123),
                             lifespan=NS(nanoseconds=456), liveliness=Policy.SYSTEM_DEFAULT,
                             liveliness_lease_duration=NS(nanoseconds=789),
                             avoid_ros_namespace_conventions=False))


class Node:
    def __init__(self, publishers=None, subscribers=None):
        self.publishers = [info()] if publishers is None else publishers
        self.subscribers = [] if subscribers is None else subscribers
        self.calls = []

    def get_publishers_info_by_topic(self, topic):
        self.calls.append(("pub", topic))
        return self.publishers

    def get_subscriptions_info_by_topic(self, topic):
        self.calls.append(("sub", topic))
        return self.subscribers


class EndpointTests(unittest.TestCase):
    def observation(self, node=None, topics=("/sample",), **kwargs):
        return ep.observe(node or Node(), topics, clock=lambda: 10.0,
                          wall=lambda: "2026-11-13T12:00:00Z", **kwargs)

    def test_duplicate_node_names_keep_distinct_publishers_and_qos(self):
        result = self.observation(Node([info(1), info(2)], [info(3, "recorder")]))
        topic = result["evidence"]["topics"][0]
        pubs = topic["publishers"]["endpoints"]
        self.assertEqual(len(pubs), 2)
        self.assertEqual({row["node"] for row in pubs}, {"/vehicle/producer"})
        self.assertEqual({row["gid"]["hex"] for row in pubs}, {"01" * 24, "02" * 24})
        self.assertEqual(pubs[0]["qos"]["reliability"], {"status": "ok", "name": "RELIABLE", "value": 1})
        self.assertEqual(pubs[0]["qos"]["deadline_ns"]["value"], 123)
        self.assertEqual(topic["subscriptions"]["endpoints"][0]["kind"], "subscription")
        self.assertEqual(pubs[0]["type_hash"]["value"], "RIHS01_" + "a" * 64)
        self.assertEqual(result["evidence"]["coverage"]["status"], "queries_completed")
        self.assertFalse(result["evidence"]["coverage"]["atomic"])

    def test_unknown_ids_and_hashes_are_explicit_not_invented(self):
        value = ep.endpoint(info(0, hash_version=0), "publisher")
        self.assertEqual(value["gid"]["status"], "unknown")
        self.assertEqual(value["gid"]["length"], 24)
        self.assertEqual(value["type_hash"]["status"], "unknown")
        value = ep.endpoint(NS(node_name="old", node_namespace="/", topic_type="T"), "publisher")
        self.assertEqual(value["node"], "/old")
        self.assertEqual(value["gid"]["status"], "unknown")
        self.assertEqual(value["qos"]["history"]["status"], "unknown")

    def test_discovery_errors_keep_successful_other_queries(self):
        node = Node(subscribers=[info(3, "recorder")])

        def fail(topic):
            raise RuntimeError("discovery changed")

        node.get_publishers_info_by_topic = fail
        result = self.observation(node)["evidence"]
        self.assertEqual(result["coverage"]["status"], "partial")
        topic = result["topics"][0]
        self.assertEqual(topic["publishers"]["status"], "error")
        self.assertIn("discovery changed", topic["publishers"]["error"])
        self.assertEqual(topic["subscriptions"]["status"], "ok")
        self.assertEqual(len(topic["subscriptions"]["endpoints"]), 1)
        unknown = self.observation(NS())["evidence"]
        self.assertEqual(unknown["topics"][0]["publishers"]["status"], "unsupported")
        self.assertFalse(any(unknown["capabilities"].values()))

    def test_topic_endpoint_and_time_caps_report_partial_coverage(self):
        node = Node([info(1), info(2), info(3)])
        value = self.observation(node, topics=("/c", "/a", "/b"), max_topics=2, max_endpoints=2)["evidence"]
        self.assertEqual(value["coverage"]["topics_omitted"], 1)
        self.assertEqual(value["coverage"]["endpoints_recorded"], 2)
        self.assertEqual(value["coverage"]["status"], "partial")
        self.assertEqual(value["topics"][0]["publishers"]["status"], "partial")
        self.assertEqual(len(node.calls), 1)
        node = Node()
        ticks = iter((0, 1, 2, 3))
        value = ep.observe(node, ["/a"], clock=lambda: next(ticks), budget_s=0.5)["evidence"]
        self.assertEqual(node.calls, [])
        self.assertEqual(value["topics"][0]["publishers"]["status"], "skipped")

    def test_only_observer_itself_is_excluded(self):
        node = Node([info(1, "observer"), info(2, "other")])
        value = self.observation(node, self_fqn="/vehicle/observer")
        rows = value["evidence"]["topics"][0]["publishers"]["endpoints"]
        self.assertEqual([row["node"] for row in rows], ["/vehicle/other"])

    def test_endpoint_epochs_dedup_order_and_preserve_changed_gid_incarnation(self):
        tracker = ep.EndpointTracker("rmw_zenoh_cpp", 3, "collector-one")
        first = self.observation(Node([info(1), info(2)]), topics=("/b", "/a"))
        action, file, doc = tracker.tick(first, "topology", "epoch.yaml", lambda _: False)
        self.assertEqual(action, "open")
        second = self.observation(Node([info(2), info(1)]), topics=("/a", "/b"))
        second["ended_at"] = "2026-11-13T12:00:01Z"
        action, same_file, updated = tracker.tick(second, "topology", "epoch.yaml", lambda _: True)
        self.assertEqual((action, same_file), ("bump", file))
        self.assertEqual(updated["sha256"], doc["sha256"])
        self.assertEqual(updated["first_observation"], doc["first_observation"])
        self.assertEqual(updated["observations"], 2)
        action, _, changed = tracker.tick(self.observation(Node([info(4)])), "topology", "epoch.yaml", lambda _: False)
        self.assertEqual(action, "open")
        self.assertNotEqual(changed["sha256"], doc["sha256"])
        restarted = ep.EndpointTracker("rmw_zenoh_cpp", 3, "collector-two")
        action, other_file, other = restarted.tick(first, "topology", "epoch.yaml", lambda _: False)
        self.assertEqual(action, "open")
        self.assertNotEqual(other_file, file)
        self.assertEqual(other["sha256"], doc["sha256"])

    def test_atomic_sidecars_do_not_change_topology_or_legacy_loading(self):
        # Optional cross-repository check in the shared Rig workspace.
        rig = Path(__file__).resolve().parents[4] / "rig"
        if not (rig / "rig_cli/graph.py").is_file():
            self.skipTest("Rig reader checkout is not adjacent to infrastructure")
        sys.path.insert(0, str(rig))
        from rig_cli.graph import load_epochs
        nodes = graph.canonicalize({"/vehicle/producer": {"pubs": [("/sample", ["std_msgs/msg/String"])]}})
        identity = graph.identity_hash(nodes)
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "graph/observer"
            directory.mkdir(parents=True)
            topology = directory / "epoch_20261113T120000Z.yaml"
            topology.write_text(graph.render_epoch(nodes, "2026-11-13T12:00:00Z", "2026-11-13T12:00:00Z", "rmw_zenoh_cpp", 3))
            before = topology.read_bytes()
            tracker = ep.EndpointTracker("rmw_zenoh_cpp", 3)
            _, filename, document = tracker.tick(self.observation(), identity, topology.name, lambda _: False)
            ep.atomic_json(directory / filename, document)
            self.assertEqual(json.loads((directory / filename).read_text())["topology_hash"], identity)
            self.assertEqual(topology.read_bytes(), before)
            self.assertEqual(len(load_epochs(Path(root))), 1)
            self.assertFalse(list(directory.glob("*.tmp.*")))

    def test_failed_topology_write_keeps_evidence_without_claiming_an_artifact(self):
        tracker = ep.EndpointTracker("rmw_zenoh_cpp", 3)
        observation = self.observation()
        _, _, failed = tracker.tick(observation, "topology", "missing.yaml", lambda _: False,
                                     topology_written=False)
        self.assertFalse(failed["topology_written"])
        self.assertIsNone(failed["topology_file"])
        self.assertEqual(failed["intended_topology_file"], "missing.yaml")
        self.assertTrue(failed["endpoint_evidence"]["topics"])
        action, _, healthy = tracker.tick(observation, "topology", "missing.yaml", lambda _: False)
        self.assertEqual(action, "open")
        self.assertTrue(healthy["topology_written"])
        self.assertNotEqual(failed["sha256"], healthy["sha256"])


if __name__ == "__main__":
    unittest.main()
