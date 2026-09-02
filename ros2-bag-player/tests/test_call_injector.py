"""The call injector's pure core (tools/call_injector.py) against the frozen script schema
(rig-replay-calls-handoff §1.2): strict schema-v1 parsing (unknown keys fail loudly on BOTH
sides), t-sorting (authors may append out of order; equal stamps keep author order), per-call
timeout defaulting, and the results contract — every appended entry is self-contained YAML, so
the file stays parseable through a mid-timeline SIGTERM, and `response` is capped at 4 KB with
`truncated: true` degrading it to a clipped string (never a half-open YAML block). rig v0.2.37
shallow-validates the same schema (schema/t) — a change that breaks these tests is a contract
renegotiation, not a refactor.
v1.12.0 adds the window half (rig-replay-window-handoff §1.3): the ONE zero (`t` = /clock −
bag start under sim time, from + elapsed under wall time — never the first sample), the window
as a FILTER (t < from skipped, t >= to never reached, t == from IN), and the ONE leading
`# window:` comment line results.yaml carries before its first entry.
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


# --- the window filter + the one zero (rig-replay-window-handoff §1.3) --------------------------

def _calls(*ts):
    return _load(_script([{**CALL, "t": t} for t in ts]))


def test_window_filters_before_after_and_keeps_t_equal_from():
    calls = _calls(2, 8, 30, 32, 38, 44)
    inside, before, after = call_injector.apply_window(calls, 30.0, 40.0)
    assert [c["t"] for c in inside] == [30, 32, 38]             # t == from fires at window start
    assert [c["t"] for c in before] == [2, 8]
    assert [c["t"] for c in after] == [44]                      # t >= to is never reached
    inside, before, after = call_injector.apply_window(calls, 0.0, None)   # unwindowed: all fire
    assert len(inside) == 6 and not before and not after
    inside, _, after = call_injector.apply_window(calls, 0.0, 44.0)        # to is EXCLUSIVE
    assert [c["t"] for c in inside] == [2, 8, 30, 32, 38] and [c["t"] for c in after] == [44]
    assert call_injector.apply_window([], 30.0, 40.0) == ([], [], [])


def test_window_comment_line_names_the_skipped_calls():
    calls = _calls(2, 8, 30, 32, 38, 44)
    _, before, after = call_injector.apply_window(calls, 30.0, 40.0)
    line = call_injector.window_comment(30.0, 40.0, before, after)
    assert line == "# window: from=30 to=40; skipped: t=2 t=8 (before), t=44 (after)\n"
    assert call_injector.window_comment(0.0, None, [], []) \
        == "# window: from=0 to=end; skipped: none\n"
    assert call_injector.window_comment(30.0, None, before, []) \
        == "# window: from=30 to=end; skipped: t=2 t=8 (before)\n"
    # the line is a YAML comment: prepended to appended entries, the file still parses as a list
    text = line + call_injector.render_result(30.0, "/s", True, 0.001, response={"i": 1})
    assert [e["t"] for e in yaml.safe_load(text)] == [30.0]
    assert yaml.safe_load(line) is None                         # comment-only file = nothing fired


def test_the_one_zero_never_shifts_with_the_window():
    bag_start = 1788361659.604888347
    # sim time: the first /clock sample under --start-offset 30 is ≈ bag_start + 30 -> t ≈ 30,
    # NOT 0 (the pre-v1.12.0 bug: pinning t=0 at that sample fired everything 30 s late)
    assert abs(call_injector.sim_now(bag_start + 30.0, bag_start) - 30.0) < 1e-6
    assert abs(call_injector.sim_now(bag_start + 47.25, bag_start) - 47.25) < 1e-6
    assert call_injector.sim_now(bag_start, bag_start) == 0.0   # unwindowed: first sample ≈ 0
    # wall time: from + elapsed (approximate by construction)
    assert call_injector.wall_now(2.5, 30.0) == 32.5
    assert call_injector.wall_now(2.5, 0.0) == 2.5              # unwindowed: v1.10.x's elapsed


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
