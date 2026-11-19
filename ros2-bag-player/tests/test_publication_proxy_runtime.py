"""Real MCAP evidence, split files, compression, and repeated proxy exports."""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from publication_identity import identity_key, iter_mcap_records
from test_publication_identity import metadata
import test_bag_proxy_runtime as existing
rosbag2_py = existing.rosbag2_py
if rosbag2_py is not None:
    from rclpy.serialization import serialize_message
    from std_msgs.msg import String


@unittest.skipIf(rosbag2_py is None, "publication proxy acceptance requires fleet-ros")
class PublicationProxyRuntime(unittest.TestCase):
    setUp = existing.BagProxyRuntime.setUp
    bag = existing.BagProxyRuntime.bag
    convert = existing.BagProxyRuntime.convert

    def marked(self, name, rows, *, split_at=None, codec="ZSTD", outer="", empty_topic=False,
               omit_marker_part=None):
        import yaml
        import zstandard
        from mcap.writer import CompressionType, Writer
        records = [("/value", "std_msgs/msg/String", serialize_message(String(data=row.get("text", "same"))),
                    row.get("received", 100 + i), row.get("sent", 90)) for i, row in enumerate(rows)]
        bag = self.bag(name, "mcap", records)
        root = Path(bag["path"])
        raw = list(iter_mcap_records(root))
        meta_path = root / "metadata.yaml"
        meta = yaml.safe_load(meta_path.read_text())
        info = meta["rosbag2_bagfile_information"]
        original = [root / name for name in info["relative_file_paths"]]
        parts = [(0, len(rows))] if split_at is None else [(0, split_at), (split_at, len(rows))]
        info["relative_file_paths"], info["files"] = [], []
        if empty_topic:
            item = copy.deepcopy(info["topics_with_message_count"][0])
            item["topic_metadata"]["name"] = "/empty"
            item["message_count"] = 0
            info["topics_with_message_count"].append(item)
        for part, (lo, hi) in enumerate(parts):
            output = root / f"part{part}.mcap"
            with output.open("wb") as stream:
                writer = Writer(stream, compression=getattr(CompressionType, codec), chunk_size=128,
                                enable_data_crcs=True)
                writer.start(profile="ros2")
                if part != omit_marker_part:
                    writer.add_metadata("rig.publication_provenance",
                                        {"schema": "1", "contract": "rmw_publication"})
                schema = raw[0].schema
                sid = writer.register_schema(schema.name, schema.encoding, schema.data)
                channels = {}
                if empty_topic:
                    writer.register_channel("/empty", "cdr", sid, dict(raw[0].channel.metadata))
                for i in range(lo, hi):
                    row, message = rows[i], raw[i].message
                    seq = row.get("sequence", i + 1)
                    evidence = metadata(gid=row.get("gid", "12" * 16), upper=seq >> 32,
                                        scope=row.get("scope", "boot:example"))
                    evidence.update(row.get("metadata", {}))
                    channel_metadata = {**raw[i].channel.metadata, **evidence}
                    key = json.dumps(channel_metadata, sort_keys=True)
                    if key not in channels:
                        channels[key] = writer.register_channel("/value", "cdr", sid, channel_metadata)
                    data = (zstandard.ZstdCompressor().compress(message.data) if outer == "message"
                            else message.data)
                    writer.add_message(channels[key], message.log_time, data, message.publish_time, seq & 0xffffffff)
                writer.finish()
            if outer == "file":
                compressed = output.with_suffix(".mcap.zstd")
                compressed.write_bytes(zstandard.ZstdCompressor().compress(output.read_bytes()))
                output.unlink()
                output = compressed
            info["relative_file_paths"].append(output.name)
            start = min(r.message.log_time for r in raw[lo:hi])
            end = max(r.message.log_time for r in raw[lo:hi])
            info["files"].append(dict(path=output.name, starting_time={"nanoseconds_since_epoch": start},
                                      duration={"nanoseconds": end-start}, message_count=hi-lo))
        for path in original:
            path.unlink()
        info["compression_mode"] = outer.upper() if outer else "NONE"
        info["compression_format"] = "zstd" if outer else ""
        meta_path.write_text(yaml.safe_dump(meta, sort_keys=False))
        return bag

    @staticmethod
    def identities(path):
        return [identity_key(row.channel.metadata, row.message.sequence) for row in iter_mcap_records(path)]

    def test_same_topic_channels_are_attached_to_exact_raw_record_not_sorted_ordinal(self):
        a = self.marked("a", [dict(gid="12"*16, sequence=1, received=200),
                               dict(gid="34"*16, sequence=1, received=100)])
        b = self.marked("b", [dict(gid="12"*16, sequence=1, received=300)])
        result, records, provenance, _ = self.convert([a, b], "merged")
        self.assertEqual(result["statistics"]["publication_duplicates"], 1)
        self.assertEqual(len(records), 2)
        self.assertEqual(provenance[2]["duplicate_of"], provenance[0]["id"])
        self.assertEqual(provenance[0]["source_record"]["record"], 0)
        self.assertEqual(provenance[1]["source_record"]["record"], 1)
        self.assertEqual(len(set(self.identities(self.root / "merged/bag"))), 2)

    def test_splits_full_width_sequence_repeated_delivery_and_second_export(self):
        a = self.marked("a", [dict(sequence=1), dict(sequence=2**32+1), dict(sequence=1)], split_at=1)
        b = self.marked("b", [dict(sequence=1, received=110), dict(sequence=2**32+1, received=111)], codec="LZ4")
        result, records, _, _ = self.convert([a, b], "first", time_source="sent")
        self.assertEqual((len(records), result["statistics"]["publication_duplicates"]), (3, 2))
        first = self.root / "first/bag"
        self.assertEqual(Counter(json.loads(k)[-1] for k in self.identities(first)), Counter({1: 2, 2**32+1: 1}))
        result, records, _, _ = self.convert([dict(path=str(first), storage="mcap"), b], "again")
        self.assertEqual((len(records), result["statistics"]["publication_duplicates"]), (3, 2))
        self.assertEqual(Counter(self.identities(first)), Counter(self.identities(self.root / "again/bag")))

    def test_equal_bytes_new_publications_and_restart_are_distinct_in_aggressive_mode(self):
        a = self.marked("a", [dict(sequence=1)])
        b = self.marked("b", [dict(sequence=2), dict(sequence=1, gid="34"*16)])
        result, records, _, _ = self.convert([a, b], "merged", deduplication="aggressive")
        self.assertEqual(len(records), 3)
        self.assertEqual(result["statistics"]["likely_duplicates"], 0)

    def test_late_conflict_preserves_every_record_and_reports_it(self):
        bags = [self.marked(name, [dict(sequence=1, text=text)])
                for name, text in [("a", "same"), ("b", "same"), ("c", "different")]]
        result, records, provenance, _ = self.convert(bags, "merged")
        self.assertEqual(len(records), 3)
        self.assertEqual(result["statistics"]["conflicting_publication_records"], 3)
        self.assertEqual({r["decision"] for r in provenance}, {"publication_identity_conflict"})

    def test_unsupported_marker_and_zero_gid_never_prove_a_duplicate(self):
        a = self.marked("a", [dict(sequence=1)])
        b = self.marked("b", [dict(sequence=1, gid="00"*16),
                               dict(sequence=1, metadata={"rig.provenance.schema": "2"})])
        result, records, _, _ = self.convert([a, b], "merged")
        self.assertEqual(len(records), 3)
        self.assertEqual(result["statistics"]["publication_identity_records"], 1)

    def test_outer_compression_and_mixed_storage_preserve_cdr_and_source_files(self):
        for mode in ("file", "message"):
            with self.subTest(mode=mode):
                a = self.marked("a_"+mode, [dict(sequence=7)], outer=mode)
                b = self.marked("b_"+mode, [dict(sequence=7, received=200)])
                unmarked = self.bag("plain_"+mode, "sqlite3", [("/value", "std_msgs/msg/String",
                    serialize_message(String(data="same")), 300, 90)])
                source = Path(a["path"])
                before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
                result, records, _, _ = self.convert([a, b, unmarked], "out_"+mode)
                after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
                self.assertEqual(before, after)
                self.assertEqual(len(records), 2)
                self.assertEqual(result["statistics"]["publication_duplicates"], 1)
                self.assertTrue(all(bytes(row[1]) == serialize_message(String(data="same")) for row in records))

    def test_empty_topics_qos_and_schema_survive_raw_output(self):
        import yaml
        a = self.marked("a", [dict(sequence=1), dict(sequence=1, gid="34"*16)], empty_topic=True)
        result, records, _, topics = self.convert([a], "merged")
        self.assertEqual(len(records), 2)
        self.assertEqual({t.name for t in topics}, {"/value", "/empty"})
        meta = yaml.safe_load((self.root / "merged/bag/metadata.yaml").read_text())["rosbag2_bagfile_information"]
        self.assertEqual({t["topic_metadata"]["name"]: t["message_count"] for t in meta["topics_with_message_count"]},
                         {"/value": 2, "/empty": 0})
        raw = list(iter_mcap_records(self.root / "merged/bag"))
        self.assertTrue(all(r.schema.name == "std_msgs/msg/String" and r.schema.data for r in raw))
        self.assertTrue(all("offered_qos_profiles" in r.channel.metadata for r in raw))
        self.assertTrue(result["validation"]["publication_metadata_verified"])
        from mcap.reader import make_reader
        with (self.root / "merged/bag" / meta["relative_file_paths"][0]).open("rb") as stream:
            markers = [m.metadata for m in make_reader(stream).iter_metadata()
                       if m.name == "rig.publication_provenance"]
        self.assertEqual(markers, [{"schema": "1", "contract": "rmw_publication"}])

    def test_marked_split_without_outer_marker_fails_before_export(self):
        a = self.marked("a", [dict(sequence=1), dict(sequence=2)], split_at=1, omit_marker_part=1)
        with self.assertRaisesRegex(ValueError, "lack required outer marker"):
            self.convert([a], "failed")
        self.assertFalse((self.root / "failed/bag").exists())

    def test_real_ros_compression_writer_file_and_message_bags(self):
        import yaml
        payload = serialize_message(String(data="real compression"))
        for mode in ("FILE", "MESSAGE"):
            with self.subTest(mode=mode):
                path = self.root / mode
                writer = rosbag2_py.SequentialCompressionWriter(rosbag2_py.CompressionOptions(
                    compression_format="zstd", compression_mode=getattr(rosbag2_py.CompressionMode, mode),
                    compression_queue_size=10, compression_threads=1))
                writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"),
                            rosbag2_py.ConverterOptions("cdr", "cdr"))
                writer.create_topic(rosbag2_py.TopicMetadata(id=0, name="/value", type="std_msgs/msg/String",
                                                            serialization_format="cdr"))
                writer.write("/value", payload, recv_timestamp=100, send_timestamp=90)
                del writer  # CompressionWriter finalizes/join its workers in its destructor.
                meta = yaml.safe_load((path / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
                self.assertEqual(meta["compression_mode"].upper(), mode)
                before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}
                _, records, _, _ = self.convert([dict(path=str(path), storage="mcap")], "out_"+mode)
                self.assertEqual(len(records), 1)
                self.assertEqual(bytes(records[0][1]), payload)
                self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()})

    def test_off_preserves_identity_evidence_without_dropping_messages(self):
        a = self.marked("a", [dict(sequence=7)])
        b = self.marked("b", [dict(sequence=7)])
        result, records, _, _ = self.convert([a, b], "off", deduplication="off")
        self.assertEqual(len(records), 2)
        self.assertEqual(result["statistics"]["publication_duplicates"], 0)
        identities = self.identities(self.root / "off/bag")
        self.assertEqual(len(identities), 2)
        self.assertIsNotNone(identities[0])
        self.assertEqual(identities[0], identities[1])

    def test_older_image_without_python_mcap_only_accepts_sqlite_inputs(self):
        payload = serialize_message(String(data="old image"))
        a = self.bag("sqlite", "sqlite3", [("/value", "std_msgs/msg/String", payload, 100, 90)])
        b = self.bag("mcap", "mcap", [("/value", "std_msgs/msg/String", payload, 100, 90)])
        with mock.patch.dict(sys.modules, {"mcap": None}):
            result, records, _, _ = self.convert([a], "legacy")
            self.assertEqual(bytes(records[0][1]), payload)
            self.assertFalse(result["validation"]["publication_metadata_verified"])
            with self.assertRaisesRegex(ValueError, "updated player image"):
                self.convert([b], "refused")


if __name__ == "__main__":
    unittest.main()
