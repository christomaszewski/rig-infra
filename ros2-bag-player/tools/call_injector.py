#!/usr/bin/env python3
"""ros2-bag-player call injector: execute a schema-v1 call-script YAML (the frozen contract —
rig-replay-calls-handoff.md §1.2) against the LIVE graph's service servers, on the replay clock.
This is the service-call half of SIL replay: recorded calls (exported via
``ros2-bag-player-up <config> export-calls``, then retimed/edited) or brand-new ones fire at
their scripted ``t``; results append into the run dir. rig (>= v0.2.37) activates this container
via the ``calls`` compose profile by exporting ``RIG_REPLAY_CALLS``; it stays schema-opaque about
``request`` — the srv type's own field map is validated HERE, against the type, at load.

Split like ``graph_snapshot.py``: a pure core (script parse/validate/sort + results rendering —
unit-tested in ``../tests/`` without ROS) and a thin rclpy shell (clock waiter, typed caller,
results appender). rclpy imports lazily so the core stays importable on a dev box without ROS.

Time base — one clock doctrine, third consumer: under ``RIG_SIM_TIME=1``, ``t`` counts from the
FIRST ``/clock`` sample this process observes (≈ play start; under ``play.start_offset_s`` the
zero shifts with it), so ``-r`` scales the timeline exactly like the bag. Without it
(``--wall-clock`` replays), ``t`` counts from injector start. The compose gives the injector a
head start (the player ``depends_on`` it, ``required: false``) and rosbag2 needs seconds to open
a bag, so the subscription stands before the first sample; a missing ``/clock`` refuses after
``CALL_INJECTOR_CLOCK_TIMEOUT_S`` (default 60) rather than hanging a replay forever.

Doctrine pins:
  - Type validation refuses at LOAD, before rclpy even initializes — a bad type or a request
    field that does not fit the srv's Request never dies mid-timeline. The refusal names the
    msgs-overlay fix (service types ride the same interface packages as topics).
  - A call NEVER stalls the timeline: per-call ``timeout_s`` bounds every call; a timed-out or
    errored call logs ``ok: false`` and the timeline moves on (later calls can fire late by at
    most the timeout of the call before them — script accordingly).
  - Results: ``{t, service, ok, latency_s, response|error}`` APPENDED per call to
    ``<run>/calls/<name>/results.yaml`` (``<name>`` = the player config's name; flat
    ``calls/<name>`` without a registry). The data root mounts rw and ``current`` resolves ONCE
    at start — record.sh doctrine verbatim. Every entry is one self-contained ``- {...}`` item
    written with a single O_APPEND write, so the file stays parseable YAML through a
    mid-timeline SIGTERM. ``response`` is capped at 4 KB (``truncated: true`` degrades it to a
    clipped YAML string — results are a log, not a bag; the replay run's own service events are
    the full record when the live server has introspection on).
  - Exit 0 = the script is exhausted (a FINISHED timeline, the player exit-semantics doctrine);
    interrupted (SIGTERM/SIGINT mid-timeline) exits 1. ``latency_s`` is WALL time (a server
    performance measure, deliberately not scaled by ``-r``); actual fire offsets go to stderr.
"""
from __future__ import annotations

import os
import re
import sys
import time

import yaml

# ---------------------------------------------------------------------------------------------
# Pure core — script text -> validated, t-sorted call list; result -> one appendable YAML item.
# No I/O, no ROS.
# ---------------------------------------------------------------------------------------------

SCRIPT_KEYS = {"schema", "timeout_s", "calls"}
CALL_KEYS = {"t", "service", "type", "request", "timeout_s"}
TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*/srv/[A-Za-z][A-Za-z0-9_]*$")
RESPONSE_CAP = 4096          # bytes of serialized response kept per results entry
DEFAULT_TIMEOUT_S = 5.0


def _timeout(val, where: str) -> float:
    try:
        t = float(val)
    except (TypeError, ValueError):
        raise SystemExit(f"call_injector: {where} must be a number of seconds")
    if t <= 0:
        raise SystemExit(f"call_injector: {where} must be > 0")
    return t


