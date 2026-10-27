"""Bounded ROS endpoint readiness for coordinated replay; never reads Compose state.

The pure validator/evaluator/waiter work without ROS. Runtime imports are lazy, service
clients only check discovery (no requests are sent), and transport probes are separate.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ipaddress
import json
import math
import os
import re
import socket
import sys
import time


MAX_REQUEST_BYTES = 256 * 1024
NAME = r"/(?:[A-Za-z_][A-Za-z_0-9]*/)*[A-Za-z_][A-Za-z_0-9]*"


def validate_requirements(value):
    fields = {"version", "subscriptions", "services", "transports", "required_instances", "unsupported"}
    if not isinstance(value, dict) or set(value) != fields or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("readiness requirements must contain exactly the v1 fields")
    if any(not isinstance(value[key], list) or len(value[key]) > 2048 for key in fields - {"version"}):
        raise ValueError("readiness collections must be bounded lists")
    required = value["required_instances"]
    if any(not isinstance(n, str) or not n for n in required) or len(set(required)) != len(required):
        raise ValueError("required_instances must contain unique nonempty names")
    for key, name, kind in (("subscriptions", "topic", "msg"), ("services", "service", "srv")):
        for entry in value[key]:
            if not isinstance(entry, dict) or set(entry) != {"instance", name, "type", "node"}:
                raise ValueError(f"{key} require instance, {name}, type, and exact node")
            if not isinstance(entry["instance"], str) or not entry["instance"]:
                raise ValueError("readiness instance must be nonempty")
            if any(not isinstance(entry[field], str) or not re.fullmatch(NAME, entry[field]) for field in (name, "node")):
                raise ValueError("readiness endpoints and nodes must be absolute ROS names")
            if not isinstance(entry["type"], str) or not re.fullmatch(
                    rf"[A-Za-z_][A-Za-z_0-9]*/{kind}/[A-Za-z_][A-Za-z_0-9]*", entry["type"]):
                raise ValueError(f"readiness {key} require an exact {kind} type")
    for entry in value["transports"]:
        if not isinstance(entry, dict) or set(entry) != {"instance", "host", "port"}:
            raise ValueError("transport readiness requires instance, host, port")
        if not isinstance(entry["instance"], str) or not entry["instance"]:
            raise ValueError("transport instance must be nonempty")
        if not isinstance(entry["host"], str):
            raise ValueError("transport host must be a literal IP address")
        address = ipaddress.ip_address(entry["host"])
        if address.is_unspecified or address.is_multicast:
            raise ValueError("transport probe requires a reachable unicast IP, not a listen wildcard")
        if type(entry["port"]) is not int or not 1 <= entry["port"] <= 65535:
            raise ValueError("transport port must be between 1 and 65535")
    for entry in value["unsupported"]:
        if (not isinstance(entry, dict) or set(entry) != {"instance", "reason"}
                or any(not isinstance(x, str) or not x for x in entry.values())):
            raise ValueError("unsupported readiness entries require instance and reason")
    covered = {entry["instance"] for key in ("subscriptions", "services", "transports", "unsupported")
               for entry in value[key]}
    if set(required) - covered:
        raise ValueError("required consumer has no endpoint proof or explicit unsupported diagnostic")
    return value


def load_requirements(path, *, session_id=None, plan_id=None):
    with open(path, "rb") as stream:
        data = stream.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError("readiness requirements exceed 256 KiB")
    value = json.loads(data)
    if session_id is not None or plan_id is not None:
        if (not session_id or not plan_id or not isinstance(value, dict)
                or value.get("session_id") != session_id or value.get("plan_id") != plan_id):
            raise ValueError("readiness requirements belong to another replay session or plan")
        value = {key: item for key, item in value.items() if key not in ("session_id", "plan_id")}
    return validate_requirements(value)


def evaluate(requirements, observation):
    """Match exact endpoints, with unique node identity and no probe/harness evidence."""
    missing = [dict(instance=x["instance"], kind="unsupported", reason=x["reason"])
               for x in requirements["unsupported"]]
    excluded = set(observation.get("excluded_nodes", []))
    counts = observation.get("nodes", {})
    for key, name in (("subscriptions", "topic"), ("services", "service")):
        for wanted in requirements[key]:
            node = wanted["node"]
            reason = None
            if node in excluded:
                reason = "probe or replay harness node cannot prove consumer readiness"
            elif counts.get(node, 0) != 1:
                reason = "expected node missing" if not counts.get(node, 0) else "duplicate node identity"
            else:
                matches = [entry for entry in observation.get(key, []) if all(
                    entry.get(field) == wanted[field] for field in (name, "type", "node"))]
                if not matches:
                    reason = "expected endpoint name/type/node not discovered"
                elif key == "services" and not any(entry.get("available") is True for entry in matches):
                    reason = "typed service client has no ready server"
            if reason:
                missing.append(dict(instance=wanted["instance"], kind=key, name=wanted[name], node=node, reason=reason))
    for wanted in requirements["transports"]:
        if not any(entry.get("host") == wanted["host"] and entry.get("port") == wanted["port"]
                   and entry.get("available") is True for entry in observation.get("transports", [])):
            missing.append(dict(instance=wanted["instance"], kind="transport",
                                name=f"{wanted['host']}:{wanted['port']}", reason="TCP listener unreachable"))
    return dict(ready=not missing, missing=missing,
                scope="Endpoint discovery and TCP reachability; no application-health or data-delivery claim")


def wait_ready(requirements, observe, *, timeout_s=30, stable_s=.2, poll_s=.05,
               clock=time.monotonic, sleep=time.sleep, cancelled=lambda: False):
    """Wait within a monotonic deadline. `observe(remaining_s)` must itself be bounded.

    Production wraps the process in the coordinator's overall startup deadline too;
    this covers middleware initialization/type imports before the observer exists.
    """
    validate_requirements(requirements)
    if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (timeout_s, stable_s, poll_s))
            or not 0 < timeout_s <= 3600 or not 0 <= stable_s < timeout_s or not 0 < poll_s <= 1):
        raise ValueError("invalid bounded readiness timing")
    start, since, last = clock(), None, {"ready": False, "missing": []}
    deadline = start + timeout_s
    while True:
        if cancelled():
            return dict(last, state="aborted", elapsed_s=clock() - start)
        if requirements["unsupported"]:
            return dict(evaluate(requirements, {}), state="unsupported", elapsed_s=clock() - start)
        remaining = deadline - clock()
        if remaining <= 0:
            return dict(last, ready=False, state="timeout", elapsed_s=clock() - start)
        try:
            last = evaluate(requirements, observe(remaining))
        except Exception as exc:
            return dict(ready=False, missing=[], state="error", error=str(exc), elapsed_s=clock() - start)
        now = clock()
        if now >= deadline:
            return dict(last, ready=False, state="timeout", elapsed_s=now - start)
        if last["ready"]:
            since = now if since is None else since
            if now - since >= stable_s:
                return dict(last, state="ready", elapsed_s=now - start, stable_s=now - since)
        else:
            since = None
        sleep(min(poll_s, deadline - now))


def _fqn(name, namespace):
    return namespace.rstrip("/") + "/" + name


class RosObserver:
    """One rclpy node; no replay data endpoints, requests, or ROS CLI daemon."""

    def __init__(self, requirements, *, name="_rig_replay_readiness", excluded_nodes=()):
        import rclpy
        from rosidl_runtime_py.utilities import get_service
        self.rclpy, self.requirements = rclpy, requirements
        self.node = None
        self.clients = {}
        self.excluded = set(excluded_nodes)
        try:
            service_types = {entry["type"]: get_service(entry["type"]) for entry in requirements["services"]}
            rclpy.init(args=[])
            self.node = rclpy.create_node(name, namespace="/", use_global_arguments=False,
                                         enable_rosout=False, start_parameter_services=False)
            self.excluded.add(_fqn(self.node.get_name(), self.node.get_namespace()))
            for entry in requirements["services"]:
                key = (entry["service"], entry["type"])
                if key not in self.clients:
                    self.clients[key] = self.node.create_client(service_types[entry["type"]], entry["service"])
        except Exception:
            self.close()
            raise

    def close(self):
        if self.node is not None:
            self.node.destroy_node()
            self.node = None
        self.rclpy.try_shutdown()

    def __call__(self, remaining_s):
        deadline = time.monotonic() + remaining_s
        self.rclpy.spin_once(self.node, timeout_sec=0)
        nodes = Counter(_fqn(name, namespace) for name, namespace in self.node.get_node_names_and_namespaces())
        observation = dict(nodes=dict(nodes), excluded_nodes=sorted(self.excluded),
                           subscriptions=[], services=[], transports=[])
        for topic in sorted({entry["topic"] for entry in self.requirements["subscriptions"]}):
            for endpoint in self.node.get_subscriptions_info_by_topic(topic):
                observation["subscriptions"].append(dict(topic=topic, type=endpoint.topic_type,
                    node=_fqn(endpoint.node_name, endpoint.node_namespace)))
        for entry in self.requirements["services"]:
            if nodes[entry["node"]] != 1:
                continue
            namespace, _, name = entry["node"].rpartition("/")
            try:
                services = dict(self.node.get_service_names_and_types_by_node(name, namespace or "/"))
            except Exception:
                # A disappearing node is a missing endpoint, never a successful snapshot.
                if not self.node.context.ok():
                    raise
                continue
            if entry["type"] in services.get(entry["service"], []):
                observation["services"].append(dict(entry, available=self.clients[
                    entry["service"], entry["type"]].service_is_ready()))
        for entry in self.requirements["transports"]:
            remaining = deadline - time.monotonic()
            available = False
            if remaining > 0:
                try:
                    with socket.create_connection((entry["host"], entry["port"]), timeout=min(.2, remaining)):
                        available = True
                except OSError:
                    pass
            observation["transports"].append(dict(entry, available=available))
        return observation


def coordinated(requirements, participant, observe, *, timeout_s=30, stable_s=.2,
                clock=time.monotonic):
    """Transport before arm; stable consumer endpoints after arm, rechecked until commit."""
    validate_requirements(requirements)
    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 3600
            or type(stable_s) not in (int, float) or not math.isfinite(stable_s) or not 0 <= stable_s < timeout_s):
        raise ValueError("invalid bounded readiness timing")
    if requirements["unsupported"]:
        raise ValueError("Unsupported readiness: " + json.dumps(requirements["unsupported"]))
    start, since = clock(), None
    last = {"ready": False, "missing": []}

    def deadline():
        if clock() - start >= timeout_s:
            raise TimeoutError("Consumer readiness timed out: " + json.dumps(last.get("missing", [])))

    def check(transport_only=False):
        nonlocal since, last
        deadline()
        observation = observe(timeout_s - (clock() - start))
        required = requirements if not transport_only else dict(requirements, subscriptions=[], services=[])
        last = evaluate(required, observation)
        deadline()  # A slow observation cannot establish readiness after the deadline.
        if transport_only:
            return last["ready"]
        now = clock()
        since = (now if since is None else since) if last["ready"] else None
        return since is not None and now - since >= stable_s

    participant.wait_release(prepared_check=lambda: check(True), ready_check=check, on_tick=deadline)
    participant.started()
    participant.completed()
    return dict(last, state="ready", elapsed_s=clock() - start)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--stable", type=float, default=.2)
    args = parser.parse_args(argv)
    observer = participant = None
    try:
        # Constructor publishes preparing before potentially slow ROS/type initialization.
        from replay_coordination import Participant, CoordinationAborted
        participant = Participant.from_env("readiness")
        binding = dict(session_id=os.environ.get("RIG_REPLAY_SESSION_ID"),
                       plan_id=os.environ.get("RIG_REPLAY_PLAN_ID")) if participant else {}
        request = load_requirements(args.requirements, **binding)
        if request["unsupported"]:
            result = dict(evaluate(request, {}), state="unsupported")
        else:
            suffix = os.environ.get("RIG_REPLAY_SESSION_ID", "standalone")[:16]
            observer = RosObserver(request, name="_rig_replay_readiness_" + suffix)
            runner = coordinated if participant else wait_ready
            result = (runner(request, participant, observer, timeout_s=args.timeout, stable_s=args.stable)
                      if participant else runner(request, observer, timeout_s=args.timeout, stable_s=args.stable))
        if participant and result["state"] != "ready":
            participant.failed(json.dumps(result))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["state"] == "ready" else 1
    except KeyboardInterrupt:
        if participant:
            participant.aborted("readiness probe interrupted")
        print(json.dumps(dict(ready=False, state="aborted", error="readiness probe interrupted")))
        return 130
    except Exception as exc:
        aborted = participant is not None and isinstance(exc, CoordinationAborted)
        if participant and not aborted:
            participant.failed(str(exc))
        print(json.dumps(dict(ready=False, state="aborted" if aborted else "error", error=str(exc))))
        return 2
    finally:
        if observer is not None:
            observer.close()


if __name__ == "__main__":
    sys.exit(main())
