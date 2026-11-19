#!/usr/bin/env python3
"""Offline, disk-backed bag merge. Payload bytes are never rewritten.

Deduplication operates in original receive order before scheduling-time sorting.
An ordinary ROS bag does not identify the original publisher/message: equal bytes
are only a likely match. Service events can attest client/request/event identity.
All discarded records retain a provenance link to the retained record.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

from publication_identity import compression, identity_key, iter_mcap_records
from mcap_proxy import descriptor as mcap_descriptor, descriptor_hash, write_proxy


DEFAULTS = dict(time_source="received", deduplication="conservative",
                sensor_timestamp_field="header.stamp", timestamp_fallback="error",
                topic_timestamp_fields={}, max_clock_skew_s=60.0, dedup_window_ms=100.0)
TF_TYPE = "tf2_msgs/msg/TFMessage"
DEFAULT_TF_LEAD_MS = 100.0


def policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS) - {"tf_lead_time_ms"}:
        raise ValueError("bag proxy: unknown policy fields")
    result = {**DEFAULTS, **value}
    if result["time_source"] not in ("received", "sent", "sensor"):
        raise ValueError("bag proxy: time_source must be received, sent, or sensor")
    if result["deduplication"] not in ("conservative", "aggressive", "off"):
        raise ValueError("bag proxy: deduplication must be conservative, aggressive, or off")
    if result["timestamp_fallback"] not in ("error", "received", "sent"):
        raise ValueError("bag proxy: timestamp_fallback must be error, received, or sent")
    fields = result["topic_timestamp_fields"]
    if not isinstance(fields, dict) or any(not isinstance(k, str) or not k.startswith("/") for k in fields):
        raise ValueError("bag proxy: topic_timestamp_fields requires absolute topic names")
    for field in [result["sensor_timestamp_field"], *fields.values()]:
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", field):
            raise ValueError("bag proxy: timestamp fields must be dotted ROS field paths")
    for key in ("max_clock_skew_s", "dedup_window_ms", "tf_lead_time_ms"):
        if key not in result:
            continue  # Keep legacy profile policies byte-for-byte comparable to their requests.
        if type(result[key]) not in (float, int) or not math.isfinite(result[key]) or result[key] < 0:
            raise ValueError(f"bag proxy: {key} must be finite and nonnegative")
    return result


def timestamp(message, field):
    value = message
    for name in field.split("."):
        value = getattr(value, name)
    sec, nano = value.sec, value.nanosec
    if type(sec) is not int or type(nano) is not int or not 0 <= nano < 1_000_000_000:
        raise ValueError(f"invalid ROS timestamp at {field}")
    result = sec * 1_000_000_000 + nano
    if not 0 < result < 2**63:
        raise ValueError(f"unset or invalid ROS timestamp at {field}")
    return result


def effective_timestamp(topic, received, sent, message, settings, topic_type=None):
    source = settings["time_source"]
    if source == "received":
        return received, False
    is_tf = source == "sensor" and topic_type == TF_TYPE
    if is_tf and topic in settings["topic_timestamp_fields"]:
        raise ValueError(f"{topic}: TFMessage timing uses nested transform headers; "
                         "topic_timestamp_fields cannot override TF")
    def validate_clock(stamp):
        if abs(stamp - received) > settings["max_clock_skew_s"] * 1e9:
            raise ValueError(f"{topic}: {source} timestamp is outside max_clock_skew_s of receive time; "
                             "verify the clock domain before increasing the bound")

    stamps, invalid_tf_stamp = [], False
    if is_tf:
        # Check every usable clock before fallback: one missing stamp must not hide
        # an incompatible clock on a different transform in the same payload.
        for transform in getattr(message, "transforms", []):
            try:
                stamp = timestamp(transform, "header.stamp")
            except (AttributeError, ValueError):
                invalid_tf_stamp = True
            else:
                validate_clock(stamp)
                stamps.append(stamp)
    try:
        if source == "sent":
            if not 0 < sent < 2**63:
                raise ValueError("missing send timestamp")
            value = sent
        elif is_tf:
            if invalid_tf_stamp or not stamps:
                raise ValueError("TFMessage has missing or invalid transform timestamps")
            value = min(stamps)
        else:
            value = timestamp(message, settings["topic_timestamp_fields"].get(
                topic, settings["sensor_timestamp_field"]))
    except (AttributeError, ValueError):
        fallback = settings["timestamp_fallback"]
        if fallback == "error":
            raise ValueError(f"{topic}: no usable {source} timestamp; configure timestamp_fallback")
        if fallback == "received":
            return received, True
        if not 0 < sent < 2**63:
            raise ValueError(f"{topic}: no usable fallback send timestamp")
        value = sent
        fell_back = True
    else:
        fell_back = False
    # A missing field may fall back; an incompatible clock must never be hidden.
    validate_clock(value)
    if is_tf and not fell_back:
        value = max(0, value - int(settings.get("tf_lead_time_ms", DEFAULT_TF_LEAD_MS) * 1e6))
    return value, fell_back


def proven_identity(topic_type, message):
    """No graph, payload hash, header stamp, or MCAP sequence implies publisher identity."""
    if not topic_type.endswith("_Event") or "/srv/" not in topic_type or message is None:
        return None
    try:
        info = message.info
        gid = bytes(info.client_gid)
        if len(gid) != 16 or not any(gid) or info.sequence_number < 0:
            return None
        stamp = timestamp(info, "stamp")
        if info.event_type not in (0, 1, 2, 3):
            return None
        return json.dumps([gid.hex(), info.sequence_number, info.event_type, stamp])
    except (AttributeError, TypeError, ValueError):
        return None


class MergeIndex:
    """SQLite limits working RAM even for large image bags and globally reorders sensor time."""

    def __init__(self, path, settings):
        self.settings = policy(settings)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-16384;
            CREATE TABLE records(id INTEGER PRIMARY KEY, source INTEGER, ordinal INTEGER,
              topic TEXT, received INTEGER, sent INTEGER, effective INTEGER, payload BLOB,
              signature TEXT, identity TEXT, priority INTEGER, schedule_basis TEXT,
              publication_identity TEXT, publication_content TEXT, channel INTEGER,
              sequence INTEGER, source_record TEXT,
              keep INTEGER DEFAULT 1, duplicate_of INTEGER,
              reason TEXT DEFAULT 'retained');
            CREATE INDEX receive_order ON records(received,source,ordinal);
            CREATE INDEX signature ON records(signature,received);
            CREATE INDEX identity ON records(identity);
            CREATE INDEX publication_identity ON records(publication_identity);
            CREATE INDEX schedule ON records(effective,priority,source,ordinal);
            CREATE TABLE static_frames(topic TEXT, child TEXT, geometry TEXT,
                                       PRIMARY KEY(topic,child));
            CREATE TABLE matched(canonical INTEGER, source INTEGER,
                                 PRIMARY KEY(canonical,source));
        """)
        self.count = 0
        self.fallback_count = 0
        self.channels = {}
        self._channel_ids = {}
        self._channel_schema_hashes = {}

    def channel(self, descriptor):
        key = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        if key not in self._channel_ids:
            channel = len(self.channels) + 1
            self._channel_ids[key] = channel
            self.channels[channel] = descriptor
            self._channel_schema_hashes[channel] = descriptor_hash(descriptor.get("schema"))
        return self._channel_ids[key]

    def add(self, source, ordinal, topic, topic_type, payload, received, sent, message=None,
            *, channel=None, sequence=0, source_record=None):
        if type(received) is not int or not 0 <= received < 2**63 or type(sent) is not int:
            raise ValueError(f"{topic}: invalid original timestamp")
        sensor_tf = self.settings["time_source"] == "sensor" and topic_type == TF_TYPE
        static_tf = sensor_tf and topic.rsplit("/", 1)[-1] == "tf_static"
        priority = 0 if static_tf else 1 if sensor_tf else 2
        if sensor_tf and topic in self.settings["topic_timestamp_fields"]:
            raise ValueError(f"{topic}: TFMessage timing uses nested transform headers; "
                             "topic_timestamp_fields cannot override TF")
        if static_tf:
            self._static_frames(topic, message)
            effective, fallback, basis = received, False, "tf_static_prelude"
        else:
            effective, fallback = effective_timestamp(topic, received, sent, message, self.settings, topic_type)
            basis = (self.settings["timestamp_fallback"] + "_fallback" if fallback else
                     "tf_transform_stamp" if sensor_tf else self.settings["time_source"] + "_stamp")
        checksum = hashlib.sha256(payload).hexdigest()
        signature = json.dumps([topic, topic_type, checksum])
        identity = proven_identity(topic_type, message)
        if identity is not None:
            identity = json.dumps([signature, identity])
        descriptor = self.channels.get(channel, {})
        # Preserve the established service-event identity policy. Its client/request
        # identity can intentionally span different publishing service endpoints.
        publication = (None if topic_type.endswith("_Event") and "/srv/" in topic_type else
                       identity_key(descriptor.get("metadata", {}), sequence))
        content = json.dumps([signature, sent, self._channel_schema_hashes.get(channel),
                              descriptor.get("metadata", {}).get("topic_type_hash", "")],
                             sort_keys=True, separators=(",", ":")) if publication else None
        self.db.execute("INSERT INTO records(source,ordinal,topic,received,sent,effective,payload,"
                        "signature,identity,priority,schedule_basis,publication_identity,"
                        "publication_content,channel,sequence,source_record) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (source, ordinal, topic, received, sent, effective, payload, signature, identity,
                         priority, basis, publication, content, channel, sequence,
                         json.dumps(source_record) if source_record is not None else None))
        self.count += 1
        self.fallback_count += fallback
        if self.count % 1000 == 0:
            self.db.commit()

    def _static_frames(self, topic, message):
        """Only immutable static geometry can safely be available for the entire proxy."""
        if message is None:
            raise ValueError(f"{topic}: TFMessage type must be available to prepare static transforms")
        for transform in message.transforms:
            child, parent = transform.child_frame_id, transform.header.frame_id
            translation, rotation = transform.transform.translation, transform.transform.rotation
            coordinates = [translation.x, translation.y, translation.z,
                           rotation.x, rotation.y, rotation.z, rotation.w]
            if not child or not parent or not all(math.isfinite(value) for value in coordinates):
                raise ValueError(f"{topic}: invalid static transform for child {child!r}")
            geometry = json.dumps([parent, *coordinates])
            previous = self.db.execute("SELECT geometry FROM static_frames WHERE topic=? AND child=?",
                                       (topic, child)).fetchone()
            if previous and previous[0] != geometry:
                raise ValueError(f"{topic}: conflicting static transform for {child!r}; sensor-time "
                                 "preloading requires constant geometry; use an arrival profile "
                                 "for recordings with changing static transforms")
            self.db.execute("INSERT OR IGNORE INTO static_frames VALUES (?,?,?)", (topic, child, geometry))

    def deduplicate(self):
        self.db.commit()
        stats = dict(input_messages=self.count, output_messages=self.count, proven_duplicates=0,
                     publication_duplicates=0, likely_duplicates=0, ambiguous_preserved=0,
                     timestamp_fallbacks=self.fallback_count)
        # A later conflicting payload must invalidate earlier potential matches too.
        # Scope/domain/GID/sequence identifies the publication independently of bytes.
        self.db.executescript("""
            CREATE TEMP TABLE identity_conflicts AS
              SELECT publication_identity FROM records WHERE publication_identity IS NOT NULL
              GROUP BY publication_identity HAVING COUNT(DISTINCT publication_content)>1;
            CREATE UNIQUE INDEX conflicting_identity ON identity_conflicts(publication_identity);
            UPDATE records SET reason='publication_identity_conflict'
              WHERE publication_identity IN (SELECT publication_identity FROM identity_conflicts);
        """)
        stats["conflicting_publication_identities"] = self.db.execute(
            "SELECT COUNT(*) FROM identity_conflicts").fetchone()[0]
        stats["conflicting_publication_records"] = self.db.execute(
            "SELECT COUNT(*) FROM records WHERE reason='publication_identity_conflict'").fetchone()[0]
        stats["publication_identity_records"] = self.db.execute(
            "SELECT COUNT(*) FROM records WHERE publication_identity IS NOT NULL").fetchone()[0]
        mode = self.settings["deduplication"]
        window = int(self.settings["dedup_window_ms"] * 1e6)
        # The selected canonical must precede this record in the total receive order.
        for rid, source, ordinal, received, signature, identity, publication, conflict in self.db.execute(
                "SELECT id,source,ordinal,received,signature,identity,publication_identity,"
                "reason='publication_identity_conflict' FROM records "
                "ORDER BY received,source,ordinal"):
            if mode == "off" or conflict:
                continue
            common = (" AND keep=1 AND source!=? AND (received,source,ordinal)<(?,?,?) "
                      "AND NOT EXISTS(SELECT 1 FROM matched WHERE canonical=records.id AND source=?) "
                      "ORDER BY received,source,ordinal LIMIT 1")
            args = (source, received, source, ordinal, source)
            match = None
            if publication:
                match = self.db.execute("SELECT id FROM records WHERE publication_identity=?" + common,
                                        (publication, *args)).fetchone()
            elif identity:
                match = self.db.execute("SELECT id FROM records WHERE identity=?" + common,
                                        (identity, *args)).fetchone()
            reason = "proven_duplicate"
            if match:
                stats["proven_duplicates"] += 1
                if publication:
                    stats["publication_duplicates"] += 1
            elif not publication:
                likely = self.db.execute("SELECT id FROM records WHERE signature=? "
                    "AND publication_identity IS NULL AND received>=?" + common,
                    (signature, received - window, *args)).fetchone()
                if likely:
                    if mode == "aggressive":
                        match, reason = likely, "likely_duplicate"
                        stats["likely_duplicates"] += 1
                    else:
                        stats["ambiguous_preserved"] += 1
                        self.db.execute("UPDATE records SET reason='ambiguous_preserved' WHERE id=?", (rid,))
            if match:
                self.db.execute("UPDATE records SET keep=0,duplicate_of=?,reason=? WHERE id=?",
                                (match[0], reason, rid))
                self.db.execute("INSERT INTO matched VALUES (?,?)", (match[0], source))
                stats["output_messages"] -= 1
        # Static TF stamps are timeless (often zero). Make every retained static frame
        # available before the earliest dynamic TF/data, without rewriting any payload.
        first = self.db.execute("SELECT MIN(effective) FROM records WHERE keep=1 AND priority!=0").fetchone()[0]
        if first is None:
            first = self.db.execute("SELECT MIN(received) FROM records WHERE keep=1").fetchone()[0]
        if first is not None:
            lead = max(1, int(self.settings.get("tf_lead_time_ms", DEFAULT_TF_LEAD_MS) * 1e6))
            self.db.execute("UPDATE records SET effective=? WHERE priority=0", (max(0, first - lead),))
        stats["tf_messages"] = self.db.execute("SELECT COUNT(*) FROM records WHERE keep=1 AND priority=1").fetchone()[0]
        stats["tf_static_messages"] = self.db.execute("SELECT COUNT(*) FROM records WHERE keep=1 AND priority=0").fetchone()[0]
        self.db.commit()
        return stats

    def output(self):
        return self.db.execute("SELECT topic,payload,effective,sent FROM records WHERE keep=1 "
                               "ORDER BY effective,priority,source,ordinal")

    def output_records(self):
        return self.db.execute("SELECT topic,payload,effective,sent,channel,sequence FROM records WHERE keep=1 "
                               "ORDER BY effective,priority,source,ordinal")

    def provenance(self, path):
        with Path(path).open("w") as stream:
            for row in self.db.execute("SELECT id,source,ordinal,topic,received,sent,effective,"
                                       "duplicate_of,reason,schedule_basis,priority,publication_identity,"
                                       "source_record FROM records ORDER BY id"):
                values = dict(zip(("id", "input", "ordinal", "topic", "received_ns",
                    "sent_ns", "schedule_ns", "duplicate_of", "decision", "schedule_basis",
                    "schedule_priority", "publication_identity", "source_record"), row))
                for key in ("publication_identity", "source_record"):
                    if values[key] is not None:
                        values[key] = json.loads(values[key])
                stream.write(json.dumps(values, separators=(",", ":")) + "\n")

    def close(self):
        self.db.close()


