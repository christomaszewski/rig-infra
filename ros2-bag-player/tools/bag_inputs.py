"""Exact bag selections shared by the player and its read-only helpers."""
from __future__ import annotations

import heapq
import json
from pathlib import Path, PurePosixPath


def known_schema_hash(value):
    """ROS encodes an unknown type hash as an all-zero RIHS01 value."""
    return value if value and value != "RIHS01_" + "0" * 64 else None


def validate_bags(bags, *, allow_unresolved_storage=False):
    if not isinstance(bags, list) or not bags:
        raise ValueError("bags must be a nonempty list of exact recording sessions")
    seen = set()
    for bag in bags:
        if not isinstance(bag, dict) or set(bag) != {"logger", "session", "relative_path", "storage"}:
            raise ValueError("each bag requires logger, session, relative_path and storage")
        for key in ("logger", "session"):
            value = bag[key]
            if not isinstance(value, str) or not value or "/" in value or value in (".", "..", "latest", "all"):
                raise ValueError(f"bag.{key} must name one exact session")
        path = bag["relative_path"]
        if (not isinstance(path, str) or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts or str(PurePosixPath(path)) != path
                or path in ("", ".") or "\x00" in path):
            raise ValueError("bag.relative_path must be a canonical path within the source root")
        if bag["storage"] not in ("mcap", "sqlite3") and not (allow_unresolved_storage and bag["storage"] is None):
            raise ValueError("bag.storage must be mcap or sqlite3")
        if path in seen:
            raise ValueError("the same bag session is selected more than once")
        seen.add(path)
    return bags


def resolved_inputs(root, bags, *, allow_unresolved_storage=False):
    return [{"path": str(Path(root) / bag["relative_path"]), "storage": bag["storage"]}
            for bag in validate_bags(bags, allow_unresolved_storage=allow_unresolved_storage)]


def selection_token(inputs, start_ns):
    """An argv-safe JSON token, never shell text; legacy callers still pass one directory."""
    return json.dumps({"inputs": inputs, "start_ns": start_ns}, separators=(",", ":"))


def parse_selection(token):
    if not token.startswith("{"):
        return [{"path": token, "storage": ""}], None
    value = json.loads(token)
    if not isinstance(value, dict) or set(value) != {"inputs", "start_ns"}:
        raise ValueError("invalid bag selection token")
    if type(value["start_ns"]) is not int or value["start_ns"] < 0:
        raise ValueError("bag selection requires a nonnegative start_ns")
    if not isinstance(value["inputs"], list) or not value["inputs"]:
        raise ValueError("bag selection requires inputs")
    for item in value["inputs"]:
        if (not isinstance(item, dict) or set(item) != {"path", "storage"}
                or not isinstance(item["path"], str) or not Path(item["path"]).is_absolute()
                or item["storage"] not in ("", "mcap", "sqlite3")):
            raise ValueError("invalid exact bag input")
    return value["inputs"], value["start_ns"]


def metadata_union(inputs):
    """Read all metadata and fail on incompatible same-name channels."""
    import rosbag2_py
    infos, types, hashes = [], {}, {}
    for item in inputs:
        info = rosbag2_py.Info().read_metadata(item["path"], item["storage"])
        for entry in info.topics_with_message_count:
            topic = entry.topic_metadata
            identity = (topic.type, topic.serialization_format)
            if topic.name in types and types[topic.name] != identity:
                raise ValueError(f"conflicting recorded types/serialization for {topic.name}")
            schema_hash = known_schema_hash(getattr(topic, "type_description_hash", ""))
            if hashes.get(topic.name) and schema_hash and hashes[topic.name] != schema_hash:
                raise ValueError(f"conflicting recorded schema hashes for {topic.name}")
            if schema_hash:
                hashes[topic.name] = schema_hash
            types[topic.name] = identity
        infos.append(info)
    return infos, types


def open_reader(item):
    import rosbag2_py
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=item["path"], storage_id=item["storage"]),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    if not reader.set_read_order(rosbag2_py.ReadOrder(
            sort_by=rosbag2_py.ReadOrderSortBy.ReceivedTimestamp, reverse=False)):
        raise RuntimeError("storage cannot read in received timestamp order")
    return reader


def merged_records(readers):
    """Bounded k-way receive-order merge; ties use input order, duplicates are retained."""
    heap = []
    for index, reader in enumerate(readers):
        if reader.has_next():
            record = reader.read_next_ext()
            heapq.heappush(heap, (record[2], index, record))
    while heap:
        _, index, record = heapq.heappop(heap)
        yield record
        if readers[index].has_next():
            following = readers[index].read_next_ext()
            if following[2] < record[2]:
                raise RuntimeError("storage returned records out of received timestamp order")
            heapq.heappush(heap, (following[2], index, following))
