#!/usr/bin/env python3
"""ros2-bag-player latch pre-pass: restore the transient-local ("latched") topics a WINDOWED
replay would otherwise lose (the frozen contract — rig-replay-window-handoff.md §1.2).

rosbag2's ``--start-offset`` skips every message recorded before the offset, latched ones
included: a replay of a planner from t=30 runs without the ``/tf_static`` its source run
published at t=0 — a silently broken experiment. So, before ``play.sh`` execs the windowed
``ros2 bag play``, this tool (started in the background, same container, dies with it):

  1. reads the session's ``metadata.yaml`` and keeps the topics whose EVERY offered QoS profile
     is ``transient_local`` (rosbag2 itself adapts the play publisher to the recorded offers and
     falls back to volatile when they disagree — a topic the main play would not latch is not
     restored either);
  2. intersects them with the SELECTION the main play uses — the same ``--topics …`` /
     ``--exclude-regex …`` argv play.sh passes to ``ros2 bag play`` — so it can NEVER publish a
     topic the main play would not (rig's self-echo subtraction is baked into that selection and
     holds for latches too);
  3. reads the bag with a ``StorageFilter`` on that topic set, stopping at the window start
     (``bag_start + from``), and keeps the LAST message per topic with a receive stamp before it
     (a topic with no message before ``from`` publishes nothing);
  4. publishes each once with the RECORDED QoS (transient-local, the recorded reliability, depth
     1) from a node grouped under the player instance — ``/<name>/latch_restore`` (rig's epoch
     reader groups by instance namespace) — touches the ready marker play.sh waits on, and STAYS
     ALIVE for the session: durability for late-joining subscribers is the whole point of
     transient-local, and a publish-and-exit would lose it.

No ``/clock`` is published here. Not started at ``from == 0`` (play.sh's decision — the
unwindowed play.sh is unchanged). Split like the siblings: a pure core (metadata -> latched topic
set, selection intersection, QoS mapping — unit-tested in ``../tests/`` without ROS) and a thin
rclpy + rosbag2_py shell. rclpy imports lazily so the core stays importable on a dev box.
"""
from __future__ import annotations

import os
import re
import sys
import time

import yaml

READY_PATH = "/tmp/latch_restore.ready"

# ---------------------------------------------------------------------------------------------
# Pure core — metadata -> latched topics; selection intersection; QoS mapping. No I/O, no ROS.
# ---------------------------------------------------------------------------------------------


def latched_topics(metadata: dict) -> list[dict]:
    """The recording's transient-local topics: [{name, type, reliability}] for every topic whose
    offered QoS profiles ALL say durability transient_local (rosbag2 plays a topic with mixed
    offers as volatile — mirrored here, so nothing is restored that the main play would not
    latch). `reliability` is the first profile's (rosbag2's own adaptation rule). Topics with no
    offered profile recorded are volatile by definition."""
    info = (metadata or {}).get("rosbag2_bagfile_information") or {}
    out: list[dict] = []
    for entry in info.get("topics_with_message_count") or []:
        tm = (entry or {}).get("topic_metadata") or {}
        profiles = tm.get("offered_qos_profiles") or []
        if isinstance(profiles, str):  # pre-Iron metadata: one YAML string
            try:
                profiles = yaml.safe_load(profiles) or []
            except yaml.YAMLError:
                profiles = []
        if not profiles or not all(str((p or {}).get("durability", "")).lower()
                                   in ("transient_local", "2") for p in profiles):
            continue
        rel = str((profiles[0] or {}).get("reliability", "reliable")).lower()
        out.append({"name": str(tm.get("name")), "type": str(tm.get("type")),
                    "reliability": "best_effort" if rel in ("best_effort", "2") else "reliable"})
    return out


def parse_selector(argv: list[str]) -> tuple[list[str] | None, str | None]:
    """The main play's selector argv -> (allow-list | None, exclude-regex | None): exactly the
    two shapes play_cmd emits (`--topics t…` greedy, or `--exclude-regex R`), nothing else.
    Neither = the main play publishes everything."""
    if not argv:
        return None, None
    if argv[0] == "--topics" and len(argv) > 1 and not any(a.startswith("-") for a in argv[1:]):
        return list(argv[1:]), None
    if argv[0] == "--exclude-regex" and len(argv) == 2:
        return None, argv[1]
    raise SystemExit(f"latch_restore: unexpected selector argv {argv!r} — play_cmd emits "
                     "`--topics t…` or `--exclude-regex R`")


def select(latched: list[dict], allow: list[str] | None, exclude: str | None) -> list[dict]:
    """latched ∩ the main play's selection. Allow mode: exact names (play.exclude already pruned
    the list in play_cmd). Exclude mode: drop names the alternation FULLY matches — pinned live
    on lyrical (mcap storage): `-x tick` does NOT exclude `/toy/tick`, `-x /toy/tick` and
    `-x .*tick` do, i.e. rosbag2's exclude is `std::regex_match`, and mirroring it exactly is
    what keeps the restored set identical to the played set (a search would silently
    under-restore a latched topic the main play still publishes). NOTE the asymmetry with
    play_cmd's allow-mode `play.exclude` filtering, which searches — pre-existing v1.8.0
    behavior, documented in the README."""
    if allow is not None:
        keep = set(allow)
        return [t for t in latched if t["name"] in keep]
    if exclude:
        pat = re.compile(exclude)
        return [t for t in latched if not pat.fullmatch(t["name"])]
    return list(latched)


