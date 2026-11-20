"""Versioned, recorder-attested publication identities in standard MCAP records.

Read the channel and payload from the same raw record. ROS bag readers may omit
the channel metadata and sequence; joining two independent iterators is unsafe.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re


PREFIX = "rig.provenance."


def identity_key(metadata, sequence):
    """Return an opaque publication key, or None when evidence is unsupported.

Schema 1 is intentionally restricted to the tested Lyrical Zenoh 16-byte GID
contract. Scope is recording evidence, not a claim of universal GID uniqueness.
"""
    if not isinstance(metadata, dict) or type(sequence) is not int or not 0 <= sequence < 2**32:
        return None
    def field(name):
        return metadata.get(PREFIX + name)
    if (field("schema") != "1" or field("kind") != "rmw_publication" or
            field("rmw") != "rmw_zenoh_cpp"):
        return None
    scope, gid = field("scope"), field("writer_gid")
    if (not isinstance(scope, str) or not scope or len(scope) > 128 or
            not re.fullmatch(r"[A-Za-z0-9_.:-]+", scope) or
            not isinstance(gid, str) or not re.fullmatch(r"[0-9a-f]{32}", gid) or
            gid == "0" * 32):
        return None
    numbers = []
    for name, bound in (("domain_id", 2**64), ("sequence_upper32", 2**32)):
        value = field(name)
        if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value) or len(value) > 20:
            return None
        number = int(value)
        if not 0 <= number < bound:
            return None
        numbers.append(number)
    domain, upper = numbers
    publication_sequence = (upper << 32) | sequence
    if publication_sequence == 2**64 - 1:
        return None
    return json.dumps(["rmw_publication", 1, scope, "rmw_zenoh_cpp", domain, gid,
                       publication_sequence], separators=(",", ":"))


def bag_info(path):
    path = Path(path)
    if path.is_file():
        return {}
    import yaml
    metadata = yaml.safe_load((path / "metadata.yaml").read_text())
    return metadata.get("rosbag2_bagfile_information", {}) if isinstance(metadata, dict) else {}


def compression(path):
    info = bag_info(path)
    mode, codec = info.get("compression_mode", ""), info.get("compression_format", "")
    mode = mode.lower() if isinstance(mode, str) else mode
    codec = codec.lower() if isinstance(codec, str) else codec
    if mode == "none":
        mode = ""
    if not mode and not codec:
        return "", ""
    if mode not in ("file", "message") or codec != "zstd":
        raise ValueError(f"publication identity: unsupported outer bag compression {mode!r}/{codec!r}")
    return mode, codec


def mcap_files(path):
    """Enumerate exactly the split files selected by a bag's metadata."""
    path = Path(path)
    if path.is_file():
        if path.suffix != ".mcap":
            raise ValueError(f"publication identity: expected MCAP file: {path}")
        return [path]
    info = bag_info(path)
    if info.get("storage_identifier") != "mcap":
        raise ValueError(f"publication identity: expected MCAP bag: {path}")
    relative = info.get("relative_file_paths")
    if (not isinstance(relative, list) or not relative or
            any(not isinstance(item, str) for item in relative) or len(set(relative)) != len(relative)):
        raise ValueError(f"publication identity: invalid split-file list: {path}")
    root = path.resolve()
    mode, _ = compression(path)
    suffix = ".mcap.zstd" if mode == "file" else ".mcap"
    result = []
    for name in relative:
        if not isinstance(name, str) or Path(name).is_absolute():
            raise ValueError(f"publication identity: invalid split filename: {name!r}")
        candidate = (root / name).resolve(strict=True)
        if (not candidate.is_relative_to(root) or not candidate.name.endswith(suffix) or
                not candidate.is_file()):
            raise ValueError(f"publication identity: split file escapes bag: {name!r}")
        result.append(candidate)
    return result


@dataclass(frozen=True)
class RawRecord:
    file_path: Path
    file_ordinal: int
    schema: object
    channel: object
    message: object


def iter_mcap_records(path, *, on_channel=None, skip_payload_topics=()):
    """Stream physical records with their own channel, including compressed bags.

    StreamReader validates data/chunk CRCs and does not reorder the whole file.
    Split files remain one caller-selected recording source. Consume the entire
    iterator: required outer-marker validation completes at each file's end.
    Topics in skip_payload_topics still yield records for original ordinals/counts,
    but their outer message-compressed payloads remain compressed. Callers must discard
    those payloads. Container structure, CRCs and publication markers remain checked.
"""
    from mcap.records import Channel, Message, Metadata, Schema
    from mcap.stream_reader import StreamReader
    skip_payload_topics = frozenset(skip_payload_topics)
    mode, _ = compression(path)
    if mode:
        import zstandard
        decompressor = zstandard.ZstdDecompressor()
    for file_path in mcap_files(path):
        with ExitStack() as stack:
            stream = stack.enter_context(file_path.open("rb"))
            if mode == "file":
                stream = stack.enter_context(decompressor.stream_reader(stream))
            reader = StreamReader(stream, validate_crcs=True)
            schemas, channels = {}, {}
            marker, marked, ordinal = False, False, 0
            for record in reader.records:
                if isinstance(record, Metadata) and record.name == "rig.publication_provenance":
                    if record.metadata.get("schema") == "1" and record.metadata.get("contract") == "rmw_publication":
                        marker = True
                elif isinstance(record, Schema):
                    if record.id in schemas and schemas[record.id] != record:
                        raise ValueError(f"publication identity: schema ID redefined in {file_path}")
                    schemas[record.id] = record
                elif isinstance(record, Channel):
                    if record.id in channels and channels[record.id] != record:
                        raise ValueError(f"publication identity: channel ID redefined in {file_path}")
                    if record.schema_id and record.schema_id not in schemas:
                        raise ValueError(f"publication identity: channel has unknown schema in {file_path}")
                    if record.id not in channels and on_channel is not None:
                        on_channel(file_path, schemas.get(record.schema_id), record)
                    channels[record.id] = record
                if not isinstance(record, Message):
                    continue
                message = record
                if message.channel_id not in channels:
                    raise ValueError(f"publication identity: message has unknown channel in {file_path}")
                channel = channels[message.channel_id]
                if channel.schema_id and channel.schema_id not in schemas:
                    raise ValueError(f"publication identity: channel has unknown schema in {file_path}")
                schema = schemas.get(channel.schema_id)
                marked = marked or identity_key(channel.metadata, message.sequence) is not None
                if mode == "message" and channel.topic not in skip_payload_topics:
                    message = replace(message, data=decompressor.decompress(message.data,
                                                                           max_output_size=2**30))
                yield RawRecord(file_path, ordinal, schema, channel, message)
                ordinal += 1
            if marked and not marker:
                raise ValueError(f"publication identity: marked samples lack required outer marker in {file_path}")
