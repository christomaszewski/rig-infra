"""The latch pre-pass's pure core (tools/latch_restore.py) against the frozen window contract
(rig-replay-window-handoff §1.2): the latched topic set from metadata.yaml (every offered
profile transient_local — mixed offers play volatile under rosbag2, so they are not restored
either), the intersection with the MAIN PLAY's selection (allow-list exact; exclude alternation
by FULL match, exactly rosbag2's -x — never a topic the main play would not publish), the recorded-reliability carry-over,
the window cutoff arithmetic, and last-message-per-topic over an ordered stream that stops at the
window. Run: `python3 tests/test_latch_restore.py` (no ROS — the rclpy/rosbag2 shell imports
lazily)."""
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import yaml

import latch_restore


def _profile(durability="volatile", reliability="reliable"):
    return {"history": "keep_last", "depth": 10, "reliability": reliability,
            "durability": durability, "deadline": {"sec": 0, "nsec": 0},
            "lifespan": {"sec": 0, "nsec": 0}, "liveliness": "automatic",
            "liveliness_lease_duration": {"sec": 0, "nsec": 0},
            "avoid_ros_namespace_conventions": False}


def _meta(topics):
    return {"rosbag2_bagfile_information": {
        "version": 9, "storage_identifier": "mcap",
        "duration": {"nanoseconds": 49908202370},
        "starting_time": {"nanoseconds_since_epoch": 1788361659604888347},
        "topics_with_message_count": [
            {"topic_metadata": {"name": n, "type": t, "serialization_format": "cdr",
                                "offered_qos_profiles": profiles},
             "message_count": 1} for n, t, profiles in topics]}}


META = _meta([
    ("/tf_static", "tf2_msgs/msg/TFMessage", [_profile("transient_local")]),
    ("/toy/map", "std_msgs/msg/String", [_profile("transient_local", "best_effort")]),
    ("/toy/tick", "std_msgs/msg/Int32", [_profile()]),
    ("/mixed", "std_msgs/msg/String", [_profile("transient_local"), _profile("volatile")]),
    ("/two_latched", "std_msgs/msg/String", [_profile("transient_local"),
                                             _profile("transient_local")]),
    ("/no_profile", "std_msgs/msg/String", []),
    ("/toy/set_bool/_service_event", "std_srvs/srv/SetBool_Event", [_profile()]),
])


def test_latched_topics_from_metadata():
    got = latch_restore.latched_topics(META)
    assert [t["name"] for t in got] == ["/tf_static", "/toy/map", "/two_latched"]
    assert got[0] == {"name": "/tf_static", "type": "tf2_msgs/msg/TFMessage",
                      "reliability": "reliable"}
    assert got[1]["reliability"] == "best_effort"               # the recorded reliability rides along
    # mixed offers -> rosbag2 plays volatile -> NOT restored; no profile -> volatile by definition
    assert all(t["name"] not in ("/mixed", "/no_profile", "/toy/tick") for t in got)


def test_pre_iron_string_profiles_and_numeric_enums_parse():
    profiles_yaml = yaml.safe_dump([{"durability": 2, "reliability": 1}])
    meta = _meta([("/old", "std_msgs/msg/String", profiles_yaml)])   # one YAML string
    assert latch_restore.latched_topics(meta) == [{"name": "/old", "type": "std_msgs/msg/String",
                                                  "reliability": "reliable"}]


def test_selector_parsing_is_exactly_play_cmds_two_shapes():
    assert latch_restore.parse_selector([]) == (None, None)
    assert latch_restore.parse_selector(["--topics", "/a", "/b"]) == (["/a", "/b"], None)
    assert latch_restore.parse_selector(["--exclude-regex", "(?:/cam/.*)"]) == (None, "(?:/cam/.*)")
    for bad in (["--topics"], ["--exclude-regex"], ["-x", "a"], ["--topics", "/a", "-x", "b"]):
        try:
            latch_restore.parse_selector(bad)
            raise AssertionError(f"expected refusal for {bad}")
        except SystemExit as exc:
            assert "unexpected selector" in str(exc)


def test_intersection_never_exceeds_the_main_plays_selection():
    latched = latch_restore.latched_topics(META)
    # allow mode: exact names only — an allow-list without /tf_static restores nothing there
    assert [t["name"] for t in latch_restore.select(latched, ["/toy/map", "/toy/tick"], None)] \
        == ["/toy/map"]
    assert latch_restore.select(latched, ["/toy/tick"], None) == []
    # exclude mode: the alternation drops by FULL match — exactly rosbag2's -x (pinned live on
    # lyrical: `-x tick` plays /toy/tick, `-x /toy/tick` and `-x .*tick` exclude it)
    assert [t["name"] for t in latch_restore.select(latched, None, "(?:/toy/.*)")] \
        == ["/tf_static", "/two_latched"]
    assert [t["name"] for t in latch_restore.select(latched, None, "(?:tf_static)|(?:map)")] \
        == ["/tf_static", "/toy/map", "/two_latched"]           # partial matches drop NOTHING
    assert [t["name"] for t in latch_restore.select(latched, None, "(?:/tf_static)|(?:.*map)")] \
        == ["/two_latched"]
    # no selector = the main play publishes everything = every latched topic
    assert len(latch_restore.select(latched, None, None)) == 3


