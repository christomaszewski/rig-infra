#!/usr/bin/env python3
"""Publish cached observer measurements as standard ROS diagnostics.

The pure mapping below imports no ROS and performs no hardware queries. Cache sample
IDs and monotonic timestamps remain authoritative: publishing a heartbeat never
makes an old measurement fresh. ROS initialization/cleanup stays in this separate
worker, bounded by run_observer's process-group shutdown deadline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import uuid

OK, WARN, ERROR, STALE = 0, 1, 2, 3
COMPONENTS = ("cpu", "memory", "storage", "temperature", "gpu", "power", "host", "recording", "collectors")


def mapping(value):
    return value if isinstance(value, dict) else {}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def observed(record):
    record = mapping(record)
    return record.get("value") if record.get("status") == "ok" else None


def keypart(text):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(text)).strip("_").lower() or "unknown"


def scalar(value):
    """ROS KeyValue strings; callers never receive NaN, infinity, or nested blobs."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".12g") if math.isfinite(value) else None
    return value if isinstance(value, str) else None


def validate_thresholds(value):
    if not isinstance(value, dict):
        raise ValueError("diagnostics thresholds must be an object")
    for key, rule in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", key):
            raise ValueError("invalid threshold metric key")
        if not isinstance(rule, dict) or set(rule) - {"warn", "error", "direction"}:
            raise ValueError(f"invalid threshold rule for {key}")
        if not ({"warn", "error"} & set(rule)):
            raise ValueError(f"threshold for {key} needs warn or error")
        direction = rule.get("direction", "above")
        if direction not in ("above", "below"):
            raise ValueError(f"invalid threshold direction for {key}")
        for level in ("warn", "error"):
            if level in rule and (isinstance(rule[level], bool) or
                                  not isinstance(rule[level], (int, float)) or number(rule[level]) is None):
                raise ValueError(f"{key}.{level} must be a finite number")
        if "warn" in rule and "error" in rule:
            if (direction == "above" and rule["warn"] > rule["error"]) or (
                    direction == "below" and rule["warn"] < rule["error"]):
                raise ValueError(f"threshold severities are reversed for {key}")
    return value


def threshold_level(values, thresholds):
    level, reasons = OK, []
    for key, rule in thresholds.items():
        value = number(values.get(key))
        if value is None:
            continue
        for name, severity in (("error", ERROR), ("warn", WARN)):
            if name not in rule:
                continue
            exceeded = value >= rule[name] if rule.get("direction", "above") == "above" else value <= rule[name]
            if exceeded:
                level = max(level, severity)
                reasons.append(f"{key}={value:g} ({name})")
                break
    return level, "; ".join(reasons)


@dataclass
class Observation:
    values: dict = field(default_factory=dict)
    availability: str = "current"
    message: str = "OK"
    level: int = OK
    source: str = "sample"


def missing(message, *, values=None, unsupported=False, retrying=False, source="sample"):
    return Observation(values or {}, "unsupported" if unsupported else "retrying" if retrying else "unavailable",
                       message, OK if unsupported else STALE, source)


def cpu_observation(metrics):
    cpu = mapping(metrics.get("cpu"))
    utilization = mapping(cpu.get("utilization"))
    values = {}
    for core, record in utilization.items():
        record = mapping(record)
        busy = number(record.get("busy_percent")) if record.get("status") == "ok" else None
        if busy is not None:
            values["cpu.utilization_pct" if core == "cpu" else f"cpu.{keypart(core)}.utilization_pct"] = busy
    load = mapping(observed(metrics.get("load")))
    for source, target in (("one", "1m"), ("five", "5m"), ("fifteen", "15m")):
        value = number(load.get(source))
        if value is not None:
            values[f"system.load_{target}"] = value
    if "cpu.utilization_pct" not in values:
        warming = mapping(utilization.get("cpu")).get("status") == "warming_up"
        return missing("CPU utilization warming up" if warming else "CPU counters unavailable",
                       values=values, retrying=warming)
    return Observation(values)


