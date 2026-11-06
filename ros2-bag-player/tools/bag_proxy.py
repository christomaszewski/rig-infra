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


DEFAULTS = dict(time_source="received", deduplication="conservative",
                sensor_timestamp_field="header.stamp", timestamp_fallback="error",
                topic_timestamp_fields={}, max_clock_skew_s=60.0, dedup_window_ms=100.0)


def policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
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
    for key in ("max_clock_skew_s", "dedup_window_ms"):
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


def effective_timestamp(topic, received, sent, message, settings):
    source = settings["time_source"]
    if source == "received":
        return received, False
    try:
        if source == "sent":
            if not 0 < sent < 2**63:
                raise ValueError("missing send timestamp")
            value = sent
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
    if abs(value - received) > settings["max_clock_skew_s"] * 1e9:
        raise ValueError(f"{topic}: {source} timestamp is outside max_clock_skew_s of receive time; "
                         "verify the clock domain before increasing the bound")
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
              signature TEXT, identity TEXT, keep INTEGER DEFAULT 1, duplicate_of INTEGER,
              reason TEXT DEFAULT 'retained');
            CREATE INDEX receive_order ON records(received,source,ordinal);
            CREATE INDEX signature ON records(signature,received);
            CREATE INDEX identity ON records(identity);
            CREATE INDEX schedule ON records(effective,source,ordinal);
            CREATE TABLE matched(canonical INTEGER, source INTEGER,
                                 PRIMARY KEY(canonical,source));
        """)
        self.count = 0
        self.fallback_count = 0

    def add(self, source, ordinal, topic, topic_type, payload, received, sent, message=None):
        if type(received) is not int or not 0 <= received < 2**63 or type(sent) is not int:
            raise ValueError(f"{topic}: invalid original timestamp")
        effective, fallback = effective_timestamp(topic, received, sent, message, self.settings)
        checksum = hashlib.sha256(payload).hexdigest()
        signature = json.dumps([topic, topic_type, checksum])
        identity = proven_identity(topic_type, message)
        if identity is not None:
            identity = json.dumps([signature, identity])
        self.db.execute("INSERT INTO records(source,ordinal,topic,received,sent,effective,payload,"
                        "signature,identity) VALUES (?,?,?,?,?,?,?,?,?)",
                        (source, ordinal, topic, received, sent, effective, payload, signature, identity))
        self.count += 1
        self.fallback_count += fallback
        if self.count % 1000 == 0:
            self.db.commit()

    def deduplicate(self):
        self.db.commit()
        stats = dict(input_messages=self.count, output_messages=self.count, proven_duplicates=0,
                     likely_duplicates=0, ambiguous_preserved=0, timestamp_fallbacks=self.fallback_count)
        mode = self.settings["deduplication"]
        window = int(self.settings["dedup_window_ms"] * 1e6)
        # The selected canonical must precede this record in the total receive order.
        for rid, source, ordinal, received, signature, identity in self.db.execute(
                "SELECT id,source,ordinal,received,signature,identity FROM records "
                "ORDER BY received,source,ordinal"):
            if mode == "off":
                continue
            common = (" AND keep=1 AND source!=? AND (received,source,ordinal)<(?,?,?) "
                      "AND NOT EXISTS(SELECT 1 FROM matched WHERE canonical=records.id AND source=?) "
                      "ORDER BY received,source,ordinal LIMIT 1")
            args = (source, received, source, ordinal, source)
            match = None
            if identity:
                match = self.db.execute("SELECT id FROM records WHERE identity=?" + common,
                                        (identity, *args)).fetchone()
            reason = "proven_duplicate"
            if match:
                stats["proven_duplicates"] += 1
            else:
                likely = self.db.execute("SELECT id FROM records WHERE signature=? "
                    "AND received>=?" + common, (signature, received - window, *args)).fetchone()
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
        self.db.commit()
        return stats

    def output(self):
        return self.db.execute("SELECT topic,payload,effective,sent FROM records WHERE keep=1 "
                               "ORDER BY effective,source,ordinal")

    def provenance(self, path):
        with Path(path).open("w") as stream:
            for row in self.db.execute("SELECT id,source,ordinal,topic,received,sent,effective,"
                                       "duplicate_of,reason FROM records ORDER BY id"):
                stream.write(json.dumps(dict(zip(("id", "input", "ordinal", "topic", "received_ns",
                    "sent_ns", "schedule_ns", "duplicate_of", "decision"), row)), separators=(",", ":")) + "\n")

    def close(self):
        self.db.close()


def convert(request):
    implementation_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message, get_service

    settings = policy(request["policy"])
    stage = Path(request["destination"])
    if (stage / "bag").exists():
        raise ValueError("bag proxy output already exists")
    index_path = stage / "merge.sqlite3"
    index = MergeIndex(index_path, settings)
    topics, classes, unknown_qos = {}, {}, set()
    writer = None
    try:
        for source, bag in enumerate(request["bags"]):
            reader = rosbag2_py.SequentialReader()
            reader.open(rosbag2_py.StorageOptions(uri=bag["path"], storage_id=bag["storage"]),
                        rosbag2_py.ConverterOptions("cdr", "cdr"))
            available = {}
            for tm in reader.get_all_topics_and_types():
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
            ordinal = 0
            while reader.has_next():
                topic, payload, received, sent = reader.read_next_ext()[:4]
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
                index.add(source, ordinal, topic, topic_type, bytes(payload), received, sent, message)
                ordinal += 1
            del reader
        stats = index.deduplicate()
        stats["unknown_qos_topics"] = sorted(unknown_qos)
        stats["unavailable_message_types"] = sorted(name for name, cls in classes.items() if cls is None)
        if not stats["output_messages"]:
            raise ValueError("bag proxy has no messages")
        writer = rosbag2_py.SequentialWriter()
        writer.open(rosbag2_py.StorageOptions(uri=str(stage / "bag"), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
        for name in sorted(topics):
            writer.create_topic(topics[name])
        expected = hashlib.sha256()
        for topic, payload, effective, sent in index.output():
            writer.write(topic, payload, recv_timestamp=effective, send_timestamp=sent)
            expected.update(json.dumps([topic, effective, sent, hashlib.sha256(payload).hexdigest()]).encode())
        writer.close()
        writer = None
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
        result = dict(version=1, validation={"ok": True, "payloads_verified": True},
                      statistics=stats, policy=settings,
                      producer={"implementation_sha256": implementation_sha})
        (stage / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        if writer is not None:
            writer.close()
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