def load_script(doc) -> list[dict]:
    """The YAML-loaded script -> calls SORTED by `t` (stable — equal stamps keep author order;
    authors may append out of order), each with its effective per-call timeout_s filled in.
    STRICT schema v1: unknown keys fail loudly, on this side exactly as on rig's."""
    if not isinstance(doc, dict):
        raise SystemExit("call_injector: the call script must be a YAML mapping "
                         "(schema: 1, calls: [...])")
    unknown = set(map(str, doc)) - SCRIPT_KEYS
    if unknown:
        raise SystemExit(f"call_injector: unknown script key(s): {', '.join(sorted(unknown))} "
                         f"(schema v1 keys: {', '.join(sorted(SCRIPT_KEYS))})")
    if doc.get("schema") != 1:
        raise SystemExit(f"call_injector: schema must be 1 (got {doc.get('schema')!r}) — this "
                         "injector speaks call-script schema v1 only")
    default_timeout = _timeout(doc.get("timeout_s", DEFAULT_TIMEOUT_S), "timeout_s")
    raw = doc.get("calls")
    if not isinstance(raw, list):
        raise SystemExit("call_injector: `calls` must be a list of call entries")
    calls: list[dict] = []
    for i, c in enumerate(raw):
        where = f"calls[{i}]"
        if not isinstance(c, dict):
            raise SystemExit(f"call_injector: {where} must be a mapping "
                             "{t, service, type, request[, timeout_s]}")
        unknown = set(map(str, c)) - CALL_KEYS
        if unknown:
            raise SystemExit(f"call_injector: unknown {where} key(s): "
                             f"{', '.join(sorted(unknown))} "
                             f"(call keys: {', '.join(sorted(CALL_KEYS))})")
        try:
            t = float(c["t"])          # missing -> KeyError, non-numeric -> ValueError/TypeError
        except (KeyError, TypeError, ValueError):
            raise SystemExit(f"call_injector: {where}.t must be a number of seconds >= 0")
        if t < 0:
            raise SystemExit(f"call_injector: {where}.t must be >= 0 (got {t:g})")
        service = c.get("service")
        if not isinstance(service, str) or not service.startswith("/"):
            raise SystemExit(f"call_injector: {where}.service must be an absolute service name "
                             f"(got {service!r})")
        typ = c.get("type")
        if not isinstance(typ, str) or not TYPE_RE.match(typ):
            raise SystemExit(f"call_injector: {where}.type must be pkg/srv/Name "
                             f"(got {typ!r})")
        request = c.get("request")
        if request is None:
            request = {}
        if not isinstance(request, dict):
            raise SystemExit(f"call_injector: {where}.request must be a mapping — the srv "
                             "type's own field map")
        timeout = _timeout(c["timeout_s"], f"{where}.timeout_s") if "timeout_s" in c \
            else default_timeout
        calls.append({"t": t, "service": service, "type": typ, "request": request,
                      "timeout_s": timeout})
    calls.sort(key=lambda c: c["t"])   # list.sort is stable
    return calls


def plain(obj):
    """Recursively strip ROS-message containers (OrderedDict, array.array, numpy arrays/scalars,
    bytes) down to what yaml.safe_dump accepts."""
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if isinstance(obj, bytes):
        return list(obj)
    if hasattr(obj, "tolist"):         # numpy arrays/scalars, array.array
        return plain(obj.tolist())
    return obj


def cap_response(response: dict) -> tuple[object, bool]:
    """(response as stored, truncated?): the response's own YAML serialization when it fits the
    4 KB cap; otherwise the serialization's first 4 KB as ONE quoted string — the cap must never
    leave a half-open YAML block, the file has to stay parseable."""
    text = yaml.safe_dump(response, default_flow_style=False, sort_keys=False)
    data = text.encode("utf-8")
    if len(data) <= RESPONSE_CAP:
        return response, False
    return data[:RESPONSE_CAP].decode("utf-8", errors="ignore"), True