def memory_observation(metrics):
    raw = mapping(observed(metrics.get("memory")))
    values = {}
    for source, target in (("MemTotal", "total"), ("MemAvailable", "available"), ("MemFree", "free")):
        row = mapping(raw.get(source))
        value = number(row.get("value"))
        # Linux meminfo reports kB, meaning kibibytes. The health contract uses MB.
        if value is not None and row.get("unit") == "kB":
            values[f"system.memory_{target}_mb"] = value * 1024 / 1e6
    total, available = values.get("system.memory_total_mb"), values.get("system.memory_available_mb")
    if total is None or available is None or total <= 0 or not 0 <= available <= total:
        return missing("Memory capacity/availability unavailable", values=values)
    values["system.memory_used_mb"] = total - available
    values["system.memory_used_pct"] = (total - available) * 100 / total
    return Observation(values)


def storage_observation(metrics):
    raw = mapping(observed(metrics.get("recording_filesystem")))
    total, free = number(raw.get("total_bytes")), number(raw.get("available_bytes"))
    if total is None or free is None or total <= 0 or not 0 <= free <= total:
        return missing("Recording filesystem capacity unavailable")
    return Observation({"disk.free_gb": free / 1e9, "disk.free_pct": 100 * free / total})


def sysfiles(metrics):
    return mapping(mapping(metrics.get("sysfs")).get("files"))


def sensor_values(files, suffix, label_suffix, prefix, divisor):
    values, paths = {}, []
    for path, record in sorted(files.items()):
        if not path.endswith(suffix):
            continue
        paths.append(path)
        value = number(observed(record))
        if value is None:
            continue
        stem = path[:-len(suffix)]
        label = observed(files.get(stem + label_suffix))
        if not label:
            # Preserve a stable kernel identity when optional friendly labels are absent.
            label = stem.replace("class/", "")
        key = prefix.format(keypart(label))
        if key in values:
            key = prefix.format(keypart(label) + "_" + keypart(stem))
        values[key] = value / divisor
    return values, paths


def temperature_observation(metrics):
    files = sysfiles(metrics)
    values, paths = sensor_values(files, "/temp", "/type", "temp.{}_c", 1000)
    hwvalues, hwpaths = sensor_values(
        {key: value for key, value in files.items() if re.search(r"/temp\d+_(input|label)$", key)},
        "_input", "_label", "temp.{}_c", 1000)
    values.update(hwvalues)
    return Observation(values) if values else missing("Temperature sensors unavailable" if paths or hwpaths
        else "No supported temperature sensors", unsupported=not (paths or hwpaths))


def gpu_observation(metrics):
    files = sysfiles(metrics)
    values, devices = {}, []
    for path in sorted(files):
        if not path.startswith("class/devfreq/") or not path.endswith("/cur_freq"):
            continue
        base = path.rsplit("/", 1)[0]
        label = str(observed(files.get(base + "/name")) or base.split("/")[-1])
        if not re.search(r"gpu|ga10b|gv11b|gp10b", label + " " + base, re.I):
            continue
        devices.append(base)
        frequency = number(observed(files[path]))
        if frequency is not None:
            values[f"gpu.{keypart(label)}.clock_mhz"] = frequency / 1e6
        load = number(observed(files.get(base + "/load")))
        if load is not None:
            values[f"gpu.{keypart(label)}.load_raw"] = load  # kernel driver units, not percent
            values[f"gpu.{keypart(label)}.load_units"] = "driver_raw"
    jetson_load = "devices/platform/gpu.0/load"
    if jetson_load in files:
        devices.append(jetson_load)
        load = number(observed(files[jetson_load]))
        if load is not None:
            values["gpu.jetson.load_raw"] = load
            values["gpu.jetson.load_units"] = "driver_raw"
    return Observation(values) if values else missing("GPU counters unavailable" if devices else
        "No supported GPU counters", unsupported=not devices)


