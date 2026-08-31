"""The call injector's pure core (tools/call_injector.py) against the frozen script schema
(rig-replay-calls-handoff §1.2): strict schema-v1 parsing (unknown keys fail loudly on BOTH
sides), t-sorting (authors may append out of order; equal stamps keep author order), per-call
timeout defaulting, and the results contract — every appended entry is self-contained YAML, so
the file stays parseable through a mid-timeline SIGTERM, and `response` is capped at 4 KB with
`truncated: true` degrading it to a clipped string (never a half-open YAML block). rig v0.2.37
shallow-validates the same schema (schema/t) — a change that breaks these tests is a contract
renegotiation, not a refactor.
Run: `python3 tests/test_call_injector.py` (no ROS — the rclpy shell imports lazily)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import yaml

import call_injector


def _load(doc):
    return call_injector.load_script(doc)


def _script(calls, **top):
    return {"schema": 1, "calls": calls, **top}


def _expect_exit(needle, doc):
    try:
        _load(doc)
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


CALL = {"t": 12.5, "service": "/planner/set_mode", "type": "my_msgs/srv/SetMode",
        "request": {"mode": "AUTO"}}


# --- schema v1 strict parse ----------------------------------------------------------------------

def test_the_handoff_example_parses_and_fills_timeouts():
    calls = _load(_script([CALL, {**CALL, "t": 47.0, "timeout_s": 10}], timeout_s=5))
    assert [c["t"] for c in calls] == [12.5, 47.0]
    assert [c["timeout_s"] for c in calls] == [5.0, 10.0]   # script default vs per-call override
    assert calls[0]["service"] == "/planner/set_mode"
    assert calls[0]["request"] == {"mode": "AUTO"}


def test_calls_sort_by_t_stably():
    a, b, c = {**CALL, "t": 9.0}, {**CALL, "t": 3.0, "request": {"mode": "X"}}, \
        {**CALL, "t": 3.0, "request": {"mode": "Y"}}
    calls = _load(_script([a, b, c]))
    assert [x["t"] for x in calls] == [3.0, 3.0, 9.0]
    assert [x["request"]["mode"] for x in calls] == ["X", "Y", "AUTO"]   # author order at equal t


def test_default_timeout_is_5_and_request_defaults_empty():
    calls = _load(_script([{"t": 0, "service": "/s", "type": "p/srv/T"}]))
    assert calls[0]["timeout_s"] == 5.0 and calls[0]["request"] == {}
    assert calls[0]["t"] == 0.0                              # t: 0 is legal (fire at play start)


def test_empty_calls_list_is_a_finished_timeline_not_an_error():
    assert _load(_script([])) == []


def test_unknown_keys_fail_loudly_both_levels():
    _expect_exit("unknown script key", _script([CALL], timeout=5))
    _expect_exit("unknown calls[0] key", _script([{**CALL, "when": 1}]))


def test_schema_gate():
    _expect_exit("schema must be 1", _script([CALL]) | {"schema": 2})
    _expect_exit("schema must be 1", {"calls": [CALL]})
    _expect_exit("YAML mapping", ["not", "a", "mapping"])
    _expect_exit("list of call entries", _script("not a list"))


def test_call_field_validation():
    _expect_exit("calls[0].t must be a number", _script([{**CALL, "t": "noon"}]))
    _expect_exit("calls[0].t must be a number", _script([{k: v for k, v in CALL.items()
                                                         if k != "t"}]))
    _expect_exit("must be >= 0", _script([{**CALL, "t": -1}]))
    _expect_exit("absolute service name", _script([{**CALL, "service": "set_mode"}]))
    _expect_exit("absolute service name", _script([{**CALL, "service": None}]))
    _expect_exit("pkg/srv/Name", _script([{**CALL, "type": "my_msgs/msg/Mode"}]))
    _expect_exit("pkg/srv/Name", _script([{**CALL, "type": "SetMode"}]))
    _expect_exit("request must be a mapping", _script([{**CALL, "request": ["AUTO"]}]))
    _expect_exit("timeout_s must be > 0", _script([{**CALL, "timeout_s": 0}]))
    _expect_exit("timeout_s must be > 0", _script([CALL], timeout_s=-1))


# --- the results contract ------------------------------------------------------------------------

def test_ok_result_roundtrips_through_yaml():
    text = call_injector.render_result(12.5, "/planner/set_mode", True, 0.03456,
                                       response={"success": True, "message": "ok"})
    [entry] = yaml.safe_load(text)
    assert entry == {"t": 12.5, "service": "/planner/set_mode", "ok": True,
                     "latency_s": 0.0346, "response": {"success": True, "message": "ok"}}


def test_error_result_has_error_not_response():
    [entry] = yaml.safe_load(call_injector.render_result(
        47.0, "/planner/set_mode", False, 10.0, error="timeout after 10s"))
    assert entry["ok"] is False and entry["error"] == "timeout after 10s"
    assert "response" not in entry and "truncated" not in entry


def test_appended_entries_are_one_parseable_list():
    text = "".join(call_injector.render_result(float(i), "/s", True, 0.001, response={"i": i})
                   for i in range(5))
    assert [e["response"]["i"] for e in yaml.safe_load(text)] == list(range(5))


def test_response_cap_truncates_to_a_parseable_string():
    big = {"data": "x" * 10000}
    text = call_injector.render_result(1.0, "/s", True, 0.1, response=big)
    [entry] = yaml.safe_load(text)                           # still parseable — the point
    assert entry["truncated"] is True
    assert isinstance(entry["response"], str)                # degraded to a clipped YAML string
    assert len(entry["response"].encode()) <= call_injector.RESPONSE_CAP
    small = call_injector.render_result(1.0, "/s", True, 0.1, response={"data": "x"})
    assert "truncated" not in yaml.safe_load(small)[0]


def test_plain_strips_message_containers():
    import array
    from collections import OrderedDict
    obj = OrderedDict(a=array.array("B", [1, 2]), b=(b"\x03\x04",), c=[OrderedDict(d=1)])
    assert call_injector.plain(obj) == {"a": [1, 2], "b": [[3, 4]], "c": [{"d": 1}]}
    # yaml.safe_dump accepts the result (would refuse OrderedDict/array outright)
    yaml.safe_dump(call_injector.plain(obj))


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
