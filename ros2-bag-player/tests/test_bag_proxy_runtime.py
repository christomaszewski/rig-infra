"""Real storage acceptance for offline proxy conversion; no ROS node or network."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bag_proxy
import play_cmd
try:
    import rosbag2_py
    from rclpy.serialization import serialize_message, deserialize_message
    from sensor_msgs.msg import Image
    from std_msgs.msg import String
    from std_srvs.srv import SetBool
except ImportError:
    rosbag2_py = None


@unittest.skipIf(rosbag2_py is None, "bag proxy acceptance requires fleet-ros")
class BagProxyRuntime(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def bag(self, name, storage, records):
        path = self.root / name
        writer = rosbag2_py.SequentialWriter()
        writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=storage),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
        from rosbag2_py._storage import QoS
        qos = QoS(10)
        qos.reliable() if storage == "mcap" else qos.best_effort()
        types = {topic: typ for topic, typ, _, _, _ in records}
        for number, (topic, typ) in enumerate(types.items()):
            writer.create_topic(rosbag2_py.TopicMetadata(id=number, name=topic, type=typ,
                                serialization_format="cdr", offered_qos_profiles=[qos]))
        for topic, _, payload, received, sent in records:
            writer.write(topic, payload, recv_timestamp=received, send_timestamp=sent)
        writer.close()
        return dict(path=str(path), logger=name, session="exact", storage=storage)

    def convert(self, bags, name, **policy):
        stage = self.root / name
        stage.mkdir()
        result = bag_proxy.convert(dict(version=1, bags=bags, destination=str(stage), policy=policy))
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(stage / "bag"), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
        channels = reader.get_all_topics_and_types()
        records = []
        while reader.has_next():
            records.append(reader.read_next_ext()[:4])
        del reader
        provenance = [json.loads(line) for line in (stage / "messages.jsonl").read_text().splitlines()]
        self.assertTrue(result["validation"]["payloads_verified"])
        self.assertEqual(result["statistics"]["output_messages"], len(records))
        self.assertEqual(result["statistics"]["input_messages"], len(provenance))
        self.assertFalse((stage / "merge.sqlite3").exists())
        return result, records, provenance, channels

    def test_mixed_storage_conservative_and_aggressive_keep_same_source_repetition(self):
        payload = serialize_message(String(data="identical ordinary message"))
        event = SetBool.Event()
        event.info.event_type = 1
        event.info.client_gid = [1] * 16
        event.info.sequence_number = 5
        event.info.stamp.sec = 99
        event.request = [SetBool.Request(data=True)]
        request = serialize_message(event)
        bags = []
        base = 100 * 10**9
        for name, storage, offset in (("a", "mcap", 0), ("b", "sqlite3", 10_000_000)):
            bags.append(self.bag(name, storage, [
                ("/value", "std_msgs/msg/String", payload, base + offset, base - 100),
                ("/value", "std_msgs/msg/String", payload, base + offset + 20_000_000, base - 100),
                ("/control/_service_event", "std_srvs/srv/SetBool_Event", request, base + 10**9 + offset, base)]))
        for mode, count, ordinary in (("conservative", 5, 4), ("aggressive", 3, 2), ("off", 6, 4)):
            result, records, provenance, channels = self.convert(bags, mode, deduplication=mode)
            self.assertEqual(result["statistics"]["output_messages"], count)
            self.assertEqual(result["statistics"]["proven_duplicates"], 0 if mode == "off" else 1)
            self.assertEqual(result["statistics"]["likely_duplicates"], 2 if mode == "aggressive" else 0)
            self.assertEqual(sum(topic == "/value" for topic, *_ in records), ordinary)
            self.assertTrue(all(bytes(data) in (payload, request) for _, data, _, _ in records))
            dropped = [row for row in provenance if row["duplicate_of"] is not None]
            self.assertEqual(len(dropped), 6 - count)
            # Equal payloads within one recorder remain two distinct occurrences.
            self.assertEqual(sum(row["input"] == 0 and row["topic"] == "/value" and
                                 row["duplicate_of"] is None for row in provenance), 2)
            self.assertTrue(all(len(channel.offered_qos_profiles) == 2 for channel in channels))

    def test_sensor_order_changes_schedule_without_rewriting_payload_or_send_time(self):
        a = Image()
        a.header.stamp.sec = 103
        a.header.frame_id = "a"
        b = Image()
        b.header.stamp.sec = 101
        b.header.frame_id = "b"
        pa, pb = serialize_message(a), serialize_message(b)
        bags = [self.bag("a", "mcap", [("/image", "sensor_msgs/msg/Image", pa, 100 * 10**9, 99 * 10**9)]),
                self.bag("b", "sqlite3", [("/image", "sensor_msgs/msg/Image", pb, 100 * 10**9 + 10_000_000, 99 * 10**9)])]
        result, records, provenance, _ = self.convert(bags, "sensor", time_source="sensor")
        self.assertEqual([bytes(record[1]) for record in records], [pb, pa])
        self.assertEqual([record[2] for record in records], [101 * 10**9, 103 * 10**9])
        self.assertEqual(records[1][3], 99 * 10**9)
        self.assertEqual([deserialize_message(record[1], Image).header.frame_id for record in records], ["b", "a"])
        self.assertEqual([row["received_ns"] for row in provenance], [100 * 10**9, 100 * 10**9 + 10_000_000])
        self.assertEqual(result["statistics"]["timestamp_fallbacks"], 0)
        self.assertEqual(result["statistics"]["output_messages"], 2)

    def test_single_timestamp_proxy_remains_replayable_without_rewriting_metadata(self):
        payload = serialize_message(String(data="one instant"))
        bags = [self.bag("one", "mcap", [("/value", "std_msgs/msg/String", payload, 100 * 10**9, 99 * 10**9)])]
        result, records, _, _ = self.convert(bags, "single")
        self.assertEqual(result["statistics"]["output_messages"], 1)
        metadata_path = self.root / "single/bag/metadata.yaml"
        before = metadata_path.read_bytes()
        self.assertEqual(play_cmd.read_bag_bounds(before.decode(), str(metadata_path)), (100 * 10**9, 1))
        plan = dict(version=2, source=str(self.root), bags=[dict(logger="proxy", session="exact",
                    relative_path="single/bag", storage="mcap")],
                    timeline=dict(start_ns=100 * 10**9, duration_ns=1), topics=["/value"], remap={}, services=[],
                    proxy=dict(export_id="a" * 64, time_source="received", deduplication="conservative"))
        handoff = self.root / "plan.json"
        handoff.write_text(json.dumps(plan))
        self.assertEqual(play_cmd.build_args({}, {"RIG_REPLAY_PLAN": str(handoff)})[5]["duration_ns"], 1)
        self.assertEqual(metadata_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