def power_observation(metrics):
    files = sysfiles(metrics)
    values, paths = {}, []
    for kind, suffix, divisor in (("power", "w", 1e6), ("in", "voltage_v", 1000), ("curr", "current_a", 1000)):
        subset = {key: value for key, value in files.items() if re.search(rf"/{kind}\d+_(input|label)$", key)}
        readings, present = sensor_values(subset, "_input", "_label", "power.{}_" + suffix, divisor)
        values.update(readings)
        paths.extend(present)
    # Never sum rails, or pair unnamed channel numbers across unlike sensor types.
    return Observation(values) if values else missing("Power sensors unavailable" if paths else
        "No supported power sensors", unsupported=not paths)


def native_observations(state):
    """Live host presence plus explicitly named startup inventory; no active-mode claim."""
    native = mapping(mapping(state.get("host")).get("native_probe"))
    snapshot = mapping(native.get("snapshot"))
    probes = mapping(snapshot.get("probes"))
    boot = observed(mapping(state.get("sample")).get("boot_id"))
    values, startup_power = {}, {}
    if isinstance(boot, str) and boot:
        values["host.boot_id"] = boot
    associated = native.get("association") == "verified" and snapshot.get("boot_id") == boot and bool(boot)
    values["host.inventory.availability"] = "observed_at_startup" if associated else str(native.get("association") or native.get("status") or "unavailable")
    if not associated:
        values["host.inventory.reason"] = str(native.get("reason") or native.get("error") or "Host inventory association unavailable")[:512]
    else:
        os_release = mapping(observed(probes.get("os_release")))
        uname = mapping(observed(probes.get("uname")))
        for key, value in (("os", os_release.get("PRETTY_NAME") or os_release.get("NAME")),
                           ("os_version", os_release.get("VERSION_ID")),
                           ("kernel", uname.get("release")), ("arch", uname.get("machine")),
                           ("model", observed(probes.get("device_model"))),
                           ("hostname", snapshot.get("hostname")),
                           ("l4t", observed(probes.get("l4t_release"))),
                           ("observed_at", snapshot.get("collected_at"))):
            if isinstance(value, str) and value:
                values["host.inventory." + key] = value[:512]
        jetpack = mapping(probes.get("jetpack"))
        version_info = mapping(jetpack.get("value"))
        version = version_info.get("version")
        status = jetpack.get("status")
        values["host.inventory.jetpack_status"] = "unknown"
        if status in ("ok", "inferred") and isinstance(version, str) and version:
            values["host.inventory.jetpack"] = version
            values["host.inventory.jetpack_status"] = "installed" if status == "ok" else "inferred"
            source = jetpack.get("source")
            if isinstance(source, str) and source:
                values["host.inventory.jetpack_source"] = source[:512]
            l4t_version = version_info.get("l4t_version")
            if status == "inferred" and isinstance(l4t_version, str) and l4t_version:
                values["host.inventory.jetpack_l4t_version"] = l4t_version
        raw = observed(probes.get("nvpmodel"))
        match = re.search(r"NV Power Mode:\s*([^\r\n]+)\r?\n\s*(\d+)\s*(?:\n|$)", raw) if isinstance(raw, str) else None
        if match:
            startup_power = {"power.startup_mode_name": match.group(1).strip(),
                             "power.startup_mode_id": int(match.group(2)),
                             "power.startup_mode_observed_at": snapshot.get("collected_at", ""),
                             "power.mode_availability": "not_live_sampled"}
    host = Observation(values, message="Host observed; inventory is startup context" if associated else
                       "Host observed; startup inventory unavailable") if "host.boot_id" in values else missing(
                           "Host boot identity unavailable", values=values)
    return host, startup_power