def render_result(t: float, service: str, ok: bool, latency_s: float,
                  response=None, error=None) -> str:
    """One results.yaml list item (§1.2: {t, service, ok, latency_s, response|error}) as
    self-contained YAML text — appending any sequence of these items yields one parseable
    top-level list."""
    entry: dict = {"t": float(t), "service": service, "ok": bool(ok),
                   "latency_s": round(float(latency_s), 4)}
    if ok:
        stored, truncated = cap_response(plain(response if response is not None else {}))
        entry["response"] = stored
        if truncated:
            entry["truncated"] = True
    else:
        entry["error"] = str(error)
    return yaml.safe_dump([entry], default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------------------------
# Shell — typed validation at load, clock waiter, caller, results appender.
# ---------------------------------------------------------------------------------------------

def resolve_out_dir(root: str, name: str) -> str:
    """record.sh's resolve-`current`-once block with `calls` in place of `bags` — called ONCE at
    process start; the result is pinned for the container's life (a symlink flip mid-run must
    not retarget the writer)."""
    cur = os.path.join(root, "current")
    base = os.path.join(os.path.realpath(cur), "calls") if os.path.exists(cur) \
        else os.path.join(root, "calls")
    return os.path.join(base, name)


def resolve_types(calls: list[dict]):
    """Import every call's srv type and build its Request — the at-LOAD refusal (never
    mid-timeline), with the msgs-overlay hint. Returns {type: srv_class}; each call grows a
    `_req` ready to send."""
    from rosidl_runtime_py.set_message import set_message_fields
    from rosidl_runtime_py.utilities import get_service
    types: dict = {}
    for c in calls:
        typ = c["type"]
        if typ not in types:
            try:
                types[typ] = get_service(typ)
            except Exception as exc:  # noqa: BLE001 — ImportError/AttributeError/ValueError
                raise SystemExit(
                    f"call_injector: srv type {typ!r} (t={c['t']:g} -> {c['service']}) is not "
                    f"installed in this image ({exc}). Service types ride the same interface "
                    "packages as topics: declare the package in the owning service's rigging "
                    "`msgs:` block so the fleet-ros-msgs overlay carries it (rig-infra msgs/), "
                    "or point BAG_PLAYER_IMAGE at an image that has it.")
        req = types[typ].Request()
        try:
            set_message_fields(req, c["request"])
        except Exception as exc:  # noqa: BLE001 — field name/value mismatches
            raise SystemExit(f"call_injector: request for t={c['t']:g} {c['service']} does not "
                             f"fit {typ}.Request: {exc}")
        c["_req"] = req
    return types


def main() -> int:
    script_path = sys.argv[1] if len(sys.argv) > 1 else "/calls.yaml"
    name = os.environ.get("CALL_INJECTOR_NAME") or "bag_player"
    sim = str(os.environ.get("RIG_SIM_TIME") or "") == "1"
    clock_timeout = float(os.environ.get("CALL_INJECTOR_CLOCK_TIMEOUT_S") or 60)
    root = os.environ.get("RIG_BAG_ROOT") or "/data"

    try:
        with open(script_path) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"call_injector: cannot load call script {script_path}: {exc}")
    calls = load_script(doc)
    types = resolve_types(calls)       # the at-LOAD refusal — before init, before the first call

    out_dir = resolve_out_dir(root, name)
    os.makedirs(out_dir, exist_ok=True)
    results = os.path.join(out_dir, "results.yaml")

    import rclpy  # lazy: the pure core above stays importable (testable) without ROS
    from rclpy.executors import ExternalShutdownException
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rosidl_runtime_py.convert import message_to_ordereddict

    rclpy.init()
    node = rclpy.create_node("call_injector_" + re.sub(r"[^A-Za-z0-9_]", "_", name))
    sys.stderr.write(f"call-injector: {len(calls)} call(s) from {script_path} -> {results} "
                     f"(clock={'sim' if sim else 'wall'})\n")

    # Clients up front, before the clock wait — service matching warms up while we wait.
    clients: dict = {}
    for c in calls:
        key = (c["service"], c["type"])
        if key not in clients:
            clients[key] = node.create_client(types[c["type"]], c["service"])

    latest = {"clock": None}
    if sim:
        from rosgraph_msgs.msg import Clock

        def _on_clock(msg, latest=latest):
            latest["clock"] = msg.clock.sec + msg.clock.nanosec * 1e-9
        # BEST_EFFORT matches both reliable and best-effort /clock publishers.
        node.create_subscription(Clock, "/clock", _on_clock,
                                 QoSProfile(depth=10,
                                            reliability=ReliabilityPolicy.BEST_EFFORT))

    interrupted = False
    fd = os.open(results, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        # --- the zero: first /clock sample (sim) or injector start (wall) --------------------
        if sim:
            deadline = time.monotonic() + clock_timeout
            while latest["clock"] is None:
                if time.monotonic() > deadline:
                    raise SystemExit(f"call_injector: no /clock sample within "
                                     f"{clock_timeout:g}s — is the player up with "
                                     "RIG_SIM_TIME=1 (--clock)?")
                rclpy.spin_once(node, timeout_sec=0.05)
            t0 = latest["clock"]
            sys.stderr.write(f"call-injector: first /clock sample {t0:.3f} — t=0 pinned\n")

            def now() -> float:
                return latest["clock"] - t0
        else:
            start = time.monotonic()

            def now() -> float:
                return time.monotonic() - start

        # --- the timeline --------------------------------------------------------------------
        for c in calls:
            while now() < c["t"]:
                rclpy.spin_once(node, timeout_sec=min(0.05, max(0.001, c["t"] - now())))
            client, sent = clients[(c["service"], c["type"])], time.monotonic()
            fired = now()
            future = client.call_async(c["_req"])
            while not future.done() and time.monotonic() - sent < c["timeout_s"]:
                rclpy.spin_once(node, timeout_sec=0.05)
            latency = time.monotonic() - sent
            if future.done():
                try:
                    entry = render_result(c["t"], c["service"], True, latency,
                                          response=message_to_ordereddict(future.result()))
                    outcome = "ok"
                except Exception as exc:  # noqa: BLE001 — the server raised
                    entry = render_result(c["t"], c["service"], False, latency,
                                          error=f"{type(exc).__name__}: {exc}")
                    outcome = "error"
            else:
                future.cancel()
                gone = "" if client.service_is_ready() else " (no server up for this service)"
                entry = render_result(c["t"], c["service"], False, latency,
                                      error=f"timeout after {c['timeout_s']:g}s{gone}")
                outcome = "timeout"
            os.write(fd, entry.encode("utf-8"))   # ONE append per entry: parseable at any cut
            sys.stderr.write(f"call-injector: t={c['t']:g} {c['service']} fired at {fired:.3f} "
                             f"-> {outcome} in {latency:.3f}s\n")
    except (KeyboardInterrupt, ExternalShutdownException):
        interrupted = True
    finally:
        os.close(fd)
        node.destroy_node()
        rclpy.try_shutdown()
    if interrupted:
        sys.stderr.write("call-injector: interrupted before the timeline finished\n")
        return 1
    sys.stderr.write("call-injector: timeline exhausted — a finished injection, not a failure\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
