"""The export-calls pure core (tools/export_calls.py) against the frozen bootstrap contract
(rig-replay-calls-handoff §1.3): events -> schema-v1 script text sorted by t, one flow-style
line per call, CONTENTS-less events surfaced as YAML comments naming the service (visible,
never silently dropped) — and the round-trip guarantee the workflow stands on: the emitted text
parses with the call injector's own load_script, calls and order intact. Run:
`python3 tests/test_export_calls.py` (no ROS — the rosbag2 shell imports lazily)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import yaml

import call_injector
import export_calls

SESSION = "/replay/bags/bag_logger/bag_logger_20260827T100000Z"
CALLS = [
    (47.0, "/planner/set_mode", "my_msgs/srv/SetMode", {"mode": "RTL"}),
    (12.5, "/planner/set_mode", "my_msgs/srv/SetMode", {"mode": "AUTO"}),
    (30.25, "/lights/set", "std_srvs/srv/SetBool", {"data": True}),
]


def test_script_is_schema_v1_sorted_and_flow_per_line():
    text = export_calls.script_text(CALLS, {}, "service", SESSION)
    doc = yaml.safe_load(text)
    assert doc["schema"] == 1 and doc["timeout_s"] == 5
    assert [c["t"] for c in doc["calls"]] == [12.5, 30.25, 47.0]
    assert doc["calls"][0]["request"] == {"mode": "AUTO"}
    assert doc["calls"][1] == {"t": 30.25, "service": "/lights/set",
                               "type": "std_srvs/srv/SetBool", "request": {"data": True}}
    # one editable flow line per call — the handoff example's shape
    assert sum(1 for line in text.splitlines() if line.startswith("- {")) == 3


def test_round_trip_with_the_injector_by_construction():
    text = export_calls.script_text(CALLS, {}, "service", SESSION)
    loaded = call_injector.load_script(yaml.safe_load(text))
    assert [(c["t"], c["service"], c["type"], c["request"]) for c in loaded] == sorted(CALLS)
    assert all(c["timeout_s"] == 5.0 for c in loaded)


def test_contents_less_events_export_as_comments_never_dropped():
    text = export_calls.script_text(CALLS[:1], {"/gps/reset": 3}, "service", SESSION)
    doc = yaml.safe_load(text)
    assert len(doc["calls"]) == 1                            # the comment is NOT a call
    comment = [ln for ln in text.splitlines() if ln.startswith("# /gps/reset")]
    assert comment and "3 service-side request event(s) without request contents" in comment[0]
    assert "metadata-only introspection" in comment[0]


def test_empty_export_is_a_valid_empty_script():
    doc = yaml.safe_load(export_calls.script_text([], {}, "client", SESSION))
    assert doc["calls"] == [] and doc["schema"] == 1
    assert call_injector.load_script(doc) == []


def test_source_mode_is_strict_and_named_in_the_header():
    assert "client-side events" in export_calls.script_text([], {}, "client", SESSION)
    try:
        export_calls.script_text([], {}, "server", SESSION)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "service" in str(exc)


def test_awkward_request_values_survive_the_flow_dump():
    calls = [(0.0, "/s", "p/srv/T", {"msg": "with: colon, and, commas", "n": [1, 2],
                                     "f": 0.125, "none_like": "null"})]
    doc = yaml.safe_load(export_calls.script_text(calls, {}, "service", SESSION))
    assert doc["calls"][0]["request"] == calls[0][3]         # quoting handled by safe_dump


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
