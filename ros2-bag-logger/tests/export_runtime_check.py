"""Check ROS/MCAP export selection in the fleet image.

    python3 ros2-bag-logger/tests/export_runtime_check.py --output /tmp/export-check

The output directory must not exist. Requires ROS, mcap, and the provenance
capability. Uses serialized ROS messages and the real converter without starting
ROS nodes or requiring network access.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml


def create_fixture(session: Path) -> set[str]:
    """Write ordinary, hidden, service and action topics with real ROS types."""
    import rosbag2_py
    from action_msgs.msg import GoalStatusArray
    from action_msgs.srv import CancelGoal_Event
    from rclpy.serialization import serialize_message
    from std_msgs.msg import String
    from std_srvs.srv import SetBool_Event

    fixtures = [
        ("/visible", "std_msgs/msg/String", String(data="visible")),
        ("/_hidden", "std_msgs/msg/String", String(data="hidden")),
        ("/set_bool/_service_event", "std_srvs/srv/SetBool_Event", SetBool_Event()),
        ("/move/_action/status", "action_msgs/msg/GoalStatusArray", GoalStatusArray()),
        (
            "/move/_action/cancel_goal/_service_event",
            "action_msgs/srv/CancelGoal_Event",
            CancelGoal_Event(),
        ),
    ]
    session.parent.mkdir(parents=True)
    writer = rosbag2_py.SequentialWriter()
    writer.open(
        rosbag2_py.StorageOptions(uri=str(session), storage_id="mcap"),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    try:
        for index, (topic, type_name, message) in enumerate(fixtures):
            writer.create_topic(
                rosbag2_py.TopicMetadata(
                    id=0, name=topic, type=type_name, serialization_format="cdr"
                )
            )
            timestamp = (index + 1) * 1_000_000_000
            writer.write(topic, serialize_message(message), timestamp)
    finally:
        writer.close()
    return {topic for topic, _, _ in fixtures}


def mark_publications(session: Path) -> None:
    """Attach known identities while retaining the ROS payloads and schemas."""
    from mcap.reader import make_reader
    from mcap.writer import CompressionType, Writer

    for path in sorted(session.glob("*.mcap")):
        with path.open("rb") as stream:
            records = list(make_reader(stream).iter_messages(log_time_order=False))
        temporary = path.with_suffix(".rewrite")
        with temporary.open("wb") as stream:
            writer = Writer(
                stream, compression=CompressionType.ZSTD, enable_data_crcs=True
            )
            writer.start(profile="ros2")
            writer.add_metadata(
                "rig.publication_provenance",
                {"schema": "1", "contract": "rmw_publication"},
            )
            schemas = {}
            channels = {}
            for index, (schema, channel, message) in enumerate(records):
                assert schema is not None, f"Missing ROS schema in {path}"
                if schema.id not in schemas:
                    schemas[schema.id] = writer.register_schema(
                        name=schema.name, encoding=schema.encoding, data=schema.data
                    )
                if channel.id not in channels:
                    evidence = {
                        "schema": "1",
                        "kind": "rmw_publication",
                        "scope": "export-runtime-test",
                        "rmw": "rmw_zenoh_cpp",
                        "domain_id": "193",
                        "writer_gid": f"{index + 1:032x}",
                        "sequence_upper32": "7",
                    }
                    metadata = dict(channel.metadata)
                    metadata.update(
                        {
                            f"rig.provenance.{key}": value
                            for key, value in evidence.items()
                        }
                    )
                    channels[channel.id] = writer.register_channel(
                        topic=channel.topic,
                        message_encoding=channel.message_encoding,
                        schema_id=schemas[schema.id],
                        metadata=metadata,
                    )
                writer.add_message(
                    channel_id=channels[channel.id],
                    data=message.data,
                    log_time=message.log_time,
                    publish_time=message.publish_time,
                    sequence=index + 17,
                )
            writer.finish()
        temporary.replace(path)


def publication_records(session: Path, identity) -> Counter:
    """Count exact topic/identity/timestamp/payload associations."""
    records = Counter()
    for record in identity.iter_mcap_records(session):
        publication = identity.identity_key(
            record.channel.metadata, record.message.sequence
        )
        assert publication is not None, f"Missing publication identity in {session}"
        key = (
            record.channel.topic,
            publication,
            record.message.log_time,
            record.message.publish_time,
            record.message.data,
        )
        records[key] += 1
    return records


def export_cases(all_topics: set[str]) -> list[tuple[str, dict, set[str]]]:
    return [
        ("default", {}, all_topics),
        (
            "exclude-events",
            {"exclude": ["/_service_event$"]},
            {"/visible", "/_hidden", "/move/_action/status"},
        ),
        (
            "service-only",
            {"topics": ["/set_bool/_service_event"]},
            {"/set_bool/_service_event"},
        ),
        (
            "action-status-only",
            {"topics": ["/move/_action/status"]},
            {"/move/_action/status"},
        ),
        (
            "action-event-only",
            {"topics": ["/move/_action/cancel_goal/_service_event"]},
            {"/move/_action/cancel_goal/_service_event"},
        ),
        ("hidden-only", {"topics": ["/_hidden"]}, {"/_hidden"}),
        ("empty-exclude", {"exclude": [".*"]}, set()),
        ("empty-allow", {"topics": ["/absent"]}, set()),
        (
            "window",
            {"from_s": 1, "to_s": 3},
            {"/_hidden", "/set_bool/_service_event", "/move/_action/status"},
        ),
    ]


def run_export(root: Path, tools: Path, name: str, options: dict) -> Path:
    """Run the renderer and its generated conversion script, retaining logs."""
    destination = root / name
    destination.mkdir()
    options_path = root / f"{name}.yaml"
    options_path.write_text(yaml.safe_dump(options))
    environment = {
        **os.environ,
        "RIG_EXPORT_SOURCE": str(root / "source"),
        "RIG_EXPORT_DEST": str(destination),
        "RIG_EXPORT_OPTIONS": str(options_path),
    }
    rendered = subprocess.check_output(
        [
            sys.executable,
            str(tools / "bag_export.py"),
            str(root / "logger.yaml"),
            str(tools.parent),
        ],
        env=environment,
        text=True,
    ).strip().split("\t")
    result = subprocess.run(
        ["bash", rendered[0]], capture_output=True, text=True
    )
    log = root / f"{name}.log"
    log.write_text(result.stdout + "\n" + result.stderr)
    assert result.returncode == 0, f"{name}: export failed; see {log}"
    return destination / "bags/logger/session"


def verify_export(output: Path, expected: Counter, topics: set[str], identity) -> dict:
    actual = publication_records(output, identity)
    selected = Counter(
        {key: count for key, count in expected.items() if key[0] in topics}
    )
    assert actual == selected, f"{output}: raw publication records differ"
    information = yaml.safe_load((output / "metadata.yaml").read_text())[
        "rosbag2_bagfile_information"
    ]
    count = sum(actual.values())
    assert information["message_count"] == count, f"{output}: metadata count differs"
    return {
        "messages": count,
        "topics": sorted({key[0] for key in actual}),
        "metadata_count": information["message_count"],
        "result": "pass",
    }


def check_exports(root: Path) -> dict:
    tools = Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tools))
    try:
        import publication_identity
    finally:
        sys.path.pop(0)

    session = root / "source/bags/logger/session"
    topics = create_fixture(session)
    mark_publications(session)
    expected = publication_records(session, publication_identity)
    assert len(expected) == len(topics), "Fixture must have one sample per topic"
    (root / "logger.yaml").write_text("name: logger\n")

    reports = {}
    for name, options, selected_topics in export_cases(topics):
        output = run_export(root, tools, name, options)
        reports[name] = verify_export(
            output, expected, selected_topics, publication_identity
        )
    (root / "report.json").write_text(json.dumps(reports, indent=2) + "\n")
    return reports


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    root = parser.parse_args(argv).output.resolve()
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        parser.error(f"output directory already exists: {root}")
    print(json.dumps(check_exports(root), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
