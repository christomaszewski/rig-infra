#!/usr/bin/env python3
"""ros2-bag-logger graph-snapshotter: record the live ROS 2 graph topology (per-node pubs / subs /
service servers / clients) into the run directory as append-only, change-deduped EPOCH files —
`<run>/graph/<name>/epoch_<UTCstamp>.yaml` (flat `<data>/graph/<name>` without a run registry,
mirroring `bags/`). Bags record data; this records who talked to what. rig (`rig graph`,
`rig replay`) consumes the files as plain YAML and derives every view (union, instance grouping)
at read time — the writer stays dumb.

Epoch semantics (the frozen contract — rig-graph-capture-handoff §1.2):
  - Every `interval_s` tick: walk the graph, canonicalize, hash. Hash equal to the current epoch ->
    rewrite the SAME file with `last:` bumped (the liveness signal; a crash loses at most one
    interval). Hash differs -> open a NEW file (stamp = now = its `first:`); the old file is never
    touched again. All writes are atomic (temp file in the same dir, then rename).
  - On start — including restart after a crash — ALWAYS open a new epoch; never read, trust, or
    continue an existing file. Overlap is harmless; readers dedup by content. Same-second filename
    collision (pathological restart) suffixes `_<n>`.
  - Self-exclusion ONLY: the snapshotter drops its OWN node (a measurement artifact) and nothing
    else — hidden nodes, the rosbag2 recorder, everything the graph API returns is recorded raw.

Identity hash (audit-recomputable by construction): the canonical `nodes:` body only — node keys
sorted, per-kind edge lists deduped and sorted by (topic|service, type), one entry per type —
serialized as compact JSON (`json.dumps(nodes, sort_keys=True, separators=(",", ":"))`) and
sha256'd. `first`/`last`/`rmw`/`domain` are excluded, and the file's `nodes:` body IS the canonical
form, so re-hashing a written file agrees with the hash of the live observation that produced it.

Split like `bag_cmd.py`: a pure core (observation -> canonical form / hash / epoch text / filename,
no I/O, no ROS — unit-tested in ../tests/) and a thin rclpy shell (`walk` + the resident loop).
rclpy imports lazily so the core is importable on a dev box without ROS. Parameters travel by env
(`GRAPH_SNAPSHOT_NAME`, `GRAPH_SNAPSHOT_INTERVAL_S`, `GRAPH_SNAPSHOT_SETTLE_S` — exported by
ros2-bag-logger-up from the config's `graph:` block); run-dir resolution mirrors record.sh: resolve
`<root>/current` ONCE at process start and pin it (a symlink flip mid-run must not retarget the
writer).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

# ---------------------------------------------------------------------------------------------
# Pure core — observation dict -> canonical form, identity hash, epoch text, filename. No I/O.
# An observation maps node FQN -> {kind: [(name, [type, ...]), ...]} — the raw shape the four
# per-node NodeGraph calls return, so the shell stays a trivial walk.
# ---------------------------------------------------------------------------------------------

EDGE_KEY = {"pubs": "topic", "subs": "topic", "provides": "service", "requires": "service"}
_PLAIN = re.compile(r"^[A-Za-z0-9_./~-]+$")  # YAML-safe without quoting (topic/type/node names)


def fqn(name: str, namespace: str) -> str:
    """/ns/name from the (name, namespace) pairs `get_node_names_and_namespaces` returns."""
    ns = "/" + str(namespace).strip("/")
    return (ns if ns != "/" else "") + "/" + str(name)


def canonicalize(observation: dict, self_fqn: str | None = None) -> dict:
    """Observation -> the canonical `nodes:` body: self excluded, node keys sorted, every edge
    list deduped and sorted by (topic|service, type), one entry per type. Duplicate node FQNs
    (legal, discouraged) arrive merged by the by-node calls and stay merged under the one key."""
    nodes: dict = {}
    for node_key in sorted(observation):
        if node_key == self_fqn:
            continue
        raw = observation[node_key] or {}
        entry: dict = {}
        for kind, key in EDGE_KEY.items():
            pairs = set()
            for name, types in raw.get(kind) or []:
                for t in [types] if isinstance(types, str) else types:  # one entry per type
                    pairs.add((str(name), str(t)))
            entry[kind] = [{key: n, "type": t} for n, t in sorted(pairs)]
        nodes[node_key] = entry
    return nodes


def identity_hash(nodes: dict) -> str:
    """sha256 over the canonical body as compact sorted JSON — the audit formula, verbatim."""
    return hashlib.sha256(
        json.dumps(nodes, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _y(s: str) -> str:
    """A YAML plain scalar, double-quoted (JSON quoting is valid YAML) only when it needs it."""
    return s if _PLAIN.match(s) else json.dumps(s)


def render_epoch(nodes: dict, first: str, last: str, rmw: str, domain: int) -> str:
    """The epoch file text (schema 1). Hand-rolled so the stamps stay UNQUOTED plain scalars
    (yaml timestamps, as the contract example shows — PyYAML's dumper would quote them into
    strings) and the edge entries stay one-per-line flow mappings. Parses with yaml.safe_load."""
    out = [f"schema: 1\nfirst: {first}\nlast: {last}\nrmw: {_y(rmw)}\ndomain: {int(domain)}"]
    out.append("nodes:" if nodes else "nodes: {}")
    for node_key, entry in nodes.items():
        out.append(f"  {_y(node_key)}:")
        for kind, key in EDGE_KEY.items():
            edges = entry.get(kind) or []
            if not edges:
                out.append(f"    {kind}: []")
                continue
            out.append(f"    {kind}:")
            out += [f"    - {{{key}: {_y(e[key])}, type: {_y(e['type'])}}}" for e in edges]
    return "\n".join(out) + "\n"


def epoch_filename(stamp: str, exists) -> str:
    """epoch_<stamp>.yaml, suffixed _<n> while `exists(name)` says the slot is taken."""
    name, n = f"epoch_{stamp}.yaml", 0
    while exists(name):
        n += 1
        name = f"epoch_{stamp}_{n}.yaml"
    return name


class EpochTracker:
    """The epoch decision, pure (`exists` injected): feed each tick's canonical body and clock,
    get back (action, filename, file text) — "open" a new epoch when the hash changed (or on the
    first tick), "bump" = rewrite the current file with `last:` moved. State lives only in
    memory, so a process restart ALWAYS opens a new epoch, by construction."""

    def __init__(self, rmw: str, domain: int):
        self.rmw, self.domain = rmw, domain
        self.filename = self.hash = self.first = None

    def tick(self, nodes: dict, now: str, stamp: str, exists) -> tuple[str, str, str]:
        h = identity_hash(nodes)
        if h == self.hash:
            action = "bump"
        else:
            action = "open"
            self.filename, self.hash, self.first = epoch_filename(stamp, exists), h, now
        return action, self.filename, render_epoch(nodes, self.first, now, self.rmw, self.domain)


# ---------------------------------------------------------------------------------------------
# Shell — rclpy walk + the resident loop.
# ---------------------------------------------------------------------------------------------

def walk(node) -> dict:
    """One graph observation via the four per-node NodeGraph calls. A node that vanishes between
    enumeration and its by-node calls is dropped whole (the next tick sees the settled graph) —
    but a SHUTDOWN mid-walk (rclpy's signal handler invalidates the context, every call starts
    raising) must propagate, or a shutdown would masquerade as an everything-vanished epoch."""
    obs: dict = {}
    for name, ns in node.get_node_names_and_namespaces():
        try:
            obs[fqn(name, ns)] = {
                "pubs": node.get_publisher_names_and_types_by_node(name, ns),
                "subs": node.get_subscriber_names_and_types_by_node(name, ns),
                "provides": node.get_service_names_and_types_by_node(name, ns),
                "requires": node.get_client_names_and_types_by_node(name, ns),
            }
        except Exception:  # noqa: BLE001 — mid-walk disappearance; raced, not broken
            if not node.context.ok():
                raise
            obs.pop(fqn(name, ns), None)
    return obs


def resolve_out_dir(root: str, name: str) -> str:
    """record.sh's resolve-`current`-once block with `graph` in place of `bags` — called ONCE at
    process start; the result is pinned for the container's life."""
    cur = os.path.join(root, "current")
    base = os.path.join(os.path.realpath(cur), "graph") if os.path.exists(cur) \
        else os.path.join(root, "graph")
    return os.path.join(base, name)


def atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _utc() -> tuple[str, str]:
    """(ISO8601 second-resolution, filename stamp) for now — one clock read per tick."""
    t = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t), time.strftime("%Y%m%dT%H%M%SZ", t)


def main() -> int:
    import rclpy  # lazy: the pure core above stays importable (testable) without ROS

    name = os.environ.get("GRAPH_SNAPSHOT_NAME") or "bag_logger"
    interval = float(os.environ.get("GRAPH_SNAPSHOT_INTERVAL_S") or 60)
    settle = float(os.environ.get("GRAPH_SNAPSHOT_SETTLE_S") or 2)
    rmw = os.environ.get("RMW_IMPLEMENTATION") or "unknown"
    domain = int(os.environ.get("ROS_DOMAIN_ID") or 0)

    out_dir = resolve_out_dir(os.environ.get("RIG_BAG_ROOT") or "/data", name)
    os.makedirs(out_dir, exist_ok=True)

    rclpy.init()
    node = rclpy.create_node("graph_snapshotter_" + re.sub(r"[^A-Za-z0-9_]", "_", name))
    self_fqn = node.get_fully_qualified_name()
    sys.stderr.write(f"graph-snapshotter: {self_fqn} -> {out_dir} "
                     f"(interval={interval:g}s settle={settle:g}s rmw={rmw} domain={domain})\n")
    time.sleep(settle)  # zenoh graph cache warm-up: liveliness tokens replay after the node joins

    tracker = EpochTracker(rmw, domain)
    try:
        while True:
            t0 = time.monotonic()
            try:
                nodes = canonicalize(walk(node), self_fqn)
            except Exception:  # noqa: BLE001
                if not rclpy.ok():
                    break  # SIGTERM/SIGINT: rclpy's handler shut the context down — exit clean
                raise
            walk_ms = (time.monotonic() - t0) * 1000
            now, stamp = _utc()
            action, fname, text = tracker.tick(
                nodes, now, stamp, lambda f: os.path.exists(os.path.join(out_dir, f)))
            try:
                atomic_write(os.path.join(out_dir, fname), text)
                if action == "open":
                    sys.stderr.write(f"graph-snapshotter: epoch {fname} "
                                     f"({len(nodes)} nodes, walk {walk_ms:.0f} ms)\n")
            except OSError as exc:  # disk trouble: keep observing, retry next tick
                sys.stderr.write(f"graph-snapshotter: write failed ({exc}); retrying next tick\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
