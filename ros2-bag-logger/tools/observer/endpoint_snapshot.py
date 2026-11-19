"""Bounded ROS graph endpoint evidence; never subscribes to or hashes payloads.

At most 512 topics / 4096 endpoints / 250 ms of work between graph API calls
per observation. A middleware call itself cannot be preempted by this budget.
Coverage means queries completed, not that discovery was globally complete or
atomic. Endpoint sightings do not identify individual records in a ROS bag.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

MAX_TOPICS = 512
MAX_ENDPOINTS = 4096
MAX_OBSERVATION_S = 0.250
TYPE_HASH = re.compile(r"RIHS01_[0-9a-fA-F]{64}$")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def attribute(obj, name):
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def enum_value(value):
    if value is None:
        return {"status": "unknown"}
    name = attribute(value, "name")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = None
    result = {"status": "ok" if name is not None or number is not None else "unknown"}
    if name is not None:
        result["name"] = str(name)
    if number is not None:
        result["value"] = number
    return result


def endpoint(info, kind):
    name, namespace = attribute(info, "node_name"), attribute(info, "node_namespace")
    node = ((namespace.rstrip("/") + "/" + name) if isinstance(name, str) and name
            and isinstance(namespace, str) else None)
    gid = {"status": "unknown", "reason": "endpoint GID unavailable"}
    raw_gid = attribute(info, "endpoint_gid")
    if raw_gid is not None:
        try:
            raw_gid = bytes(raw_gid)
            gid = {"status": "ok" if raw_gid and any(raw_gid) else "unknown",
                   "hex": raw_gid.hex(), "length": len(raw_gid)}
            if gid["status"] != "ok":
                gid["reason"] = "empty or all-zero endpoint GID"
        except (ValueError, TypeError, OverflowError):
            gid = {"status": "unknown", "reason": "invalid endpoint GID representation"}
    type_hash = {"status": "unknown", "reason": "type hash unavailable"}
    raw_hash = attribute(info, "topic_type_hash")
    if raw_hash is not None and attribute(raw_hash, "version") != 0:
        text = str(raw_hash)
        if TYPE_HASH.fullmatch(text):
            type_hash = {"status": "ok", "value": text}
    qos = attribute(info, "qos_profile")
    policies = {key: enum_value(attribute(qos, key))
                for key in ("history", "reliability", "durability", "liveliness")}
    depth = attribute(qos, "depth")
    policies["depth"] = {"status": "ok", "value": depth} if type(depth) is int else {"status": "unknown"}
    for key in ("deadline", "lifespan", "liveliness_lease_duration"):
        value = attribute(attribute(qos, key), "nanoseconds")
        policies[key + "_ns"] = ({"status": "ok", "value": value} if type(value) is int
                                 else {"status": "unknown"})
    avoid = attribute(qos, "avoid_ros_namespace_conventions")
    policies["avoid_ros_namespace_conventions"] = ({"status": "ok", "value": avoid}
                                                   if type(avoid) is bool else {"status": "unknown"})
    typ = attribute(info, "topic_type")
    return {"kind": kind, "node": node, "gid": gid,
            "type": typ if isinstance(typ, str) and typ else None,
            "type_hash": type_hash, "qos": policies}


def observe(node, topics, self_fqn=None, *, clock=time.monotonic, wall=utc_now,
            max_topics=MAX_TOPICS, max_endpoints=MAX_ENDPOINTS, budget_s=MAX_OBSERVATION_S):
    started, started_at = clock(), wall()
    names = sorted(set(topics))
    selected = names[:max_topics]
    total_endpoints = 0
    queries = 0
    partial = len(names) > len(selected)
    rows = []
    capabilities = {}
    methods = (("publishers", "publisher", "get_publishers_info_by_topic"),
               ("subscriptions", "subscription", "get_subscriptions_info_by_topic"))
    for _, _, method in methods:
        capabilities[method] = callable(attribute(node, method))
    for topic in selected:
        row = {"topic": topic}
        for key, kind, method in methods:
            query = {"status": "ok", "endpoints": []}
            row[key] = query
            if not capabilities[method]:
                query.update(status="unsupported", error="graph API unavailable")
                partial = True
                continue
            if total_endpoints >= max_endpoints or clock() - started >= budget_s:
                query.update(status="skipped", error="observation budget exhausted")
                partial = True
                continue
            queries += 1
            try:
                infos = getattr(node, method)(topic)
                for info in infos:
                    if total_endpoints >= max_endpoints or clock() - started >= budget_s:
                        query.update(status="partial", error="observation budget exhausted")
                        partial = True
                        break
                    value = endpoint(info, kind)
                    if value["node"] == self_fqn and self_fqn is not None:
                        continue
                    query["endpoints"].append(value)
                    total_endpoints += 1
                query["endpoints"].sort(key=lambda value: json.dumps(value, sort_keys=True))
            except Exception as exc:
                query.update(status="error", error=f"{type(exc).__name__}: {exc}"[:512])
                if isinstance(exc, NotImplementedError):
                    query["status"] = "unsupported"
                partial = True
        rows.append(row)
    ended = clock()
    return {"started_at": started_at, "ended_at": wall(),
            "started_monotonic_s": started, "ended_monotonic_s": ended,
            "evidence": {"topics": rows, "capabilities": capabilities,
                         "coverage": {"status": "partial" if partial else "queries_completed",
                                      "topic_source": "topology_observation",
                                      "atomic": False, "topics_discovered": len(names),
                                      "topics_attempted": len(selected), "topics_omitted": len(names) - len(selected),
                                      "queries_attempted": queries, "endpoints_recorded": total_endpoints},
                         "limits": {"topics": max_topics, "endpoints": max_endpoints,
                                    "between_calls_budget_s": budget_s}}}


class EndpointTracker:
    def __init__(self, rmw, domain, collector_id=None):
        self.rmw, self.domain = rmw, domain
        self.collector_id = collector_id or uuid.uuid4().hex
        self.sequence = 0
        self.filename = self.digest = self.first = None
        self.first_sequence = 0

    def tick(self, observation, topology_hash, topology_file, exists, topology_written=True):
        self.sequence += 1
        # Keep all topology association changes, without changing topology's own
        # hash/epoch rules. Collector/timing fields never affect endpoint identity.
        body = {"endpoint_evidence": observation["evidence"], "topology_hash": topology_hash,
                "topology_written": topology_written}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != self.digest:
            stamp = re.sub(r"[^0-9TZ]", "", observation["started_at"])
            base = f"endpoints_{stamp}_{self.collector_id[:12]}"
            filename, count = base + ".json", 0
            while exists(filename):
                count += 1
                filename = f"{base}_{count}.json"
            self.filename, self.digest = filename, digest
            self.first = {key: observation[key] for key in ("started_at", "ended_at", "started_monotonic_s", "ended_monotonic_s")}
            self.first_sequence = self.sequence
            action = "open"
        else:
            action = "bump"
        last = {key: observation[key] for key in ("started_at", "ended_at", "started_monotonic_s", "ended_monotonic_s")}
        document = {"schema": 1, "kind": "ros_endpoint_provenance", "collector_id": self.collector_id,
                    "first_sequence": self.first_sequence, "last_sequence": self.sequence,
                    "observations": self.sequence - self.first_sequence + 1,
                    "first_observation": self.first, "last_observation": last,
                    "window_semantics": "sampled sightings; not continuous endpoint lifetime",
                    "rmw": self.rmw, "domain": self.domain, "sha256": digest,
                    "topology_file": topology_file if topology_written else None,
                    "intended_topology_file": topology_file, **body}
        return action, self.filename, document


def atomic_json(path: Path, document):
    temporary = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
