"""Real storage acceptance for offline proxy conversion; no ROS node or network."""
from array import array
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bag_proxy
import play_cmd
try:
    import rosbag2_py
    from rclpy.serialization import serialize_message, deserialize_message
    from geometry_msgs.msg import TransformStamped
    from sensor_msgs.msg import Image, PointCloud2, PointField
    from std_msgs.msg import String
    from std_srvs.srv import SetBool
    from tf2_msgs.msg import TFMessage
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
        types = {topic: typ for topic, typ, _, _, _ in records}
        for number, (topic, typ) in enumerate(types.items()):
            qos = QoS(10)
            qos.reliable() if storage == "mcap" else qos.best_effort()
            if typ == "tf2_msgs/msg/TFMessage" and topic.rsplit("/", 1)[-1] == "tf_static":
                qos.transient_local()
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

    def test_sensor_conservative_preserves_organized_lidar_clouds_and_prefetches_tf(self):
        def cloud(stamp, seed):
            message = PointCloud2()
            message.header.stamp.sec, message.header.stamp.nanosec = divmod(stamp, 10**9)
            message.header.frame_id = "os_lidar"
            message.height, message.width = 2, 3
            message.point_step, message.row_step = 48, 3 * 48 + 8
            message.is_bigendian, message.is_dense = False, False
            message.fields = [PointField(name=name, offset=offset, datatype=datatype, count=1)
                for name, offset, datatype in (
                    ("x", 0, PointField.FLOAT32), ("y", 4, PointField.FLOAT32),
                    ("z", 8, PointField.FLOAT32), ("intensity", 16, PointField.FLOAT32),
                    ("t", 20, PointField.UINT32), ("reflectivity", 24, PointField.UINT16),
                    ("ring", 26, PointField.UINT16), ("ambient", 28, PointField.UINT16),
                    ("range", 32, PointField.UINT32))]
            # Point and row padding is intentionally nonzero. Float bits include
            # negative zero, NaN payloads, infinities and a subnormal; no float round trip.
            data = bytearray([0xA5 + seed] * (message.row_step * message.height))
            bits = (0x3F800000, 0x80000000, 0x7FC12345, 0xFF800000, 0x7F800000, 0x00000001)
            for index, x_bits in enumerate(bits):
                offset = (index // message.width) * message.row_step + (index % message.width) * message.point_step
                struct.pack_into("<III", data, offset, x_bits, 0x40000000, 0x40400000)
                struct.pack_into("<IIHHH", data, offset + 16, 0x7FA54321,
                                 1_000_000 + index * 12_345, 1234 + seed + index,
                                 index // message.width, 4321 + index)
                struct.pack_into("<I", data, offset + 32, 10_000 + index)
            message.data = array("B", data)
            return message

        base = 100 * 10**9
        early, late = cloud(base + 10**9 + 222, 0), cloud(base + 3 * 10**9 + 333, 1)
        early_bytes, late_bytes = serialize_message(early), serialize_message(late)
        transform = TransformStamped()
        transform.header.stamp.sec = early.header.stamp.sec
        transform.header.stamp.nanosec = early.header.stamp.nanosec
        transform.header.frame_id, transform.child_frame_id = "base_link", "os_lidar"
        transform.transform.translation.x = 1.25
        transform.transform.rotation.w = 1.0
        tf = TFMessage(transforms=[transform])
        tf_bytes = serialize_message(tf)
        topic, typ = "/os_cloud_node/points", "sensor_msgs/msg/PointCloud2"
        bags = [self.bag("lidar-a", "mcap", [
            (topic, typ, late_bytes, base + 4_000_000_000, base + 3_010_000_000),
            ("/tf", "tf2_msgs/msg/TFMessage", tf_bytes, base + 4_100_000_000, base + 4_050_000_000),
            (topic, typ, early_bytes, base + 4_200_000_000, base + 1_010_000_000)]),
            self.bag("lidar-b", "sqlite3", [
                (topic, typ, early_bytes, base + 4_250_000_000, base + 1_010_000_000)])]
        originals = {}
        for source, bag in enumerate(bags):
            reader = rosbag2_py.SequentialReader()
            reader.open(rosbag2_py.StorageOptions(uri=bag["path"], storage_id=bag["storage"]),
                        rosbag2_py.ConverterOptions("cdr", "cdr"))
            ordinal = 0
            while reader.has_next():
                channel, payload, received, sent = reader.read_next_ext()[:4]
                originals[source, ordinal] = (channel, bytes(payload), received, sent)
                ordinal += 1
            del reader
        clouds = {early_bytes: early, late_bytes: late}
        schedules = {payload: msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
                     for payload, msg in clouds.items()}
        schedules[tf_bytes] = schedules[early_bytes] - 100_000_000
        # Acquisition precedes receipt. The matching TF arrives before the early
        # cloud and must also precede it after the cloud moves to acquisition time.
        for profile, order in (("arrival-conservative", [late_bytes, tf_bytes, early_bytes, early_bytes]),
                               ("sensor-conservative", [tf_bytes, early_bytes, early_bytes, late_bytes])):
            with self.subTest(profile=profile):
                settings = json.loads((Path(__file__).resolve().parents[1] /
                                       "export-profiles" / f"{profile}.yaml").read_text())["policy"]
                result, records, provenance, _ = self.convert(bags, profile, **settings)
                self.assertEqual([bytes(row[1]) for row in records], order)
                self.assertEqual(Counter(bytes(row[1]) for row in records if row[0] == topic),
                                 Counter([early_bytes, early_bytes, late_bytes]))
                self.assertEqual([row[2] for row in records], sorted(row[2] for row in records))
                expected = []
                for row in provenance:
                    channel, payload, received, sent = originals[row["input"], row["ordinal"]]
                    schedule = schedules.get(payload, received) if settings["time_source"] == "sensor" else received
                    self.assertEqual((row["topic"], row["received_ns"], row["sent_ns"], row["schedule_ns"]),
                                     (channel, received, sent, schedule))
                    self.assertIsNone(row["duplicate_of"])
                    expected.append((schedule, row["input"], row["ordinal"], channel, payload, sent))
                self.assertEqual([(r[0], bytes(r[1]), r[2], r[3]) for r in records],
                                 [(r[3], r[4], r[0], r[5]) for r in sorted(expected)])
                for channel, payload, _, _ in records:
                    if channel == topic:
                        decoded, original = deserialize_message(payload, PointCloud2), clouds[bytes(payload)]
                        for field in ("header", "fields", "height", "width", "point_step", "row_step",
                                      "is_bigendian", "is_dense", "data"):
                            self.assertEqual(getattr(decoded, field), getattr(original, field))
                    else:
                        self.assertEqual(deserialize_message(payload, TFMessage), tf)
                self.assertEqual(result["statistics"]["output_messages"], 4)
                self.assertEqual(result["statistics"]["ambiguous_preserved"], 1)
                self.assertEqual(result["statistics"]["proven_duplicates"], 0)
                self.assertEqual(result["statistics"]["likely_duplicates"], 0)
                self.assertEqual(result["statistics"]["timestamp_fallbacks"], 0)

    @staticmethod
    def transform(stamp, parent, child, x=0.0):
        transform = TransformStamped()
        transform.header.stamp.sec, transform.header.stamp.nanosec = divmod(stamp, 10**9)
        transform.header.frame_id, transform.child_frame_id = parent, child
        transform.transform.translation.x = float(x)
        transform.transform.rotation.w = 1.0
        return transform

    def test_sensor_tf_prefetch_supports_exact_and_interpolated_cloud_lookups(self):
        from latch_restore import read_last
        from rclpy.time import Time
        from tf2_py import BufferCore

        base = 100 * 10**9
        cloud_topic, cloud_type = "/robot/points", "sensor_msgs/msg/PointCloud2"
        clouds = []
        for stamp in (base, base + 50_000_000):
            cloud = PointCloud2()
            cloud.header.stamp.sec, cloud.header.stamp.nanosec = divmod(stamp, 10**9)
            cloud.header.frame_id = "lidar"
            clouds.append(serialize_message(cloud))
        transforms = [TFMessage(transforms=[self.transform(base, "world", "base"),
                                            self.transform(base + 25_000_000, "world", "other", 3)]),
                      TFMessage(transforms=[self.transform(base + 100_000_000, "world", "base", 1)])]
        # Static stamps can be zero or from a different epoch; they do not describe
        # when the fixed geometry becomes valid. Separate payloads carry separate edges.
        statics = [TFMessage(transforms=[self.transform(0, "base", "mount", 10)]),
                   TFMessage(transforms=[self.transform(1, "mount", "lidar", 1)])]
        source = [(cloud_topic, cloud_type, payload, base + 1_000_000_000 + i * 100_000_000, base)
                  for i, payload in enumerate(clouds)]
        source += [("/robot/tf", "tf2_msgs/msg/TFMessage", serialize_message(message),
                    base + 1_200_000_000 + i * 100_000_000, base)
                   for i, message in enumerate(transforms)]
        source += [("/robot/tf_static", "tf2_msgs/msg/TFMessage", serialize_message(message),
                    base + 1_400_000_000 + i * 100_000_000, base)
                   for i, message in enumerate(statics)]
        bags = [self.bag("late-tf", "mcap", source)]
        result, records, provenance, _ = self.convert(
            bags, "prefetch", time_source="sensor", deduplication="conservative",
            timestamp_fallback="received")
        self.assertEqual(Counter(hashlib.sha256(bytes(row[1])).hexdigest() for row in records),
                         Counter(hashlib.sha256(row[2]).hexdigest() for row in source))
        self.assertEqual([row[0] for row in records], ["/robot/tf_static", "/robot/tf_static",
                         "/robot/tf", "/robot/tf", cloud_topic, cloud_topic])
        self.assertLess(records[1][2], records[2][2])
        self.assertEqual([row[2] for row in records[2:]],
                         [base - 100_000_000, base, base, base + 50_000_000])
        self.assertTrue(all(row["duplicate_of"] is None for row in provenance))
        self.assertEqual(result["statistics"]["timestamp_fallbacks"], 0)

        # A window beginning after the prelude must restore both fixed edges, even
        # though the final /tf_static payload contains only the lidar edge.
        restored = read_last(str(self.root / "prefetch/bag"), ["/robot/tf_static"], base,
                             static_tf_topics=["/robot/tf_static"])
        static_union = deserialize_message(restored["/robot/tf_static"][1], TFMessage)
        self.assertEqual({transform.child_frame_id: transform for transform in static_union.transforms},
                         {message.transforms[0].child_frame_id: message.transforms[0] for message in statics})
        static_buffer = BufferCore()
        for transform in static_union.transforms:
            static_buffer.set_transform_static(transform, "window-regression")
        self.assertEqual(static_buffer.lookup_transform_core("base", "lidar", Time(nanoseconds=base))
                         .transform.translation.x, 11.0)

        # Feed the actual native tf2 cache strictly in storage order. The second
        # cloud needs both bracketing dynamic samples, plus both static edges.
        buffer = BufferCore()
        looked_up = []
        for topic, payload, _, _ in records:
            if topic == cloud_topic:
                message = deserialize_message(payload, PointCloud2)
                transformed = buffer.lookup_transform_core("world", message.header.frame_id,
                                                           Time.from_msg(message.header.stamp))
                looked_up.append(transformed.transform.translation.x)
            else:
                setter = buffer.set_transform_static if topic.endswith("/tf_static") else buffer.set_transform
                for transform in deserialize_message(payload, TFMessage).transforms:
                    setter(transform, "proxy-regression")
        self.assertEqual(looked_up, [11.0, 11.5])

    def test_sensor_tf_wins_equal_schedule_across_sources_without_lead(self):
        base = 100 * 10**9
        cloud = PointCloud2()
        cloud.header.stamp.sec, cloud.header.frame_id = 100, "lidar"
        cloud_bytes = serialize_message(cloud)
        tf_bytes = serialize_message(TFMessage(transforms=[self.transform(base, "world", "lidar")]))
        bags = [self.bag("cloud-first", "mcap", [
                    ("/points", "sensor_msgs/msg/PointCloud2", cloud_bytes, base + 10_000_000, base)]),
                self.bag("tf-second", "sqlite3", [
                    ("/tf", "tf2_msgs/msg/TFMessage", tf_bytes, base + 20_000_000, base)])]
        _, records, provenance, _ = self.convert(bags, "tie", time_source="sensor", tf_lead_time_ms=0)
        self.assertEqual([(topic, bytes(payload), received) for topic, payload, received, _ in records],
                         [("/tf", tf_bytes, base), ("/points", cloud_bytes, base)])
        self.assertEqual([row["schedule_ns"] for row in provenance], [base, base])

    def test_sensor_tf_rejects_hidden_clock_skew_and_changing_static_geometry(self):
        base = 100 * 10**9
        mixed_clocks = TFMessage(transforms=[self.transform(base, "world", "base"),
                                            self.transform(base + 61 * 10**9, "world", "other")])
        bags = [self.bag("mixed-clock", "mcap", [("/tf", "tf2_msgs/msg/TFMessage",
                serialize_message(mixed_clocks), base, base)])]
        with self.assertRaisesRegex(ValueError, "max_clock_skew_s"):
            self.convert(bags, "bad-clock", time_source="sensor", timestamp_fallback="received")

        originals = [TFMessage(transforms=[self.transform(0, "base", "lidar", x)]) for x in (1, 2)]
        bags = [self.bag("moving-static", "mcap", [("/tf_static", "tf2_msgs/msg/TFMessage",
                serialize_message(message), base + i, base) for i, message in enumerate(originals)])]
        with self.assertRaisesRegex(ValueError, "(?i)static"):
            self.convert(bags, "bad-static", time_source="sensor", timestamp_fallback="received")

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