def test_window_cutoff_and_last_message_before_it():
    start = 1788361659604888347
    cutoff = latch_restore.window_start_ns(start, 30.0)
    assert cutoff == start + 30_000_000_000
    stream = [("/toy/map", b"v1", start + 1_000_000_000),
              ("/tf_static", b"tf", start + 1_000_000_000),
              ("/toy/map", b"v2", start + 10_000_000_000),
              ("/toy/map", b"v3", start + 35_000_000_000),      # in-window: the main play's
              ("/tf_static", b"late", start + 40_000_000_000)]
    last = latch_restore.last_before(iter(stream), cutoff)
    assert last == {"/toy/map": (start + 10_000_000_000, b"v2"),
                    "/tf_static": (start + 1_000_000_000, b"tf")}
    # a message exactly AT the window start belongs to the main play (--start-offset seeks there)
    assert latch_restore.last_before(iter([("/x", b"at", cutoff)]), cutoff) == {}
    # the stream stops at the first in-window stamp — nothing after it is consumed
    consumed = []

    def gen():
        for m in stream:
            consumed.append(m[1])
            yield m
    latch_restore.last_before(gen(), cutoff)
    assert consumed == [b"v1", b"tf", b"v2", b"v3"]


def test_static_tf_detection_requires_type_and_selected_latched_topic():
    meta = _meta([
        ("/tf_static", "tf2_msgs/msg/TFMessage", [_profile("transient_local")]),
        ("/vehicle/tf_static", "tf2_msgs/msg/TFMessage", [_profile("transient_local")]),
        ("/other/tf_static", "std_msgs/msg/String", [_profile("transient_local")]),
        ("/tf", "tf2_msgs/msg/TFMessage", [_profile("transient_local")]),
        ("/unknown/tf_static", "tf2_msgs/msg/TFMessage", []),
    ])
    latched = latch_restore.latched_topics(meta)
    assert latch_restore.static_tf_names(latched) == {"/tf_static", "/vehicle/tf_static"}
    selected = latch_restore.select(latched, None, "/tf_static")
    assert latch_restore.static_tf_names(selected) == {"/vehicle/tf_static"}
    assert latch_restore.static_tf_names(latch_restore.select(latched, ["/tf"], None)) == set()


def test_static_tf_union_restores_each_child_and_latest_edge_before_window():
    def transform(child, parent, value):
        return SimpleNamespace(child_frame_id=child, header=SimpleNamespace(frame_id=parent),
                               translation=value)

    first = transform("lidar", "base", 1)
    second = transform("camera", "base", 2)
    updated = transform("lidar", "mount", 3)
    future = transform("lidar", "future", 4)
    other_namespace = transform("camera", "other_base", 5)
    first_payload, second_payload, update_payload = [first], [second], [updated]
    stream = [
        ("/tf_static", first_payload, 1),
        ("/map", b"old", 2),
        ("/tf_static", second_payload, 3),
        ("/vehicle/tf_static", [other_namespace], 3),
        ("/map", b"new", 4),
        ("/tf_static", update_payload, 5),
        ("/tf_static", [], 6),  # An empty update does not erase existing static edges.
        ("/tf_static", [future], 7),  # At the cutoff belongs to normal playback.
    ]
    result = latch_restore.last_before(iter(stream), 7, {"/tf_static", "/vehicle/tf_static"},
                                       decode_tf=lambda data: data, encode_tf=tuple)
    assert result == {"/tf_static": (6, (second, updated)),
                      "/vehicle/tf_static": (3, (other_namespace,)), "/map": (4, b"new")}
    # Restoring a union must not mutate any original payload collection.
    assert first_payload == [first] and second_payload == [second] and update_payload == [updated]


def test_static_tf_accumulation_keeps_latest_per_child_without_reading_past_window():
    consumed = []

    def stream():
        for stamp in range(1001):
            consumed.append(stamp)
            yield "/tf_static", [SimpleNamespace(child_frame_id="lidar", value=stamp)], stamp
        raise AssertionError("read past the first in-window message")

    result = latch_restore.last_before(stream(), 1000, {"/tf_static"},
                                       decode_tf=lambda data: data, encode_tf=tuple)
    stamp, transforms = result["/tf_static"]
    assert stamp == 999 and len(transforms) == 1 and transforms[0].value == 999
    assert consumed[-1] == 1000
    assert latch_restore.last_before(iter([]), 10, {"/tf_static"},
                                     decode_tf=lambda data: data, encode_tf=tuple) == {}


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
