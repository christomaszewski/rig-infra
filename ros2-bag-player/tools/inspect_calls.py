#!/usr/bin/env python3
"""Read-only v1 recorded-request inspection; pure counting core plus a lazy ROS reader.

Counts use bag RECEIVE timestamps and the selected introspection origin, like ros2 bag play.
Repeated identities are reported, not removed: rosbag2 replays repeated selected-origin events.
No node, publisher, service client or output file is created by this tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sys
import time

EVENT_SUFFIX = "/_service_event"
MAX_REQUEST_BYTES = 256 * 1024
SCAN_TIMEOUT_S = 75
REQUEST_SENT, REQUEST_RECEIVED, RESPONSE_SENT, RESPONSE_RECEIVED = range(4)


def validate_request(value):
    fields = {"version", "session", "start_ns", "from_s", "to_s", "services", "source_mode"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("inspection request must contain exactly the v1 request fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("inspection request version must be 1")
    if not isinstance(value["session"], str) or not Path(value["session"]).is_absolute():
        raise ValueError("inspection session must be an absolute exact bag directory")
    if type(value["start_ns"]) is not int or not 0 <= value["start_ns"] < 2**63:
        raise ValueError("inspection start_ns must be a nonnegative int64")
    for name in ("from_s", "to_s"):
        offset = value[name]
        if name == "to_s" and offset is None:
            continue
        if (type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0
                or value["start_ns"] + offset * 1e9 >= 2**63):
            raise ValueError(f"inspection {name} must be a finite nonnegative offset")
    if value["to_s"] is not None and value["to_s"] <= value["from_s"]:
        raise ValueError("inspection to_s must be greater than from_s")
    if value["to_s"] is not None and int(value["to_s"] * 1e9) <= int(value["from_s"] * 1e9):
        raise ValueError("inspection window is empty at nanosecond precision")
    services = value["services"]
    if (not isinstance(services, list) or len(services) > 4096
            or any(not isinstance(s, str) or not re.fullmatch(r"/(?:[A-Za-z_][A-Za-z_0-9]*/)*[A-Za-z_][A-Za-z_0-9]*", s)
                   for s in services) or len(set(services)) != len(services)):
        raise ValueError("inspection services must be unique absolute ROS service names")
    if value["source_mode"] not in ("service", "client"):
        raise ValueError("inspection source_mode must be service or client")
    return value


def load_request(path):
    with open(path, "rb") as stream:
        data = stream.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError("inspection request exceeds 256 KiB")
    return validate_request(json.loads(data))


@dataclass
class ServiceSummary:
    type: str | None = None
    failure: str | None = None
    request_count: int = 0
    other_origin_request_count: int = 0
    duplicate_count: int = 0
    metadata_count: int = 0
    response_count: int = 0
    seen: set = field(default_factory=set)

    def observe(self, event_type, has_request, identity, source_mode):
        if event_type not in (REQUEST_SENT, REQUEST_RECEIVED, RESPONSE_SENT, RESPONSE_RECEIVED):
            raise ValueError(f"unknown service event type: {event_type}")
        if event_type in (RESPONSE_SENT, RESPONSE_RECEIVED):
            self.response_count += 1
        elif not has_request:
            self.metadata_count += 1
        elif event_type == (REQUEST_RECEIVED if source_mode == "service" else REQUEST_SENT):
            self.request_count += 1
            if identity in self.seen:
                self.duplicate_count += 1
            self.seen.add(identity)
        else:
            self.other_origin_request_count += 1

    def result(self):
        if self.failure:
            status, reason = "unsupported_type", self.failure
        elif self.type is None:
            status, reason = "missing", "No service-event topic is recorded for this service."
        elif self.request_count:
            status, reason = "usable", "Selected-origin request contents are recorded in the window."
            if self.duplicate_count:
                reason += " Repeated selected-origin identities will also be replayed."
        elif self.other_origin_request_count:
            status, reason = "other_origin", "Request contents exist only for the other introspection origin in the window."
        elif self.metadata_count:
            status, reason = "metadata_only", "Request events in the window have no request contents."
        elif self.response_count:
            status, reason = "response_only", "Only response events are recorded in the window."
        else:
            status, reason = "empty_window", "No service events are recorded in the selected window."
        return {"status": status, "request_count": self.request_count,
                "other_origin_request_count": self.other_origin_request_count,
                "duplicate_count": self.duplicate_count, "type": self.type, "reason": reason}


def inspect(request, session=None, *, budget_s=SCAN_TIMEOUT_S):
    request = validate_request(request)
    deadline = time.monotonic() + budget_s
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_service
    import rosbag2_py

    session = str(session or request["session"])
    metadata = rosbag2_py.Info().read_metadata(session, "")
    if metadata.starting_time.nanoseconds != request["start_ns"]:
        raise ValueError("bag starting_time changed after replay planning")
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=session, storage_id=""),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    available = {}
    for topic in reader.get_all_topics_and_types():
        if topic.name in available and available[topic.name] != topic.type:
            raise ValueError(f"conflicting recorded types for {topic.name}")
        available[topic.name] = topic.type
    summaries = {name: ServiceSummary() for name in request["services"]}
    supported = {}
    for name, summary in summaries.items():
        topic = name + EVENT_SUFFIX
        event_type = available.get(topic)
        if event_type is None:
            continue
        summary.type = event_type.removesuffix("_Event")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*/srv/[A-Za-z_][A-Za-z_0-9]*_Event", event_type):
            summary.failure = "Recorded topic does not have a ROS service-event type."
            continue
        try:
            service = get_service(summary.type)
            event_class = service.Event
            # Resolve typesupport now so an absent fleet overlay is a typed omission.
            from rclpy.type_support import check_for_type_support
            check_for_type_support(event_class)
        except (ImportError, AttributeError, ValueError, RuntimeError) as exc:
            summary.failure = f"Service-event type is unavailable in the selected player image: {exc}"
            continue
        supported[topic] = (summary, event_class)
    if supported:
        if not reader.set_read_order(rosbag2_py.ReadOrder(
                sort_by=rosbag2_py.ReadOrderSortBy.ReceivedTimestamp, reverse=False)):
            raise RuntimeError("storage cannot inspect service events in received timestamp order")
        reader.set_filter(rosbag2_py.StorageFilter(topics=list(supported),
                                                 services_events=list(supported)))
        lower = request["start_ns"] + int(request["from_s"] * 1e9)
        upper = (request["start_ns"] + int(request["to_s"] * 1e9)
                 if request["to_s"] is not None else None)
        reader.seek(lower)
        while reader.has_next():
            if time.monotonic() >= deadline:
                raise TimeoutError("service-event inspection exceeded its 75-second scan budget")
            topic, payload, received_ns, _sent_ns = reader.read_next_ext()
            if upper is not None and received_ns >= upper:
                break
            # rosbag2 StorageFilter.topics alone can also return service-event topics.
            # Always enforce the exact selection independently of storage plugin filtering.
            if topic not in supported:
                continue
            if received_ns < lower:
                continue
            summary, event_class = supported[topic]
            # Corrupt bytes are a technical failure, never an empty/unsupported result.
            event = deserialize_message(payload, event_class)
            identity = (bytes(event.info.client_gid), event.info.sequence_number)
            summary.observe(event.info.event_type, bool(event.request), identity, request["source_mode"])
    if time.monotonic() >= deadline:
        raise TimeoutError("service-event inspection exceeded its 75-second scan budget")
    return {"version": 1, "source_mode": request["source_mode"],
            "services": {name: summary.result() for name, summary in summaries.items()}}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise ValueError("usage: inspect_calls.py REQUEST_JSON EXACT_CONTAINER_BAG_DIRECTORY")
    request = load_request(args[0])
    # Storage plugins may log through native stdout. Reserve stdout for exactly one JSON result.
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        result = inspect(request, args[1])
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write(f"replay-inspect-calls: {exc}\n")
        sys.exit(2)