def convert(request):
    source = Path(__file__)
    producer = {
        "implementation_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "module_sha256": {
            name: hashlib.sha256(source.with_name(name).read_bytes()).hexdigest()
            for name in ("mcap_proxy.py", "publication_identity.py")
        },
    }
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message, get_service

    try:
        import mcap  # noqa: F401 — require inspection before accepting any MCAP source.
    except ImportError:
        if any(bag["storage"] == "mcap" for bag in request["bags"]):
            raise ValueError("bag proxy: MCAP inputs require the updated player image with Python mcap; "
                             "publication evidence cannot safely be inspected by the older ROS reader") from None
        raw_output = False  # Older images can still merge SQLite-only, unmarked recordings.
    else:
        raw_output = True

    settings = policy(request["policy"])
    stage = Path(request["destination"])
    if (stage / "bag").exists():
        raise ValueError("bag proxy output already exists")
    index_path = stage / "merge.sqlite3"
    index = MergeIndex(index_path, settings)
    topics, classes, unknown_qos = {}, {}, set()
    try:
        for source, bag in enumerate(request["bags"]):
            mode, _ = compression(bag["path"])
            reader = None
            if bag["storage"] == "mcap":
                # Metadata-only ROS API avoids its compression reader writing
                # decompressed files into the read-only source recording.
                info = rosbag2_py.Info().read_metadata(bag["path"], "mcap")
                bag_topics = [entry.topic_metadata for entry in info.topics_with_message_count]
            else:
                if mode:
                    raise ValueError("bag proxy: outer compression is supported for MCAP inputs only")
                reader = rosbag2_py.SequentialReader()
                reader.open(rosbag2_py.StorageOptions(uri=bag["path"], storage_id=bag["storage"]),
                            rosbag2_py.ConverterOptions("cdr", "cdr"))
                bag_topics = reader.get_all_topics_and_types()
            available = {}
            for tm in bag_topics:
                if tm.serialization_format != "cdr":
                    raise ValueError(f"{tm.name}: only CDR bag serialization is supported")
                previous = topics.get(tm.name)
                if previous:
                    unknown_hashes = ("", "RIHS01_" + "0" * 64)
                    old_hash = getattr(previous, "type_description_hash", "")
                    new_hash = getattr(tm, "type_description_hash", "")
                    if previous.type != tm.type or (old_hash not in unknown_hashes and
                            new_hash not in unknown_hashes and old_hash != new_hash):
                        raise ValueError(f"{tm.name}: conflicting type/schema across input bags")
                    if old_hash in unknown_hashes and new_hash not in unknown_hashes:
                        previous.type_description_hash = new_hash
                    qos = list(previous.offered_qos_profiles)
                    if not qos or not tm.offered_qos_profiles:
                        qos = []  # Missing evidence cannot establish transient-local durability.
                        unknown_qos.add(tm.name)
                    else:
                        for item in tm.offered_qos_profiles:
                            if item not in qos:
                                qos.append(item)
                    previous.offered_qos_profiles = qos
                else:
                    topics[tm.name] = tm
                    if not tm.offered_qos_profiles:
                        unknown_qos.add(tm.name)
                available[tm.name] = tm.type
            def records():
                if bag["storage"] == "mcap":
                    raw_channels = {}
                    def register_raw(file_path, schema, channel):
                        if channel.message_encoding != "cdr":
                            raise ValueError(f"{channel.topic}: only CDR bag serialization is supported")
                        if channel.topic not in available:
                            raise ValueError(f"{channel.topic}: raw channel absent from bag metadata")
                        if schema and schema.name != available.get(channel.topic):
                            raise ValueError(f"{channel.topic}: raw channel type disagrees with bag metadata")
                        raw_channels[file_path, channel.id] = index.channel(mcap_descriptor(schema, channel))
                    for raw in iter_mcap_records(bag["path"], on_channel=register_raw):
                        channel, data = raw.channel, raw.message
                        key = (raw.file_path, channel.id)
                        yield (channel.topic, data.data, data.log_time, data.publish_time,
                               raw_channels[key], data.sequence,
                               dict(file=raw.file_path.name, record=raw.file_ordinal, channel_id=channel.id))
                else:
                    while reader.has_next():
                        yield (*reader.read_next_ext()[:4], None, 0, None)

            for ordinal, (topic, payload, received, sent, channel, sequence, source_record) in enumerate(records()):
                topic_type = available[topic]
                message = None
                needs_sensor = settings["time_source"] == "sensor"
                needs_identity = topic_type.endswith("_Event") and "/srv/" in topic_type
                if needs_sensor or needs_identity:
                    if topic_type not in classes:
                        try:
                            classes[topic_type] = (get_service(topic_type[:-6]).Event if needs_identity
                                                   else get_message(topic_type))
                        except (ImportError, AttributeError, ValueError, RuntimeError):
                            classes[topic_type] = None
                    cls = classes[topic_type]
                    if cls is not None:
                        message = deserialize_message(payload, cls)
                    elif needs_sensor and settings["timestamp_fallback"] == "error":
                        raise ValueError(f"{topic}: message type unavailable for sensor timestamps: {topic_type}")
                index.add(source, ordinal, topic, topic_type, bytes(payload), received, sent, message,
                          channel=channel, sequence=sequence, source_record=source_record)
            del reader
        stats = index.deduplicate()
        stats["unknown_qos_topics"] = sorted(unknown_qos)
        stats["unavailable_message_types"] = sorted(name for name, cls in classes.items() if cls is None)
        if not stats["output_messages"]:
            raise ValueError("bag proxy has no messages")
        if raw_output:
            write_proxy(stage / "bag", topics, index)
        else:
            writer = rosbag2_py.SequentialWriter()
            writer.open(rosbag2_py.StorageOptions(uri=str(stage / "bag"), storage_id="mcap"),
                        rosbag2_py.ConverterOptions("cdr", "cdr"))
            try:
                for name in sorted(topics):
                    writer.create_topic(topics[name])
                for topic, payload, effective, sent in index.output():
                    writer.write(topic, payload, recv_timestamp=effective, send_timestamp=sent)
            finally:
                writer.close()
        expected = hashlib.sha256()
        for topic, payload, effective, sent in index.output():
            expected.update(json.dumps([topic, effective, sent, hashlib.sha256(payload).hexdigest()]).encode())
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(stage / "bag"), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
        actual, count, previous = hashlib.sha256(), 0, -1
        while reader.has_next():
            topic, payload, effective, sent = reader.read_next_ext()[:4]
            if effective < previous:
                raise ValueError("bag proxy verification found unsorted scheduling times")
            previous = effective
            actual.update(json.dumps([topic, effective, sent, hashlib.sha256(payload).hexdigest()]).encode())
            count += 1
        del reader
        if count != stats["output_messages"] or actual.digest() != expected.digest():
            raise ValueError("bag proxy verification failed: written messages differ")
        index.provenance(stage / "messages.jsonl")
        result = dict(version=1, validation={"ok": True, "payloads_verified": True,
                                          "publication_metadata_verified": raw_output},
                      statistics=stats, policy=settings,
                      producer=producer)
        (stage / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        index.close()
        index_path.unlink(missing_ok=True)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise ValueError("usage: bag_proxy.py REQUEST_JSON")
    request = json.loads(Path(args[0]).read_text())
    result = convert(request)
    Path(request["result"]).write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write(f"bag proxy: {exc}\n")
        sys.exit(2)
