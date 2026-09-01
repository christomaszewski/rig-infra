#!/usr/bin/env python3
"""ros2-bag-player export-calls: read a source session's ``…/_service_event`` topics and emit a
schema-v1 call-script YAML on STDOUT (the frozen contract — rig-replay-calls-handoff.md §1.3).
The call injector's bootstrap: export, redirect to a file, edit (retime one call, drop one,
inject a new one), replay with ``calls:`` / ``RIG_REPLAY_CALLS``. An exported-but-unedited
script, injected, reproduces verbatim playback — same calls, same order, times within pacing
tolerance — which is what lets an operator edit ONE call without perturbing the rest.

Requests are reconstructed from SERVER-side events by default (``CALL_EXPORT_SOURCE`` =
``service`` | ``client`` — the launcher wires ``play.services_source`` through; servers under
test adopted introspection, their callers may not have): ``service`` exports REQUEST_RECEIVED
events, ``client`` REQUEST_SENT. ``t`` = the event's bag-receive stamp minus the session's
metadata ``starting_time`` — the same zero rosbag2's ``--clock`` starts publishing from, so an
exported ``t`` lands where the recording put it. Events without request CONTENTS (metadata-only
introspection) export as YAML comments naming the service — visible, never silently dropped.

Split like the siblings: a pure core (events -> script text, unit-tested in ``../tests/``
without ROS) and a rosbag2_py shell. Runs in a one-shot bag-player container wired by
``ros2-bag-player-up <config> export-calls`` (the tool is mounted beside play.sh; the session
path argv comes from play_cmd's host-side resolution). rig adds no verb — ROS stays on this
side of the line.
"""
from __future__ import annotations

import os
import sys

import yaml

EVENT_SUFFIX = "/_service_event"
DEFAULT_TIMEOUT_S = 5


def plain(obj):
    """Recursively strip ROS-message containers (OrderedDict, array.array, numpy arrays/scalars,
    bytes) down to what yaml.safe_dump accepts. (Deliberately duplicated from call_injector.py —
    each tool is mounted alone into its container and stays self-contained.)"""
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if isinstance(obj, bytes):
        return list(obj)
    if hasattr(obj, "tolist"):         # numpy arrays/scalars, array.array
        return plain(obj.tolist())
    return obj


def script_text(calls: list[tuple], missing: dict, source_mode: str, session: str) -> str:
    """(t, service, srv_type, request-dict) tuples + {service: contents-less count} -> the
    schema-v1 script. Calls sort by t (stable); one flow-style line per call — the shape the
    handoff's example shows, one line per edit. The output parses with the injector's own
    load_script (round-trip by construction) and with yaml.safe_load."""
    if source_mode not in ("service", "client"):
        raise SystemExit(f"export_calls: requests source must be `service` or `client` "
                         f"(got {source_mode!r})")
    out = [
        "# exported by ros2-bag-player export-calls — call-script schema v1 "
        "(rig-replay-calls-handoff §1.2)",
        f"# source: {session} (requests from {source_mode}-side events)",
        "# `t` = seconds from play start on the replay clock; edit/retime/inject, then replay "
        "with `calls:` / RIG_REPLAY_CALLS",
        "schema: 1",
        f"timeout_s: {DEFAULT_TIMEOUT_S}",
    ]
    ordered = sorted(calls, key=lambda c: c[0])
    if ordered:
        out.append("calls:")
        for t, service, srv_type, request in ordered:
            line = yaml.safe_dump({"t": round(float(t), 3), "service": service,
                                   "type": srv_type, "request": request},
                                  default_flow_style=True, sort_keys=False,
                                  width=1_000_000).strip()
            out.append(f"- {line}")
    else:
        out.append("calls: []")
    for service in sorted(missing):
        out.append(f"# {service}: {missing[service]} {source_mode}-side request event(s) "
                   "without request contents (metadata-only introspection) — not exported; "
                   "re-record with CONTENTS-level introspection to script this service")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------
# Shell — rosbag2 reader (in-container; rosbag2_py + the fleet's interface packages).
# ---------------------------------------------------------------------------------------------

def read_events(session: str, source_mode: str) -> tuple[list[tuple], dict]:
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.convert import message_to_ordereddict
    from rosidl_runtime_py.utilities import get_service
    from service_msgs.msg import ServiceEventInfo
    import rosbag2_py

    want = ServiceEventInfo.REQUEST_RECEIVED if source_mode == "service" \
        else ServiceEventInfo.REQUEST_SENT

    # The session's zero: metadata starting_time — the stamp rosbag2's /clock starts from.
    # storage_id "" = autodetect (lyrical reads mcap and db3 alike; the play side of the same
    # session auto-detects too).
    start_ns = rosbag2_py.Info().read_metadata(session, "").starting_time.nanoseconds

    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=session, storage_id=""),
                rosbag2_py.ConverterOptions(input_serialization_format="cdr",
                                            output_serialization_format="cdr"))
    types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    calls: list[tuple] = []
    missing: dict = {}
    while reader.has_next():
        # read_next_ext: (topic, data, recv_ns, send_ns) — lyrical deprecation-warns the old
        # 3-tuple read_next. We keep the RECEIVE stamp on purpose: it is what read_next returned
        # and what play paces by, so the exported timeline stays byte-identical across the API
        # move. (send_ns — when the introspecting node published the event — is available here
        # if a future schema wants call-origin time; pre-lyrical readers only have read_next.)
        topic, data, t_ns, _send_ns = reader.read_next_ext()
        if not topic.endswith(EVENT_SUFFIX):
            continue
        typ = types[topic]                       # pkg/srv/Name_Event
        if not typ.endswith("_Event"):
            sys.stderr.write(f"export_calls: {topic} has non-event type {typ!r} — skipped\n")
            continue
        srv_type = typ[: -len("_Event")]
        event = deserialize_message(data, get_service(srv_type).Event)
        if event.info.event_type != want:
            continue
        service = topic[: -len(EVENT_SUFFIX)]
        t = (t_ns - start_ns) / 1e9
        if len(event.request):
            calls.append((t, service, srv_type,
                          plain(message_to_ordereddict(event.request[0]))))
        else:
            missing[service] = missing.get(service, 0) + 1
    return calls, missing


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: export_calls.py </replay/bags/<logger>/<session>>\n")
        return 2
    session = sys.argv[1]
    source_mode = os.environ.get("CALL_EXPORT_SOURCE") or "service"
    calls, missing = read_events(session, source_mode)
    sys.stderr.write(f"export_calls: {len(calls)} call(s) exported"
                     + (f", {sum(missing.values())} contents-less event(s) commented"
                        if missing else "") + "\n")
    sys.stdout.write(script_text(calls, missing, source_mode, session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
