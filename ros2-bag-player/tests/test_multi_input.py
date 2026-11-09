"""Multi-input contracts: one player, one timeline, retained duplicate records."""
import json
from pathlib import Path
import shlex
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bag_inputs
import inspect_calls
import inspect_calls_host
import latch_restore
import play_cmd


class Reader:
    def __init__(self, records):
        self.records = iter(records)
        self.next = next(self.records, None)

    def has_next(self):
        return self.next is not None

    def read_next_ext(self):
        item, self.next = self.next, next(self.records, None)
        return item

    def set_filter(self, value):
        pass

    def seek(self, lower):
        while self.has_next() and self.next[2] < lower:
            self.read_next_ext()


class MultiInput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bags = []
        for logger, storage, start, duration in (("a", "mcap", 100, 3), ("b", "sqlite3", 101, 4)):
            directory = self.root / "bags" / logger / "exact"
            directory.mkdir(parents=True)
            (directory / ("data.mcap" if storage == "mcap" else "data.db3")).write_bytes(b"fixture")
            (directory / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
                "storage_identifier": storage,
                "starting_time": {"nanoseconds_since_epoch": start * 10**9},
                "duration": {"nanoseconds": duration * 10**9},
                "topics_with_message_count": [{"topic_metadata": {"name": "/value",
                    "type": "std_msgs/msg/String", "serialization_format": "cdr"}, "message_count": 4}]}}))
            self.bags.append(dict(logger=logger, session="exact", relative_path=f"bags/{logger}/exact", storage=storage))
        self.plan = dict(version=2, source=str(self.root), bags=self.bags,
                         timeline=dict(start_ns=100 * 10**9, duration_ns=5 * 10**9),
                         topics=["/value"], remap={}, services=[])
        self.path = self.root / "plan.json"

    def build(self, config=None, **env):
        self.path.write_text(json.dumps(self.plan))
        return play_cmd.build_args(config or {}, {"RIG_REPLAY_PLAN": str(self.path), **env})

    def test_one_player_has_repeated_inputs_global_window_and_one_clock(self):
        _, _, path, args, _, extras = self.build(RIG_SIM_TIME="1", RIG_REPLAY_FROM_S="2", RIG_REPLAY_TO_S="4")
        self.assertEqual(extras["bag_start_ns"], 100 * 10**9)
        self.assertEqual(extras["duration_ns"], 5 * 10**9)
        self.assertEqual(args[args.index("--playback-until-nsec") + 1], str(104 * 10**9 - 1))
        for coordinated in (False, True):
            script = play_cmd.play_script(path, args, extras["latch"], inputs=extras["inputs"],
                                          start_ns=extras["bag_start_ns"], coordinated=coordinated)
            command = shlex.split(script.splitlines()[-1])
            self.assertEqual(command.count("--input"), 2)
            self.assertEqual(command.count("--clock"), 1)
            self.assertEqual(command.count("play"), 1)
            self.assertIn("--message-order", command)
            self.assertEqual(command[command.index("--input") + 1:command.index("--input") + 3],
                             ["/replay/bags/a/exact", "mcap"])
            self.assertNotEqual(command[command.index("play") + 1], path)
        rendered = play_cmd.render({}, {"RIG_REPLAY_PLAN": str(self.path)}, self.root / "launcher")
        inputs, zero = bag_inputs.parse_selection(rendered[3])
        self.assertEqual(len(inputs), 2)
        self.assertEqual(zero, 100 * 10**9)

    def test_changed_bounds_and_conflicting_types_fail_before_launch(self):
        self.plan["timeline"]["start_ns"] -= 1
        with self.assertRaisesRegex(SystemExit, "metadata changed"):
            self.build()
        self.plan["timeline"]["start_ns"] += 1
        path = self.root / "bags/b/exact/metadata.yaml"
        metadata = yaml.safe_load(path.read_text())
        metadata["rosbag2_bagfile_information"]["topics_with_message_count"][0]["topic_metadata"]["type"] = "std_msgs/msg/Int32"
        path.write_text(yaml.safe_dump(metadata))
        with self.assertRaisesRegex(SystemExit, "conflicting recorded types"):
            self.build()

    def test_proxy_can_bind_under_exports_without_synthetic_bags_layout(self):
        relative = "exports/inputs/bags/sensor-conservative/export-1/bag"
        destination = self.root / relative
        destination.parent.mkdir(parents=True)
        (self.root / self.bags[0]["relative_path"]).rename(destination)
        self.plan.update(bags=[{**self.bags[0], "relative_path": relative}],
                         timeline=dict(start_ns=100 * 10**9, duration_ns=3 * 10**9),
                         proxy=dict(export_id="a" * 64, time_source="sensor", deduplication="conservative"))
        _, _, path, _, _, extras = self.build({"play": {"time_source": "sensor"}})
        self.assertEqual(path, "/replay/" + relative)
        self.assertEqual(extras["inputs"], [{"path": path, "storage": "mcap"}])
        request = dict(version=2, source=str(self.root), bags=self.plan["bags"], start_ns=100 * 10**9,
                       from_s=0, to_s=None, services=["/control"], source_mode="service")
        self.path.write_text(json.dumps(request))
        env = {"BAG_PLAYER_IMAGE": "test:image", "RIG_TARGET_ARCH": "arm64",
               "RIG_RUNTIME_IMAGE_PINS": json.dumps({"version": 1, "images": {"test:image": "sha256:" + "a" * 64}})}
        self.assertIn(f"type=bind,src={self.root},dst=/bag,readonly",
                      inspect_calls_host.command(self.path, env, "proxy-test"))
    def test_duplicate_sessions_and_escaping_paths_are_rejected(self):
        for bags in ([self.bags[0], self.bags[0]], [{**self.bags[0], "relative_path": "../elsewhere"}]):
            self.plan["bags"] = bags
            with self.assertRaises(SystemExit):
                self.build()

    def test_unknown_zero_schema_hash_does_not_conflict_with_known_hash(self):
        for name, stamp in (("a", "0"), ("b", "a")):
            path = self.root / f"bags/{name}/exact/metadata.yaml"
            metadata = yaml.safe_load(path.read_text())
            metadata["rosbag2_bagfile_information"]["topics_with_message_count"][0]["topic_metadata"]["type_description_hash"] = "RIHS01_" + stamp * 64
            path.write_text(yaml.safe_dump(metadata))
        self.assertEqual(len(self.build()[5]["inputs"]), 2)

    def test_image_inventory_accepts_unrepaired_metadata_but_playback_refuses(self):
        self.plan["timeline"] = dict(start_ns=None, duration_ns=None)
        self.plan["bags"][0]["storage"] = None
        self.plan["topics"] = []
        self.path.write_text(json.dumps(self.plan))
        result = play_cmd.runtime_config({}, {"RIG_REPLAY_PLAN": str(self.path)}, self.root)
        self.assertEqual(result[2], str(self.root))
        with self.assertRaisesRegex(SystemExit, "storage"):
            self.build()

    def test_nonempty_zero_duration_has_one_nanosecond_logical_window(self):
        self.plan["bags"] = [self.bags[0]]
        self.plan["timeline"]["duration_ns"] = 1
        path = self.root / "bags/a/exact/metadata.yaml"
        metadata = yaml.safe_load(path.read_text())
        metadata["rosbag2_bagfile_information"]["duration"]["nanoseconds"] = 0
        path.write_text(yaml.safe_dump(metadata))
        self.assertEqual(self.build()[5]["duration_ns"], 1)
        self.assertEqual(yaml.safe_load(path.read_text())["rosbag2_bagfile_information"]["duration"]["nanoseconds"], 0)

    def test_sensor_timing_requires_proxy_and_never_changes_native_order(self):
        config = {"play": {"time_source": "sensor", "sensor_timestamp_field": "header.stamp",
                           "tf_lead_time_ms": 25}}
        with self.assertRaisesRegex(SystemExit, "offline bag proxy"):
            self.build(config)
        self.plan["bags"] = [self.bags[0]]
        self.plan["timeline"]["duration_ns"] = 3 * 10**9
        self.plan["proxy"] = {"export_id": "a" * 64, "time_source": "sensor", "deduplication": "conservative"}
        args = self.build(config)[3]
        self.assertEqual(args[args.index("--message-order") + 1], "received")
        for change in ({"time_source": "invalid"}, {"timestamp_fallback": "silent"},
                       {"sensor_timestamp_field": ""}, {"dedup_window_ms": -1},
                       *({"tf_lead_time_ms": value} for value in
                         (-1, True, "100", None, float("nan"), float("inf")))):
            with self.assertRaises(SystemExit):
                self.build({"play": {**config["play"], **change}})
        with self.assertRaisesRegex(SystemExit, "disagrees"):
            self.build({"play": {"time_source": "sent"}})

    def test_proxy_schema_is_strict_before_playback_or_image_inventory(self):
        valid = {"export_id": "a" * 64, "time_source": "sensor", "deduplication": "conservative"}
        self.plan["bags"] = [self.bags[0]]
        bad = [None, False, True, "sensor", [], {}, {"export_id": "a" * 64},
               {**valid, "extra": True}, {**valid, "export_id": "a" * 63},
               {**valid, "export_id": True}, {**valid, "export_id": "G" * 64},
               {**valid, "time_source": []}, {**valid, "time_source": "auto"},
               {**valid, "deduplication": False}, {**valid, "deduplication": "none"}]
        for proxy in bad:
            for inspect in (False, True):
                with self.subTest(proxy=proxy, inspect=inspect), self.assertRaisesRegex(SystemExit, "proxy"):
                    play_cmd.apply_plan({}, {"RIG_REPLAY_PLAN": "/plan.json"},
                        lambda _: json.dumps({**self.plan, "proxy": proxy}), inspect=inspect)
        for plan in ({**self.plan, "bags": self.bags},
                     {key: value for key, value in {**self.plan, "version": 1,
                        "bag": {"logger": "a", "session": "exact"}}.items() if key not in ("bags", "timeline")}):
            with self.assertRaisesRegex(SystemExit, "one combined bag"):
                play_cmd.apply_plan({}, {"RIG_REPLAY_PLAN": "/plan.json"},
                                    lambda _: json.dumps({**plan, "proxy": valid}))

    def test_helper_merge_orders_staggered_inputs_and_preserves_duplicates(self):
        a = Reader([("/x", b"first", 100, 90), ("/x", b"duplicate", 103, 100)])
        b = Reader([("/x", b"middle", 102, 95), ("/x", b"duplicate", 103, 100), ("/x", b"last", 104, 101)])
        records = list(bag_inputs.merged_records([a, b]))
        self.assertEqual([record[2] for record in records], [100, 102, 103, 103, 104])
        self.assertEqual(sum(record[1] == b"duplicate" for record in records), 2)
        last = latch_restore.last_before((r[:3] for r in records), 104)
        self.assertEqual(last, {"/x": (103, b"duplicate")})
        with self.assertRaisesRegex(RuntimeError, "out of received"):
            list(bag_inputs.merged_records([Reader([("/x", b"x", 3, 0), ("/x", b"y", 2, 0)])]))

    def test_latch_requires_consistent_durability_across_inputs(self):
        def doc(durability):
            return {"rosbag2_bagfile_information": {"topics_with_message_count": [{"topic_metadata": {
                "name": "/latched", "type": "std_msgs/msg/String", "offered_qos_profiles":
                [{"durability": durability, "reliability": "reliable"}]}}]}}
        merged = latch_restore.merged_metadata([doc("transient_local"), doc("volatile")])
        self.assertEqual(latch_restore.latched_topics(merged), [])
        merged = latch_restore.merged_metadata([doc("transient_local"), doc("transient_local")])
        self.assertEqual([row["name"] for row in latch_restore.latched_topics(merged)], ["/latched"])

    def test_inspector_mounts_selection_root_and_retains_global_window(self):
        request = dict(version=2, source=str(self.root), bags=self.bags, start_ns=100 * 10**9,
                       from_s=2, to_s=4, services=["/control"], source_mode="service")
        inspect_calls.validate_request(request)
        self.path.write_text(json.dumps(request))
        env = {"BAG_PLAYER_IMAGE": "test:image", "RIG_TARGET_ARCH": "arm64",
               "RIG_RUNTIME_IMAGE_PINS": json.dumps({"version": 1, "images": {"test:image": "sha256:" + "a" * 64}})}
        command = inspect_calls_host.command(self.path, env, "multi-test")
        self.assertIn(f"type=bind,src={self.root},dst=/bag,readonly", command)
        self.assertEqual(sum("dst=/bag," in arg for arg in command), 1)
        self.assertIn("--network=none", command)

    def test_inspector_counts_duplicate_request_identities_across_input_bags(self):
        request = dict(version=2, source=str(self.root), bags=self.bags, start_ns=100 * 10**9,
                       from_s=2, to_s=4, services=["/control"], source_mode="service")
        event = types.SimpleNamespace(info=types.SimpleNamespace(client_gid=[1] * 16,
                                       sequence_number=7, event_type=1), request=[True])
        modules = {"rclpy.serialization": types.SimpleNamespace(deserialize_message=lambda *_: event),
                   "rclpy.type_support": types.SimpleNamespace(check_for_type_support=lambda _: None),
                   "rosidl_runtime_py.utilities": types.SimpleNamespace(get_service=lambda _: types.SimpleNamespace(Event=object)),
                   "rosbag2_py": types.SimpleNamespace(StorageFilter=lambda **kw: kw)}
        readers = [Reader([("/control/_service_event", b"early", 101 * 10**9, 0),
                           ("/control/_service_event", b"duplicate", 103 * 10**9, 0)]),
                   Reader([("/control/_service_event", b"duplicate", 103 * 10**9, 0),
                           ("/control/_service_event", b"late", 104 * 10**9, 0)])]
        metadata = [types.SimpleNamespace(starting_time=types.SimpleNamespace(nanoseconds=100 * 10**9)),
                    types.SimpleNamespace(starting_time=types.SimpleNamespace(nanoseconds=101 * 10**9))]
        with patch.dict(sys.modules, modules), patch.object(inspect_calls, "metadata_union", return_value=(
                metadata, {"/control/_service_event": ("std_srvs/srv/SetBool_Event", "cdr")})), \
                patch.object(inspect_calls, "open_reader", side_effect=readers):
            result = inspect_calls.inspect(request)["services"]["/control"]
        self.assertEqual((result["request_count"], result["duplicate_count"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
