#!/usr/bin/env python3
"""Fail closed before publishing a convert that changes publication evidence.

The small outer-record scan also works in older custom images without ``mcap``.
Our recorder always emits its marker outside chunks, so those images can still
export old bags while refusing marked bags before conversion. For marked input,
an on-disk multiset verifies each selected raw record, independently of ordering,
chunk compression, channel IDs and split boundaries. Repeated records are kept.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import tempfile
import shutil

import yaml

from publication_identity import PREFIX, identity_key, iter_mcap_records, mcap_files

MAGIC = b"\x89MCAP0\r\n"
MARKER = "rig.publication_provenance"


def outer_evidence(path: Path, *, marker_only=False) -> bool:
    """Find the uncompressed marker or reserved channel keys, without codecs.

    Chunk/message payloads are skipped, never searched for coincidental text.
    Metadata/channel reads are bounded; oversized records fail closed.
    """
    if path.name.endswith(".mcap.zstd"):
        # Outer rosbag file compression hides even the uncompressed MCAP marker.
        # Spool to disk so the scan remains bounded in memory and never mutates
        # the read-only source. Missing codecs refuse before conversion.
        try:
            import zstandard
        except ImportError as exc:
            raise ValueError("checking a file-compressed MCAP requires Python zstandard; "
                             "use the current fleet-ros image; source untouched") from exc
        with tempfile.TemporaryDirectory(prefix="rig-export-check-") as directory:
            raw = Path(directory) / "source.mcap"
            with path.open("rb") as source, raw.open("wb") as target:
                with zstandard.ZstdDecompressor().stream_reader(source) as reader:
                    shutil.copyfileobj(reader, target, length=1024 * 1024)
            return outer_evidence(raw, marker_only=marker_only)
    found = False
    marker_found = False
    size = path.stat().st_size
    with path.open("rb") as stream:
        if stream.read(8) != MAGIC or size < 16:
            raise ValueError(f"invalid MCAP header: {path}")
        stream.seek(size - 8)
        if stream.read(8) != MAGIC:
            raise ValueError(f"incomplete MCAP: {path}")
        stream.seek(8)
        while stream.tell() < size - 8:
            header = stream.read(9)
            if len(header) != 9:
                raise ValueError(f"truncated MCAP record: {path}")
            opcode, length = struct.unpack("<BQ", header)
            end = stream.tell() + length
            if end > size - 8:
                raise ValueError(f"invalid MCAP record length: {path}")
            if opcode in (0x04, 0x0C):  # Channel / Metadata, including summary channels
                if length > 16 * 1024 * 1024:
                    raise ValueError(f"MCAP metadata too large to verify safely: {path}")
                data = stream.read(length)
                pos = 0

                def string():
                    nonlocal pos
                    if pos + 4 > len(data):
                        raise ValueError(f"truncated MCAP metadata: {path}")
                    count = struct.unpack_from("<I", data, pos)[0]
                    pos += 4
                    if pos + count > len(data):
                        raise ValueError(f"truncated MCAP string: {path}")
                    value = data[pos:pos + count].decode("utf-8")
                    pos += count
                    return value

                if opcode == 0x04:
                    pos = 4  # channel_id + schema_id
                    string()  # topic
                    string()  # encoding
                    name = None
                else:
                    name = string()
                if pos + 4 > len(data):
                    raise ValueError(f"truncated MCAP map: {path}")
                map_length = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                if pos + map_length != len(data):
                    raise ValueError(f"invalid MCAP map length: {path}")
                fields = {}
                while pos < len(data):
                    key = string()
                    value = string()
                    if key in fields:
                        raise ValueError(f"duplicate MCAP metadata key: {path}")
                    fields[key] = value
                if name == MARKER:
                    if fields.get("schema") != "1" or fields.get("contract") != "rmw_publication":
                        raise ValueError(f"unsupported publication provenance marker: {path}")
                    found = True
                    marker_found = True
                if opcode == 0x04 and any(key.startswith(PREFIX) for key in fields):
                    found = True
            stream.seek(end)
    return marker_found if marker_only else found


def capability_path() -> Path:
    return Path("/opt/ros") / os.environ.get("ROS_DISTRO", "lyrical") / "share/rig_publication_provenance/capability.json"


def require_capability(path: Path):
    try:
        capability = json.loads(path.read_text())
        valid = (capability.get("schema_version") == 1 and
                 capability.get("contract") == "rmw_publication" and
                 capability.get("preserve_mcap_convert") is True and
                 "rmw_zenoh_cpp" in capability.get("rmw_implementations", []))
    except (OSError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("this bag contains publication provenance; the runtime lacks verified "
                         "MCAP convert support. Use the current fleet-ros image; source untouched")
    try:
        import mcap.reader  # noqa: F401
    except ImportError as exc:
        raise ValueError("publication provenance verification requires Python mcap; "
                         "use the current fleet-ros image; source untouched") from exc


def selected(record, spec):
    """Selection for this renderer's topic/exclude/window options.

    The renderer enables hidden/service/action interfaces for lossless export,
    and resolves explicit filters to exact full topic names before conversion.
    """
    output = spec["output_bags"][0]
    topic = record.channel.topic
    if not output.get("all_topics", False) and topic not in output.get("topics", []):
        return False
    if output.get("exclude_regex") and re.search(output["exclude_regex"], topic):
        return False
    source = (spec.get("input_bags") or [{}])[0]
    start, end = source.get("start_time_ns", 0), source.get("end_time_ns", 0)
    return ((not start or record.message.log_time >= start) and
            (not end or record.message.log_time <= end))  # rosbag2 end is inclusive


def fingerprint(record):
    channel, message, schema = record.channel, record.message, record.schema
    metadata = channel.metadata
    evidence = {key: value for key, value in metadata.items() if key.startswith(PREFIX)}
    if evidence and identity_key(metadata, message.sequence) is None:
        raise ValueError(f"unsupported or malformed publication provenance: {record.file_path}, "
                         f"record {record.file_ordinal}")
    # QoS can be normalized by rosbag2; these fields describe the actual sample
    # and its attested writer. Channel/schema IDs are file-local and may change.
    values = [channel.topic, schema.name if schema else None, channel.message_encoding,
              metadata.get("topic_type_hash", ""), evidence, message.sequence,
              message.log_time, message.publish_time, hashlib.sha256(message.data).hexdigest()]
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest, bool(evidence)


def file_signature(path):
    stat = Path(path).stat()
    return json.dumps([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])


def check_source_files(db):
    for path, signature in db.execute("SELECT path,signature FROM source_files"):
        if file_signature(path) != signature:
            raise ValueError(f"source changed during export; stop recording before exporting: {path}")


def prepare(source: Path, spec: dict, state: Path, capability: Path | None = None):
    info = yaml.safe_load((source / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
    files = mcap_files(source) if info.get("storage_identifier") == "mcap" else []
    signatures = [(str(path), file_signature(path)) for path in [source / "metadata.yaml", *files]]
    marked = any([outer_evidence(path) for path in files])
    temporary = state.with_name(state.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(temporary) as db:
            db.execute("CREATE TABLE state (marked INTEGER NOT NULL)")
            db.execute("INSERT INTO state VALUES (?)", (int(marked),))
            db.execute("CREATE TABLE records (digest TEXT PRIMARY KEY, count INTEGER NOT NULL)")
            db.execute("CREATE TABLE source_files (path TEXT PRIMARY KEY, signature TEXT NOT NULL)")
            db.executemany("INSERT INTO source_files VALUES (?,?)", signatures)
            if marked:
                require_capability(capability or capability_path())
                for record in iter_mcap_records(source):
                    digest, _ = fingerprint(record)  # validate even dropped channels
                    if selected(record, spec):
                        db.execute("INSERT INTO records VALUES (?,1) ON CONFLICT(digest) "
                                   "DO UPDATE SET count=count+1", (digest,))
                check_source_files(db)
        temporary.replace(state)
    finally:
        temporary.unlink(missing_ok=True)


def verify(output: Path, state: Path):
    # mode=rw refuses a missing index; sqlite's normal default creates empty DBs.
    with sqlite3.connect(state.resolve().as_uri() + "?mode=rw", uri=True) as db:
        row = db.execute("SELECT marked FROM state").fetchone()
        if row == (0,):
            return
        if row != (1,):
            raise ValueError("invalid publication verification state")
        check_source_files(db)
        evidence_files = {path for path in mcap_files(output) if outer_evidence(path, marker_only=True)}
        count = 0
        for record in iter_mcap_records(output):
            digest, marked = fingerprint(record)
            if marked and record.file_path not in evidence_files:
                raise ValueError("converted provenance lacks its uncompressed file marker")
            changed = db.execute("UPDATE records SET count=count-1 WHERE digest=? AND count>0", (digest,))
            if changed.rowcount != 1:
                raise ValueError("converted record changed payload, timestamps, type, sequence or "
                                 "publication provenance (or added a duplicate); source untouched")
            count += 1
        remaining = db.execute("SELECT COALESCE(SUM(count),0) FROM records").fetchone()[0]
        if remaining:
            raise ValueError(f"converted bag lost {remaining} selected record(s) or their "
                             "publication provenance; source untouched")
        check_source_files(db)
        print(f"bag_export: verified publication provenance and content of {count} record(s)", file=sys.stderr)


def stage_input(source: Path, destination: Path, spec: dict, input_spec: Path):
    """Give rosbag2's FILE decompressor a private writable input directory.

    It writes decompressed files beside the input, even when converting to a
    different output path. Copy only the listed bag files and metadata, never
    let absolute names or symlinks redirect these writes into the source run.
    """
    metadata = source / "metadata.yaml"
    info = yaml.safe_load(metadata.read_text())["rosbag2_bagfile_information"]
    names = info.get("relative_file_paths")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ValueError("file-compressed input has no valid relative_file_paths")
    if len(set(names)) != len(names):
        raise ValueError("file-compressed input has duplicate paths")
    root = source.resolve()
    paths = []
    for name in names:
        relative = Path(name)
        actual = (root / relative).resolve(strict=True)
        if (relative.is_absolute() or ".." in relative.parts or name == "metadata.yaml" or
                not actual.is_relative_to(root) or not actual.is_file()):
            raise ValueError(f"unsafe file-compressed input path: {name!r}")
        paths.append((actual, destination / relative))
    destination.mkdir(parents=True, exist_ok=False)
    for actual, target in paths:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(actual, target)
    shutil.copyfile(metadata, destination / "metadata.yaml")
    options = dict((spec.get("input_bags") or [{}])[0])
    options["uri"] = str(destination)
    input_spec.write_text(yaml.safe_dump({"input_bags": [options]}, sort_keys=False))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("prepare")
    pre.add_argument("source", type=Path)
    pre.add_argument("spec", type=Path)
    pre.add_argument("state", type=Path)
    post = sub.add_parser("verify")
    post.add_argument("output", type=Path)
    post.add_argument("state", type=Path)
    stage = sub.add_parser("stage-input")
    stage.add_argument("source", type=Path)
    stage.add_argument("destination", type=Path)
    stage.add_argument("spec", type=Path)
    stage.add_argument("input_spec", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.source, yaml.safe_load(args.spec.read_text()), args.state)
        elif args.command == "verify":
            verify(args.output, args.state)
        else:
            stage_input(args.source, args.destination, yaml.safe_load(args.spec.read_text()), args.input_spec)
    except Exception as exc:
        print(f"bag_export: publication verification FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
