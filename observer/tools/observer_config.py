#!/usr/bin/env python3
"""Validate observer configuration and emit deterministic launcher environment.

Native host collection belongs only to ``observer-up up``. Rendering may create a
clearly marked cache placeholder, but never inspects the render host or replaces
existing runtime evidence. Image inspection and control verbs do not write files.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

import yaml

COMPONENTS = ("cpu", "memory", "storage", "temperature", "gpu", "power", "host", "recording", "collectors")
PLACEHOLDER = {
    "schema_version": 1, "scope": "host", "status": "unavailable", "generated": True,
    "collected_at": None, "boot_id": None,
    "error": "Native host evidence is unavailable. observer-up up refreshes this runtime cache; config/bake never probes the host.",
    "probes": {},
}


def _block(cfg, name, allowed):
    value = cfg.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"unknown {name} settings: {', '.join(sorted(map(str, unknown)))}")
    return value


def _boolean(block, key, default, label):
    value = block.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{label}.{key} must be true or false")
    return value


def _number(block, key, default, label, zero=False, minimum=None):
    value = block.get(key, default)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f"{label}.{key} must be finite and {'nonnegative' if zero else 'positive'}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label}.{key} must be at least {minimum:g} seconds")
    return format(value, "g")


def _thresholds(value):
    if not isinstance(value, dict):
        raise ValueError("diagnostics.thresholds must be a mapping")
    result = {}
    for metric, setting in value.items():
        if not isinstance(metric, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", metric):
            raise ValueError("diagnostics threshold keys must be metric names")
        if not isinstance(setting, dict) or set(setting) - {"warn", "error", "direction"} or not ({"warn", "error"} & set(setting)):
            raise ValueError(f"threshold {metric} needs warn and/or error, with optional direction")
        row = {"direction": setting.get("direction", "above")}
        if row["direction"] not in ("above", "below"):
            raise ValueError(f"threshold {metric}.direction must be above or below")
        for severity in ("warn", "error"):
            if severity not in setting:
                continue
            number = setting[severity]
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError(f"threshold {metric}.{severity} must be finite")
            row[severity] = number
        if "warn" in row and "error" in row:
            if (row["direction"] == "above" and row["error"] < row["warn"]
                    or row["direction"] == "below" and row["error"] > row["warn"]):
                raise ValueError(f"threshold {metric} error must be more severe than warn")
        result[metric] = row
    return result


def config_env(cfg, repo, sim_time=None):
    """Pure normalized environment: inherited observer settings never affect config."""
    if not isinstance(cfg, dict):
        raise ValueError("observer config must be a mapping")
    if cfg.get("service", "observer") != "observer":
        raise ValueError("service must be observer")
    name = cfg.get("name", "observer")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise ValueError("name must be a ROS identifier: start with a letter, then letters, numbers or underscores")
    graph = _block(cfg, "graph", {"enabled", "interval_s", "settle_s", "endpoints"})
    system = _block(cfg, "system", {"enabled", "interval_s", "settings_interval_s"})
    recording = _block(cfg, "recording", {"enabled"})
    diagnostics = _block(cfg, "diagnostics", {"enabled", "interval_s", "stale_after_s", "topic", "components", "thresholds", "publish_during_replay"})
    graph_on = _boolean(graph, "enabled", True, "graph")
    system_on = _boolean(system, "enabled", True, "system")
    recording_on = _boolean(recording, "enabled", True, "recording")
    diagnostics_on = _boolean(diagnostics, "enabled", True, "diagnostics")
    replay_optin = _boolean(diagnostics, "publish_during_replay", False, "diagnostics")
    if diagnostics_on and not system_on:
        raise ValueError("diagnostics.enabled requires system.enabled to collect live measurements")
    if graph_on and not recording_on:
        raise ValueError("graph.enabled requires recording.enabled; graph snapshots are disk recordings")
    if not graph_on and not system_on:
        raise ValueError("observer needs graph.enabled or system.enabled")
    topic = diagnostics.get("topic", "/diagnostics")
    if not isinstance(topic, str) or not re.fullmatch(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+", topic):
        raise ValueError("diagnostics.topic must be an absolute ROS topic name")
    components = diagnostics.get("components", list(COMPONENTS))
    if (not isinstance(components, list) or not components
            or any(not isinstance(item, str) or item not in COMPONENTS for item in components)
            or len(set(components)) != len(components)):
        raise ValueError("diagnostics.components must be a nonempty list of unique supported components")
    thresholds = _thresholds(diagnostics.get("thresholds", {}))
    is_replay = str(sim_time or "").strip().lower() in {"1", "true", "yes", "on"}
    native_dir = Path(repo).resolve() / "var" / "run" / name / "native"
    native_source = native_dir / "native-host.json"
    boolean = lambda value: str(value).lower()
    return {
        "OBSERVER_NAME": name,
        "OBSERVER_RECORDING_ENABLED": boolean(recording_on),
        "OBSERVER_DIAGNOSTICS_ENABLED": boolean(diagnostics_on and (not is_replay or replay_optin)),
        "OBSERVER_STATE_DIR": "/run/observer",
        "OBSERVER_NATIVE_PROBE_SOURCE": str(native_source),
        "OBSERVER_NATIVE_PROBE_DIR": str(native_dir),
        "OBSERVER_NATIVE_PROBE_FILE": "/native-host/native-host.json",
        "GRAPH_SNAPSHOT_ENABLED": boolean(graph_on),
        "GRAPH_SNAPSHOT_ENDPOINTS_ENABLED": boolean(_boolean(graph, "endpoints", True, "graph")),
        "GRAPH_SNAPSHOT_NAME": name,
        "GRAPH_SNAPSHOT_INTERVAL_S": _number(graph, "interval_s", 60, "graph"),
        "GRAPH_SNAPSHOT_SETTLE_S": _number(graph, "settle_s", 2, "graph", zero=True),
        "SYSTEM_SNAPSHOT_ENABLED": boolean(system_on),
        "SYSTEM_SNAPSHOT_NAME": name,
        "SYSTEM_SNAPSHOT_INTERVAL_S": _number(system, "interval_s", 1, "system", minimum=0.1),
        "SYSTEM_SNAPSHOT_SETTINGS_INTERVAL_S": _number(system, "settings_interval_s", 30, "system", minimum=1),
        "OBSERVER_DIAGNOSTICS_TOPIC": topic,
        "OBSERVER_DIAGNOSTICS_INTERVAL_S": _number(diagnostics, "interval_s", 1, "diagnostics"),
        "OBSERVER_DIAGNOSTICS_STALE_AFTER_S": _number(diagnostics, "stale_after_s", 5, "diagnostics"),
        "OBSERVER_DIAGNOSTICS_COMPONENTS": json.dumps(components, separators=(",", ":")),
        "OBSERVER_DIAGNOSTICS_THRESHOLDS": json.dumps(thresholds, sort_keys=True, separators=(",", ":")),
        "OBSERVER_DIAGNOSTICS_INSTANCE": name + ("_replay_host" if is_replay else ""),
        "OBSERVER_DIAGNOSTICS_SCOPE": "replay_host" if is_replay else "live_host",
    }


def ensure_placeholder(path, reset=False):
    path = Path(path)
    if path.exists() and not reset:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".native-host-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(PLACEHOLDER, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
        if reset:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("repo")
    parser.add_argument("--mode", choices=("up", "config", "inspect", "control"), default="config")
    args = parser.parse_args(argv)
    try:
        cfg = yaml.safe_load(Path(args.config).read_text())
        env = config_env(cfg, args.repo, os.environ.get("RIG_SIM_TIME"))
        if args.mode in ("up", "config"):
            reset = args.mode == "up" and env["SYSTEM_SNAPSHOT_ENABLED"] == "true"
            try:
                ensure_placeholder(env["OBSERVER_NATIVE_PROBE_SOURCE"], reset=reset)
            except OSError as exc:
                cache_dir = Path(env["OBSERVER_NATIVE_PROBE_DIR"])
                if args.mode != "up" or not cache_dir.is_dir() or not os.access(cache_dir, os.R_OK | os.X_OK):
                    raise OSError(f"native cache directory must exist and be readable for the observer bind mount: {cache_dir}: {exc}") from exc
                # An existing directory remains a usable read-only bind even if
                # this launch cannot refresh it. Preserve original timestamps;
                # the worker reports missing/stale evidence instead of claiming
                # that this launch made a new native observation.
                print(f"observer-config: native cache refresh unavailable; retaining existing evidence with its original timestamp: {exc}",
                      file=sys.stderr)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"observer-config: {exc}", file=sys.stderr)
        return 2
    for key, value in env.items():
        print(f"export {key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
