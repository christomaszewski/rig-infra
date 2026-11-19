"""Raw MCAP I/O for the offline proxy; ROS still supplies topic/QoS metadata."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from publication_identity import identity_key, iter_mcap_records


def descriptor(schema, channel):
    return dict(topic=channel.topic, encoding=channel.message_encoding,
                metadata=dict(channel.metadata),
                schema=(dict(name=schema.name, encoding=schema.encoding,
                             data=base64.b64encode(schema.data).decode("ascii")) if schema else None))


def descriptor_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def raw_digest_update(digest, topic, payload, received, sent, sequence, channel_hash):
    digest.update(json.dumps([topic, received, sent, sequence, channel_hash,
                             hashlib.sha256(payload).hexdigest()], separators=(",", ":")).encode())


def write_proxy(path, topics, index):
    """Preserve raw channel evidence while emitting an ordinary ROS2 MCAP bag.

An empty stock writer supplies the runtime's metadata.yaml and QoS serialization,
including zero-message topics. Its empty MCAP is replaced with the raw output.
"""
    import rosbag2_py
    import yaml
    from mcap.reader import make_reader
    from mcap.writer import Writer

    path = Path(path)
    seed = rosbag2_py.SequentialWriter()
    seed.open(rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"),
              rosbag2_py.ConverterOptions("cdr", "cdr"))
    try:
        for name in sorted(topics):
            seed.create_topic(topics[name])
    finally:
        seed.close()
    metadata_path = path / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text())
    info = metadata["rosbag2_bagfile_information"]
    files = info["relative_file_paths"]
    if len(files) != 1:
        raise ValueError("bag proxy: stock writer did not create one metadata template file")
    target = path / files[0]
    with target.open("rb") as stream:
        summary = make_reader(stream).get_summary()
    if summary is None:
        raise ValueError("bag proxy: stock metadata template has no MCAP summary")
    defaults = {c.topic: descriptor(summary.schemas.get(c.schema_id), c)
                for c in summary.channels.values()}
    if set(defaults) != set(topics):
        raise ValueError("bag proxy: stock metadata template omitted a topic")
    counts = {name: 0 for name in topics}
    schemas, channels, cached = {}, {}, {}
    expected = hashlib.sha256()
    first, last = None, None
    marked = False
    with target.open("wb") as stream:
        writer = Writer(stream, enable_data_crcs=True)
        writer.start(profile="ros2", library="rig-bag-proxy")

        def register(value):
            key = descriptor_hash(value)
            if key not in channels:
                schema = value["schema"]
                sid = 0
                if schema is not None:
                    skey = json.dumps(schema, sort_keys=True)
                    if skey not in schemas:
                        schemas[skey] = writer.register_schema(schema["name"], schema["encoding"],
                                                              base64.b64decode(schema["data"]))
                    sid = schemas[skey]
                channels[key] = writer.register_channel(value["topic"], value["encoding"], sid,
                                                        metadata=value["metadata"])
            return channels[key], key

        def prepare(topic, channel):
            cache_key = (topic, channel)
            if cache_key not in cached:
                value = index.channels.get(channel, defaults[topic])
                value = {**value, "metadata": dict(value["metadata"])}
                # Match the preexisting proxy's merged QoS evidence for every channel.
                qos = defaults[topic]["metadata"].get("offered_qos_profiles")
                if qos is not None:
                    value["metadata"]["offered_qos_profiles"] = qos
                cached[cache_key] = register(value)
            return cached[cache_key]

        # Preserve schemas and channel metadata for zero-message topics too.
        represented = set()
        for channel, value in index.channels.items():
            represented.add(value["topic"])
            prepare(value["topic"], channel)
        for topic in set(defaults) - represented:
            prepare(topic, None)
        for topic, payload, effective, sent, channel, sequence in index.output_records():
            cid, channel_hash = prepare(topic, channel)
            if not marked:
                evidence = index.channels.get(channel, defaults[topic])["metadata"]
                if identity_key(evidence, sequence) is not None:
                    writer.add_metadata("rig.publication_provenance",
                                        {"schema": "1", "contract": "rmw_publication"})
                    marked = True
            writer.add_message(cid, effective, bytes(payload), sent, sequence)
            raw_digest_update(expected, topic, payload, effective, sent, sequence, channel_hash)
            counts[topic] += 1
            first = effective if first is None else min(first, effective)
            last = effective if last is None else max(last, effective)
        writer.finish()
    count = sum(counts.values())
    info["message_count"] = count
    info["starting_time"] = {"nanoseconds_since_epoch": first or 0}
    info["duration"] = {"nanoseconds": (last - first) if first is not None else 0}
    for topic in info["topics_with_message_count"]:
        topic["message_count"] = counts[topic["topic_metadata"]["name"]]
    info["files"] = [dict(path=files[0], starting_time=info["starting_time"],
                          duration=info["duration"], message_count=count)]
    metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False))

    actual, written = hashlib.sha256(), 0
    channel_hashes = {}
    for record in iter_mcap_records(path):
        key = (record.file_path, record.channel.id)
        if key not in channel_hashes:
            channel_hashes[key] = descriptor_hash(descriptor(record.schema, record.channel))
        message = record.message
        raw_digest_update(actual, record.channel.topic, message.data, message.log_time,
                          message.publish_time, message.sequence, channel_hashes[key])
        written += 1
    if written != count or actual.digest() != expected.digest():
        raise ValueError("bag proxy: raw output payload/channel/identity verification failed")
