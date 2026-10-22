#!/usr/bin/env python3
"""Build-time contract for io.rig.rosbag2.replay=1, without a ROS node or network.

The contract covers the CLI and Python APIs used by the v1 structured player plan,
recorded/scripted service calls, window latch restoration, release gate and reindex.
Fleet-specific message/service types remain the interface overlay's responsibility.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile


PLAY_FLAGS = {"--remap", "--clock", "--start-offset", "--playback-duration", "--start-paused",
              "--publish-service-requests", "--service-requests-source", "--services", "--topics",
              "--exclude-regex", "--rate", "--loop", "--playback-until-nsec"}


def require(condition, description):
    if not condition:
        raise RuntimeError("rosbag2 replay runtime check failed: " + description)


def require_flags(help_text, flags):
    observed = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", help_text))
    missing = sorted(set(flags) - observed)
    if missing:
        raise RuntimeError("rosbag2 replay runtime lacks required CLI flags: " + ", ".join(missing))


def cli_help(verb, flags):
    result = subprocess.run(["ros2", "bag", verb, "--help"], check=True, capture_output=True,
                            text=True, timeout=30)
    require_flags(result.stdout, flags)
    return result.stdout


def check_runtime():
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.serialization import deserialize_message, serialize_message
    from rclpy.type_support import check_for_type_support
    import rosbag2_py
    from rosbag2_interfaces.srv import IsPaused, Resume
    from rosgraph_msgs.msg import Clock
    from rosidl_runtime_py.convert import message_to_ordereddict
    from rosidl_runtime_py.set_message import set_message_fields
    from rosidl_runtime_py.utilities import get_message, get_service
    from service_msgs.msg import ServiceEventInfo
    import yaml

    help_text = cli_help("play", PLAY_FLAGS)
    if "client_introspection" not in help_text or "service_introspection" not in help_text:
        raise RuntimeError("rosbag2 replay runtime lacks both service request source modes")
    cli_help("reindex", {"--storage"})
    for name in ("init", "create_node", "spin_once", "try_shutdown"):
        require(callable(getattr(rclpy, name)), "rclpy." + name)
    for name in ("create_publisher", "create_subscription", "create_client", "destroy_node"):
        require(callable(getattr(Node, name)), "rclpy.node.Node." + name)
    require(issubclass(ExternalShutdownException, Exception), "ExternalShutdownException")
    QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST, reliability=ReliabilityPolicy.RELIABLE,
               durability=DurabilityPolicy.TRANSIENT_LOCAL)
    Clock()
    IsPaused.Request()
    require(isinstance(IsPaused.Response().paused, bool), "IsPaused.Response.paused")
    Resume.Request()

    # Generated request/event types and Python typesupport are needed by both call paths.
    service = get_service("std_srvs/srv/SetBool")
    request = service.Request()
    set_message_fields(request, {"data": True})
    require(message_to_ordereddict(request)["data"] is True, "request field conversion")
    event = service.Event()
    check_for_type_support(service.Event)
    event.info.event_type = ServiceEventInfo.REQUEST_RECEIVED
    event.request = [request]
    require(ServiceEventInfo.REQUEST_RECEIVED != ServiceEventInfo.REQUEST_SENT, "service event constants")
    require((ServiceEventInfo.REQUEST_SENT, ServiceEventInfo.REQUEST_RECEIVED,
             ServiceEventInfo.RESPONSE_SENT, ServiceEventInfo.RESPONSE_RECEIVED) == (0, 1, 2, 3),
            "service inspection event classifications")
    require(len(bytes(event.info.client_gid)) == 16 and isinstance(event.info.sequence_number, int),
            "service inspection request identity")
    require(deserialize_message(serialize_message(event), service.Event).request[0].data,
            "service event request serialization")
    require(rosbag2_py.StorageFilter(services_events=["/runtime_check/_service_event"]).services_events
            == ["/runtime_check/_service_event"], "service-event storage filter")

    message_type = get_message("std_msgs/msg/String")
    message = message_type(data="rig replay runtime v1")
    payload = serialize_message(message)
    with tempfile.TemporaryDirectory(prefix="rig-replay-runtime-") as temporary:
        for storage in ("mcap", "sqlite3"):
            path = Path(temporary) / storage
            writer = rosbag2_py.SequentialWriter()
            writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=storage),
                        rosbag2_py.ConverterOptions("cdr", "cdr"))
            writer.create_topic(rosbag2_py.TopicMetadata(id=0, name="/runtime_check",
                type="std_msgs/msg/String", serialization_format="cdr"))
            for stamp in (1_000_000_000, 2_000_000_000):
                writer.write("/runtime_check", payload, stamp)
            writer.close()
            reader = rosbag2_py.SequentialReader()
            reader.open(rosbag2_py.StorageOptions(uri=str(path), storage_id="",
                                                end_time_ns=1_500_000_000),
                        rosbag2_py.ConverterOptions("cdr", "cdr"))
            reader.set_filter(rosbag2_py.StorageFilter(topics=["/runtime_check"]))
            require(reader.set_read_order(rosbag2_py.ReadOrder(
                sort_by=rosbag2_py.ReadOrderSortBy.ReceivedTimestamp, reverse=False)),
                storage + " received-timestamp reader order")
            reader.seek(1_000_000_000)
            require(reader.get_all_topics_and_types()[0].type == "std_msgs/msg/String", "topic type discovery")
            require(reader.has_next(), "bounded reader first message")
            topic, data, stamp, _ = reader.read_next_ext()
            require(topic == "/runtime_check" and stamp == 1_000_000_000, "extended reader timestamps")
            require(deserialize_message(data, message_type) == message, "message deserialization")
            # Storage plugins may treat end_time_ns as a hint; latch_restore also enforces
            # its cutoff on each received timestamp, so storage-side enforcement is not v1.
            del reader
            info = rosbag2_py.Info().read_metadata(str(path), "")
            require(info.message_count == 2 and info.starting_time.nanoseconds == 1_000_000_000,
                    "bag metadata timestamps/counts")
            require(yaml.safe_load((path / "metadata.yaml").read_text())["rosbag2_bagfile_information"],
                    "bag metadata YAML")
            (path / "metadata.yaml").unlink()
            subprocess.run(["ros2", "bag", "reindex", "--storage", storage, str(path)],
                           check=True, capture_output=True, text=True, timeout=30)
            require(rosbag2_py.Info().read_metadata(str(path), "").message_count == 2,
                    storage + " reindexed message count")
    return {"io.rig.rosbag2.replay": "1", "storage": ["mcap", "sqlite3"]}


if __name__ == "__main__":
    print(json.dumps(check_runtime(), sort_keys=True))
