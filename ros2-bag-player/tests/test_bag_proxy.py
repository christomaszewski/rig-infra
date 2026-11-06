"""Offline dedup decisions, timing, provenance, and path isolation without ROS."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bag_proxy import MergeIndex, effective_timestamp, policy, proven_identity
from bag_proxy_host import mapped_request


def event(sequence=1, gid=b"x" * 16, stamp=1):
    return NS(info=NS(client_gid=gid, sequence_number=sequence, event_type=1,
                      stamp=NS(sec=stamp, nanosec=0)))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def index(self, **settings):
        result = MergeIndex(self.root / "index.db", settings)
        self.addCleanup(result.close)
        return result

    def test_equal_ordinary_messages_are_ambiguous_in_conservative_mode(self):
        index = self.index()
        index.add(0, 0, "/sample", "std_msgs/msg/String", b"same", 100, 50)
        index.add(1, 0, "/sample", "std_msgs/msg/String", b"same", 105, 50)
        stats = index.deduplicate()
        self.assertEqual(stats["output_messages"], 2)
        self.assertEqual(stats["ambiguous_preserved"], 1)
        self.assertEqual([r[1] for r in index.output()], [b"same", b"same"])

    def test_aggressive_pairing_preserves_repeats_within_each_recording(self):
        index = self.index(deduplication="aggressive")
        for source in (0, 1):
            for ordinal in (0, 1):
                index.add(source, ordinal, "/sample", "T", b"same", 100 + source + ordinal, 0)
        stats = index.deduplicate()
        self.assertEqual(stats["likely_duplicates"], 2)
        self.assertEqual(stats["output_messages"], 2)

    def test_aggressive_window_and_topic_boundaries(self):
        index = self.index(deduplication="aggressive", dedup_window_ms=0.00001)
        for source, topic, stamp in ((0, "/a", 100), (1, "/a", 111), (2, "/b", 100)):
            index.add(source, 0, topic, "T", b"same", stamp, 0)
        self.assertEqual(index.deduplicate()["output_messages"], 3)

    def test_service_identity_is_conservative_and_cross_recording_only(self):
        index = self.index()
        for source, ordinal, stamp in ((0, 0, 100), (0, 1, 110), (1, 0, 200)):
            index.add(source, ordinal, "/call/_service_event", "std_srvs/srv/Trigger_Event",
                      b"same-event", stamp, 0, event())
        stats = index.deduplicate()
        self.assertEqual(stats["proven_duplicates"], 1)
        self.assertEqual(stats["output_messages"], 2)
        index.provenance(self.root / "messages.jsonl")
        rows = [json.loads(s) for s in (self.root / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(rows[2]["duplicate_of"], rows[0]["id"])
        self.assertEqual(rows[2]["received_ns"], 200)

    def test_unknown_gid_and_different_event_bytes_are_not_proven(self):
        self.assertIsNone(proven_identity("std_srvs/srv/Trigger_Event", event(gid=b"\0" * 16)))
        index = self.index()
        for source, payload in ((0, b"a"), (1, b"b")):
            index.add(source, 0, "/call/_service_event", "std_srvs/srv/Trigger_Event",
                      payload, 100, 0, event())
        self.assertEqual(index.deduplicate()["proven_duplicates"], 0)

    def test_sensor_reordering_keeps_payload_and_send_time(self):
        index = self.index(time_source="sensor")
        index.add(0, 0, "/image", "T", b"first-arrival", 4_000_000_000, 3_100_000_000,
                  NS(header=NS(stamp=NS(sec=3, nanosec=0))))
        index.add(1, 0, "/image", "T", b"second-arrival", 5_000_000_000, 2_100_000_000,
                  NS(header=NS(stamp=NS(sec=2, nanosec=0))))
        index.deduplicate()
        output = list(index.output())
        self.assertEqual([r[1] for r in output], [b"second-arrival", b"first-arrival"])
        self.assertEqual([r[2] for r in output], [2_000_000_000, 3_000_000_000])
        self.assertEqual(output[0][3], 2_100_000_000)

    def test_dedup_decision_uses_receive_order_even_with_sensor_sort(self):
        index = self.index(time_source="sensor", deduplication="aggressive")
        # API fixtures use identical bytes with different decoded stamps to isolate ordering.
        index.add(1, 0, "/sample", "T", b"same", 3_000_000_010, 0,
                  NS(header=NS(stamp=NS(sec=1, nanosec=0))))
        index.add(0, 0, "/sample", "T", b"same", 3_000_000_000, 0,
                  NS(header=NS(stamp=NS(sec=2, nanosec=0))))
        self.assertEqual(index.deduplicate()["output_messages"], 1)
        self.assertEqual(list(index.output())[0][2], 2_000_000_000)

    def test_fallback_is_explicit_and_clock_domain_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, "timestamp_fallback"):
            effective_timestamp("/a", 1_000_000_000, 0, None, policy({"time_source": "sensor"}))
        settings = policy({"time_source": "sensor", "timestamp_fallback": "received"})
        self.assertEqual(effective_timestamp("/a", 1_000_000_000, 0, None, settings), (1_000_000_000, True))
        message = NS(header=NS(stamp=NS(sec=100, nanosec=0)))
        with self.assertRaisesRegex(ValueError, "clock domain"):
            effective_timestamp("/a", 1_000_000_000, 0, message, settings)

    def test_per_topic_timestamp_field(self):
        settings = policy({"time_source": "sensor", "topic_timestamp_fields": {"/a": "capture"}})
        self.assertEqual(effective_timestamp("/a", 2_000_000_000, 0,
                         NS(capture=NS(sec=1, nanosec=12)), settings), (1_000_000_012, False))

    def test_host_rejects_escape_and_maps_only_exact_inputs(self):
        source, stage = self.root / "source", self.root / "stage"
        (source / "bags/logger/session").mkdir(parents=True)
        (source / "bags/logger/session/metadata.yaml").write_text("metadata")
        stage.mkdir()
        req = dict(version=1, source_root=str(source), destination=str(stage),
                   result=str(stage / "result.json"), policy={},
                   bags=[dict(path=str(source / "bags/logger/session"), storage="mcap")])
        self.assertEqual(mapped_request(req)["bags"][0]["path"], "/source/bags/logger/session")
        req["result"] = str(self.root / "escape.json")
        with self.assertRaisesRegex(ValueError, "result path"):
            mapped_request(req)


if __name__ == "__main__":
    unittest.main()
