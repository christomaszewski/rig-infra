#!/usr/bin/env python3
"""Run in fleet-ros: exact synthetic MCAP provenance preservation through ros2 bag convert."""
from collections import Counter
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest


PREFIX = "rig.provenance."


def bag_files(path):
    import yaml
    if path.is_file():
        return [path], ""
    info = yaml.safe_load((path / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
    return [path / item for item in info["relative_file_paths"]], info.get("compression_mode", "").lower()


def mcap_stream(path):
    data = path.read_bytes()
    if path.suffix == ".zstd":
        import zstandard
        data = zstandard.ZstdDecompressor().decompress(data)
    return BytesIO(data)


def records(path):
    from mcap.reader import make_reader
    result = []
    files, compression = bag_files(path)
    for file in files:
        with mcap_stream(file) as stream:
            for schema, channel, message in make_reader(stream).iter_messages(log_time_order=False):
                data = message.data
                if compression == "message":
                    import zstandard
                    data = zstandard.ZstdDecompressor().decompress(data)
                result.append((channel.topic, schema.name, tuple(sorted(channel.metadata.items())),
                               message.sequence, message.log_time, message.publish_time, data))
    return result


def publication(record):
    metadata = dict(record[2])
    if metadata.get(PREFIX + "schema") != "1":
        return None
    return tuple(metadata[PREFIX + key] for key in
                 ("scope", "rmw", "domain_id", "writer_gid")) + (
                     (int(metadata[PREFIX + "sequence_upper32"]) << 32) | record[3],)


class PublicationProvenance(unittest.TestCase):
    def test_convert_preserves_channels_sequence_timestamps_and_cdr(self):
        from mcap.writer import Writer, CompressionType
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mcap"
            with source.open("wb") as stream:
                writer = Writer(stream, compression=CompressionType.ZSTD)
                writer.start(profile="ros2")
                schema = writer.register_schema("std_msgs/msg/String", "ros2msg", b"string data\n")
                common = {PREFIX + "schema": "1", PREFIX + "kind": "rmw_publication",
                          PREFIX + "scope": "test:scope", PREFIX + "rmw": "rmw_zenoh_cpp",
                          PREFIX + "domain_id": "42", "topic_type_hash": "RIHS01_" + "a" * 64,
                          "offered_qos_profiles": "[]"}
                # Same payload and topic: different writers and high32 values must stay distinct.
                for index, (gid, high, low) in enumerate((("01" * 16, 0, 0xffffffff), ("01" * 16, 1, 0),
                                       ("02" * 16, 1, 0), ("03" * 16, 0xffffffff, 0xfffffffe))):
                    metadata = dict(common, **{PREFIX + "writer_gid": gid,
                                              PREFIX + "sequence_upper32": str(high)})
                    channel = writer.register_channel("/provenance", "cdr", schema, metadata)
                    writer.add_message(channel, 100 + index * 2_000_000_000,
                                       b"\x00\x01\x00\x00\x02\x00\x00\x00x\x00", 50, low)
                channel = writer.register_channel("/provenance", "cdr", schema,
                                                  {"topic_type_hash": "", "offered_qos_profiles": "[]"})
                writer.add_message(channel, 8_000_000_100, b"\x00\x01\x00\x00\x02\x00\x00\x00x\x00", 51, 7)
                writer.finish()
            for mode, split in ((mode, split) for mode in ("none", "file", "message")
                                for split in (False, True)):
                with self.subTest(mode=mode, split=split):
                    output = root / (mode + ("-split" if split else "-converted"))
                    config = root / "convert.yaml"
                    options = {"uri": str(output), "storage_id": "mcap", "all_topics": True,
                               "storage_preset_profile": "zstd_small"}
                    if split:
                        options["max_bagfile_duration"] = 1
                    if mode != "none":
                        options.update(compression_mode=mode, compression_format="zstd")
                    config.write_text(yaml.safe_dump({"output_bags": [options]}))
                    completed = subprocess.run(["ros2", "bag", "convert", "-i", str(source), "-o", str(config)],
                                               capture_output=True, text=True, timeout=60)
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    self.assertEqual(bag_files(output)[1], "" if mode == "none" else mode)
                    self.assertEqual(Counter(records(source)), Counter(records(output)))
                    if split:
                        self.assertGreater(len(bag_files(output)[0]), 1)
                    from mcap.reader import make_reader
                    for file in bag_files(output)[0]:
                        with mcap_stream(file) as stream:
                            metadata = list(make_reader(stream).iter_metadata())
                        marked = any(publication(record) for record in records(file))
                        self.assertEqual(marked, any(item.name == "rig.publication_provenance" for item in metadata))
                    if mode != "none":
                        decoded = root / (output.name + "-decoded")
                        config.write_text(yaml.safe_dump({"output_bags": [{"uri": str(decoded),
                            "storage_id": "mcap", "all_topics": True}]}))
                        completed = subprocess.run(["ros2", "bag", "convert", "-i", str(output),
                                                   "-o", str(config)], capture_output=True, text=True, timeout=60)
                        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                        self.assertEqual(Counter(records(source)), Counter(records(decoded)))



if __name__ == "__main__":
    unittest.main(verbosity=2)