def recording_observation(state):
    raw = mapping(state.get("recording"))
    if not isinstance(raw.get("enabled"), bool) or raw.get("state") not in ("disabled", "recording", "error", "failed", "stopped"):
        return missing("Recording status unavailable")
    values = {"recording.enabled": raw["enabled"], "recording.state": raw["state"]}
    errors = number(raw.get("write_errors"))
    if errors is not None:
        values["recording.write_errors"] = errors
    if isinstance(raw.get("path"), str):
        values["recording.path"] = raw["path"]
    if raw["state"] in ("error", "failed"):
        return Observation(values, message=str(raw.get("last_error") or "Recording failed"), level=ERROR)
    if raw["state"] == "stopped":
        return Observation(values, message="Recording stopped")
    if errors and errors > 0:
        return Observation(values, message="Recording resumed after write errors", level=WARN)
    return Observation(values, message="Recording disabled" if raw["state"] == "disabled" else "Recording")


def collectors_observation(state):
    workers = mapping(state.get("workers"))
    expected = state.get("expected")
    if not isinstance(expected, list) or not expected:
        return missing("Collector supervision status unavailable", source="workers")
    values, failures = {}, []
    level = OK
    for name in expected:
        row = mapping(workers.get(name))
        worker_state = row.get("state", "missing")
        values[f"collector.{keypart(name)}.state"] = worker_state
        restarts = number(row.get("restarts"))
        if restarts is not None:
            values[f"collector.{keypart(name)}.restarts"] = restarts
        exit_code = number(row.get("last_exit_code"))
        if exit_code is not None:
            values[f"collector.{keypart(name)}.last_exit_code"] = exit_code
        if worker_state != "running":
            level = max(level, ERROR if worker_state in ("missing", "stopped") else WARN)
            failures.append(f"{name}: {worker_state}")
        if row.get("last_error"):
            values[f"collector.{keypart(name)}.last_error"] = str(row["last_error"])[:512]
    return Observation(values, message="; ".join(failures) or "Collectors running", level=level, source="workers")


def map_observations(state, workers):
    metrics = mapping(mapping(state.get("sample")).get("metrics"))
    host, startup_power = native_observations(state)
    power = power_observation(metrics)
    power.values.update(startup_power)
    return {"cpu": cpu_observation(metrics), "memory": memory_observation(metrics),
            "storage": storage_observation(metrics), "temperature": temperature_observation(metrics),
            "gpu": gpu_observation(metrics), "power": power,
            "host": host, "recording": recording_observation(state), "collectors": collectors_observation(workers)}


def validate_cache(state, kind):
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError(f"invalid {kind} cache schema")
    if kind == "system":
        if not isinstance(state.get("collector_id"), str) or not state["collector_id"]:
            raise ValueError("missing collector identity")
        sequence = state.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("invalid collector sequence")
        if number(mapping(state.get("sample")).get("monotonic_s")) is None:
            raise ValueError("missing sample monotonic timestamp")
    elif not isinstance(state.get("supervisor_id"), str) or number(state.get("monotonic_s")) is None:
        raise ValueError("missing supervisor identity/timestamp")
    return state


def read_cache(path, kind):
    try:
        with Path(path).open() as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("cache exceeds read limit")
        def invalid_constant(value):
            raise ValueError(f"nonfinite JSON value: {value}")
        return validate_cache(json.loads(raw, parse_constant=invalid_constant), kind), None
    except (OSError, ValueError, TypeError) as exc:
        return None, f"{kind} cache unavailable: {exc}"