def window_start_ns(bag_start_ns: int, from_s: float) -> int:
    """The window's first stamp: messages with recv stamp < this are pre-window (ours to restore),
    >= this are the main play's (`--start-offset` seeks there)."""
    return int(bag_start_ns) + int(round(float(from_s) * 1e9))


def last_before(messages, cutoff_ns: int) -> dict:
    """{topic: (stamp_ns, data)} — the last message per topic with stamp < cutoff, over a stream
    of (topic, data, stamp_ns) in receive order; stops at the first stamp >= cutoff (the stream is
    ordered, so nothing later can be earlier)."""
    last: dict = {}
    for topic, data, t_ns in messages:
        if t_ns >= cutoff_ns:
            break
        last[topic] = (t_ns, data)
    return last


# ---------------------------------------------------------------------------------------------
# Shell — rosbag2_py reader, rclpy publishers, the ready marker, the resident loop.
# ---------------------------------------------------------------------------------------------

def read_last(session: str, topics: list[str], cutoff_ns: int) -> dict:
    import rosbag2_py
    reader = rosbag2_py.SequentialReader()
    # end_time_ns bounds the storage read at the window; the loop's own cutoff is the guarantee.
    reader.open(rosbag2_py.StorageOptions(uri=session, storage_id="", end_time_ns=cutoff_ns),
                rosbag2_py.ConverterOptions(input_serialization_format="cdr",
                                            output_serialization_format="cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=topics))

    def stream():
        while reader.has_next():
            topic, data, recv_ns, _send_ns = reader.read_next_ext()
            yield topic, data, recv_ns
    return last_before(stream(), cutoff_ns)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ros2-bag-player latch pre-pass")
    ap.add_argument("session", help="/replay/bags/<logger>/<session>")
    ap.add_argument("--from", dest="from_s", type=float, required=True,
                    help="window start, seconds from bag start (must be > 0)")
    ap.add_argument("--topics", nargs="+", help="the main play's allow-list")
    ap.add_argument("--exclude-regex", help="the main play's exclude alternation")
    a = ap.parse_args()
    if a.topics and a.exclude_regex:
        raise SystemExit("latch_restore: --topics and --exclude-regex together — play_cmd never "
                         "emits both")
    if a.from_s <= 0:
        raise SystemExit("latch_restore: --from must be > 0 (play.sh does not start the pre-pass "
                         "at 0)")
    name = re.sub(r"[^A-Za-z0-9_]", "_", os.environ.get("BAG_PLAYER_NAME") or "bag_player")

    def ready():
        with open(READY_PATH, "w") as f:
            f.write("ok\n")

    meta_path = os.path.join(a.session, "metadata.yaml")
    try:
        with open(meta_path) as f:
            metadata = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"latch_restore: cannot read {meta_path}: {exc}")
    bag_start_ns = int(metadata["rosbag2_bagfile_information"]["starting_time"]
                       ["nanoseconds_since_epoch"])
    cutoff = window_start_ns(bag_start_ns, a.from_s)
    wanted = select(latched_topics(metadata), a.topics, a.exclude_regex)
    if not wanted:
        sys.stderr.write("latch-restore: no transient-local topic in the selection — nothing to "
                         "restore\n")
        ready()
        return 0

    t0 = time.monotonic()
    last = read_last(a.session, [t["name"] for t in wanted], cutoff)
    read_ms = (time.monotonic() - t0) * 1000
    have = [t for t in wanted if t["name"] in last]
    missing = [t["name"] for t in wanted if t["name"] not in last]
    if missing:
        sys.stderr.write(f"latch-restore: no pre-window message on {' '.join(missing)} — "
                         "nothing to restore there\n")
    if not have:
        ready()
        return 0

    import rclpy
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    rclpy.init()
    node = rclpy.create_node("latch_restore", namespace="/" + name)
    pubs = []
    for t in have:
        try:
            cls = get_message(t["type"])
        except Exception as exc:  # noqa: BLE001 — ImportError/AttributeError/ValueError
            sys.stderr.write(f"latch-restore: {t['name']} type {t['type']!r} is not installed "
                             f"in this image ({exc}) — skipped (the main play would fail on it "
                             "too; declare the package in the owning service's rigging `msgs:` "
                             "block)\n")
            continue
        qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         reliability=ReliabilityPolicy.BEST_EFFORT
                         if t["reliability"] == "best_effort" else ReliabilityPolicy.RELIABLE)
        pub = node.create_publisher(cls, t["name"], qos)
        stamp_ns, data = last[t["name"]]
        pub.publish(deserialize_message(data, cls))
        pubs.append(pub)
        sys.stderr.write(f"latch-restore: {t['name']} <- last pre-window sample at "
                         f"t={(stamp_ns - bag_start_ns) / 1e9:.3f}s ({t['type']}, "
                         f"{t['reliability']})\n")
    ready()
    sys.stderr.write(f"latch-restore: {len(pubs)} latched topic(s) restored before from="
                     f"{a.from_s:g}s (bag read {read_ms:.0f} ms) — staying alive for the "
                     "session\n")
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