class Reporter:
    def __init__(self, *, instance="observer", vehicle_id="", scope="live_host", interval=1,
                 stale_after=5, components=COMPONENTS, thresholds=None, publisher_id=None):
        self.instance, self.vehicle_id, self.scope = instance, vehicle_id, scope
        self.interval, self.stale_after = interval, stale_after
        self.components = set(components)
        self.thresholds = validate_thresholds(thresholds or {})
        self.publisher_id, self.sequence = publisher_id or uuid.uuid4().hex, 0
        self.state, self.workers, self.retired = {}, {}, set()
        self.native_id, self.native_mono = "", None
        self.last_good = {}

    def accept(self, state, now, wall):
        validate_cache(state, "system")
        cid, sequence = state["collector_id"], state["sequence"]
        current_id = self.state.get("collector_id")
        if cid in self.retired:
            raise ValueError("retired collector cache")
        if cid == current_id:
            if sequence < self.state["sequence"]:
                raise ValueError("out-of-order collector cache")
            if sequence == self.state["sequence"]:
                return  # duplicate source ID cannot replace timestamps or measurements
        elif current_id:
            self.retired.add(current_id)
        self.state = state
        native = mapping(mapping(state.get("host")).get("native_probe"))
        snapshot = mapping(native.get("snapshot"))
        identity = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest() if snapshot else ""
        if identity != self.native_id:
            self.native_id, self.native_mono = identity, None
            try:
                stamp = dt.datetime.fromisoformat(snapshot["collected_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    age = wall - stamp.timestamp()
                    if math.isfinite(age) and age >= -1:
                        self.native_mono = now - max(age, 0)
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                pass

    def publish(self, state=None, workers=None, *, now=None, wall=None, error=None, workers_error=None):
        now, wall = time.monotonic() if now is None else now, time.time() if wall is None else wall
        self.sequence += 1
        if state is not None:
            try:
                self.accept(state, now, wall)
            except (ValueError, TypeError) as exc:
                error = str(exc)
        if workers is not None:
            try:
                validate_cache(workers, "workers")
                if workers.get("supervisor_id") != self.workers.get("supervisor_id") or (
                        workers["monotonic_s"] >= self.workers.get("monotonic_s", -1)):
                    self.workers = workers
            except (ValueError, TypeError) as exc:
                workers_error = str(exc)
        sample = mapping(self.state.get("sample"))
        identities = {
            "sample": (f"{self.state.get('collector_id', '')}:{self.state.get('sequence', '')}" if self.state else "",
                       number(sample.get("monotonic_s"))),
            "native": (self.native_id, self.native_mono),
            "workers": (f"{self.workers.get('supervisor_id', '')}:{self.workers.get('monotonic_s', '')}" if self.workers else "",
                        number(self.workers.get("monotonic_s")))}
        rows = []
        for component, observation in map_observations(self.state, self.workers).items():
            if component not in self.components:
                continue
            identity, stamp = identities[observation.source]
            age = now - stamp if stamp is not None else None
            source_error = workers_error if observation.source == "workers" else error
            source_missing = not self.workers if observation.source == "workers" else not self.state
            fault, fault_message = threshold_level(observation.values, self.thresholds)
            base_level = max(observation.level if observation.level != STALE else OK, fault)
            message = fault_message or observation.message
            values = dict(observation.values)
            if self.native_mono is not None:
                native_age = max(0, now - self.native_mono)
                if "host.inventory.observed_at" in values:
                    values["host.inventory.age_s"] = native_age
                if "power.startup_mode_observed_at" in values:
                    values["power.startup_mode_age_s"] = native_age
            availability = observation.availability
            level = base_level
            if availability == "current" and stamp is not None:
                self.last_good[component] = (dict(values), base_level, identity, stamp)
            unavailable = availability in ("unavailable", "retrying")
            expired = age is None or age < -1 or age > self.stale_after
            if source_missing or source_error or unavailable or (expired and availability != "unsupported"):
                previous = self.last_good.get(component)
                if (unavailable or source_error or source_missing) and previous:
                    values, base_level, identity, stamp = previous
                    values = dict(values)
                    age = now - stamp
                level = STALE
                availability = "retrying" if observation.availability == "retrying" else "unavailable" if source_missing else "stale"
                message = source_error or ("No source observation" if source_missing else
                    observation.message if unavailable else "Observation expired" if expired else message)
                if previous or (observation.availability == "current" and observation.values):
                    values["health.last_level"] = base_level
                values["health.error"] = message
            values.update({"health.instance": self.instance, "health.service": "observer",
                           "health.publisher_id": self.publisher_id, "health.sequence": self.sequence,
                           "health.publish_interval_s": self.interval, "health.sample_id": identity,
                           "health.stale_after_s": self.stale_after, "health.availability": availability,
                           "observer.scope": self.scope})
            if age is not None and math.isfinite(age):
                values["health.sample_age_s"] = max(0, age)
            if self.vehicle_id:
                values["health.vehicle_id"] = self.vehicle_id
            rows.append({"name": f"{self.instance}: {component}", "hardware_id": "", "level": level,
                         "message": message, "values": {key: text for key, value in values.items()
                                                       if (text := scalar(value)) is not None}})
        return rows


def configuration(env):
    instance = env.get("OBSERVER_DIAGNOSTICS_INSTANCE") or env.get("OBSERVER_NAME") or "observer"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", instance):
        raise ValueError("diagnostics instance must be a valid ROS namespace segment")
    components = json.loads(env.get("OBSERVER_DIAGNOSTICS_COMPONENTS") or json.dumps(COMPONENTS))
    if not isinstance(components, list) or any(not isinstance(c, str) or c not in COMPONENTS for c in components):
        raise ValueError("invalid diagnostics components")
    intervals = {}
    for key, default in (("INTERVAL_S", 1), ("STALE_AFTER_S", 5)):
        value = number(env.get("OBSERVER_DIAGNOSTICS_" + key, default))
        if value is None or value <= 0:
            raise ValueError(f"diagnostics {key} must be finite and positive")
        intervals[key] = value
    return dict(instance=instance, vehicle_id=env.get("VEHICLE_ID", ""),
                scope=env.get("OBSERVER_DIAGNOSTICS_SCOPE") or "live_host",
                interval=intervals["INTERVAL_S"], stale_after=intervals["STALE_AFTER_S"],
                components=components,
                thresholds=validate_thresholds(json.loads(env.get("OBSERVER_DIAGNOSTICS_THRESHOLDS") or "{}")))


def diagnostic_array(rows, wall_ns, array_type, status_type, keyvalue_type):
    """Use generated constants: DiagnosticStatus.level is a byte on some distros."""
    message = array_type()
    message.header.stamp.sec, message.header.stamp.nanosec = divmod(wall_ns, 1_000_000_000)
    levels = (status_type.OK, status_type.WARN, status_type.ERROR, status_type.STALE)
    for row in rows:
        status = status_type()
        status.name, status.hardware_id = row["name"], row["hardware_id"]
        status.level, status.message = levels[row["level"]], row["message"]
        status.values = [keyvalue_type(key=key, value=value) for key, value in row["values"].items()]
        message.status.append(status)
    return message


def main():
    try:
        config = configuration(os.environ)
    except (ValueError, TypeError) as exc:
        print(f"observer-diagnostics: {exc}", file=sys.stderr)
        return 2
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

    reporter = Reporter(**config)
    state_dir = Path(os.environ.get("OBSERVER_STATE_DIR") or "/run/observer")
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    node = None
    try:
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        node = rclpy.create_node("system_health", namespace="/" + config["instance"])
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE)
        publisher = node.create_publisher(DiagnosticArray,
            os.environ.get("OBSERVER_DIAGNOSTICS_TOPIC") or "/diagnostics", qos)
        print(f"observer-diagnostics: /{config['instance']}/system_health publishing", file=sys.stderr, flush=True)
        while not stopping and rclpy.ok():
            started = time.monotonic()
            state, error = read_cache(state_dir / "system.json", "system")
            workers, workers_error = read_cache(state_dir / "workers.json", "workers")
            rows = reporter.publish(state, workers, now=started, error=error, workers_error=workers_error)
            wall_ns = time.time_ns()  # always system time, regardless of use_sim_time
            message = diagnostic_array(rows, wall_ns, DiagnosticArray, DiagnosticStatus, KeyValue)
            publisher.publish(message)
            deadline = started + config["interval"]
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    finally:
        try:
            if node is not None:
                node.destroy_node()
        finally:
            try:
                rclpy.try_shutdown(uninstall_handlers=False)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
