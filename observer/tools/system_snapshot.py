#!/usr/bin/env python3
"""Run-scoped host observations. Reads explicit host mounts; never invokes commands.

CPU percentages are interval deltas (busy excludes idle AND iowait); disk/network
counters retain kernel units. Missing data stays missing. The observer's Python
runtime is labelled separately and is not evidence of workload library versions.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import sys
import time
import uuid


def timestamp() -> dict:
    return {"at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "monotonic_s": time.monotonic()}


def read_file(root: Path, relative: str) -> dict:
    """Bounded reads of caller-allowlisted files, including normal sysfs links."""
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
        with path.open() as stream:
            value = stream.read(4 * 1024 * 1024 + 1)
        if len(value) > 4 * 1024 * 1024:
            return {"status": "error", "source": str(path), "error": "file exceeds read limit"}
        return {"status": "ok", "source": str(path), "value": value.strip()}
    except FileNotFoundError:
        status, error = "missing", "not available"
    except PermissionError:
        status, error = "permission_denied", "not readable"
    except (OSError, ValueError) as exc:
        status, error = "error", str(exc)
    return {"status": status, "source": str(path), "error": error}


def parsed(root: Path, relative: str, parser) -> dict:
    result = read_file(root, relative)
    if result["status"] == "ok":
        try:
            result["value"] = parser(result["value"])
        except (ValueError, IndexError, KeyError, TypeError) as exc:
            result.update(status="error", error=f"invalid data: {exc}")
            result.pop("value", None)
    return result


CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def parse_cpu(raw: str) -> dict:
    result = {}
    for line in raw.splitlines():
        fields = line.split()
        if fields and re.fullmatch(r"cpu\d*", fields[0]):
            if len(fields) < 9:
                raise ValueError("CPU counter row has fewer than eight counters")
            values = list(map(int, fields[1:9]))  # guest already included in user/nice
            if any(value < 0 for value in values):
                raise ValueError("negative CPU counter")
            result[fields[0]] = dict(zip(CPU_FIELDS, values))
    if "cpu" not in result:
        raise ValueError("aggregate CPU counter is absent")
    return result


def cpu_utilization(current: dict, previous: dict | None) -> dict:
    result = {}
    for name, counters in current.items():
        if previous is None or name not in previous:
            result[name] = {"status": "warming_up"}
            continue
        delta = {key: counters[key] - previous[name][key] for key in CPU_FIELDS}
        total = sum(delta.values())
        if any(value < 0 for value in delta.values()) or total <= 0:
            result[name] = {"status": "unavailable", "reason": "counter reset or no elapsed ticks"}
            continue
        result[name] = {"status": "ok", "busy_percent":
                        100 * (total - delta["idle"] - delta["iowait"]) / total,
                        "iowait_percent": 100 * delta["iowait"] / total,
                        "idle_percent": 100 * delta["idle"] / total}
    return result


def parse_load(raw: str) -> dict:
    parts = raw.split()
    running, tasks = map(int, parts[3].split("/"))
    averages = list(map(float, parts[:3]))
    if any(not math.isfinite(value) or value < 0 for value in averages):
        raise ValueError("invalid load average")
    return {"one": averages[0], "five": averages[1], "fifteen": averages[2],
            "runnable_tasks": running, "tasks": tasks}


def parse_mem(raw: str) -> dict:
    result = {}
    for line in raw.splitlines():
        key, value = line.split(":", 1)
        parts = value.split()
        result[key] = {"value": int(parts[0]), "unit": parts[1] if len(parts) > 1 else "count"}
    if not result:
        raise ValueError("empty meminfo")
    return result


DISK_FIELDS = ("reads_completed", "reads_merged", "sectors_read", "read_ms",
               "writes_completed", "writes_merged", "sectors_written", "write_ms",
               "io_in_progress", "io_ms", "weighted_io_ms", "discards_completed",
               "discards_merged", "sectors_discarded", "discard_ms", "flushes_completed", "flush_ms")
NET_FIELDS = ("bytes", "packets", "errors", "dropped", "fifo", "frame", "compressed", "multicast")


def parse_disks(raw: str) -> dict:
    result = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 14:
            raise ValueError("short diskstats row")
        result[fields[2]] = dict(zip(DISK_FIELDS, map(int, fields[3:])))
    return result


def parse_network(raw: str) -> dict:
    result = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        name, values = line.split(":", 1)
        values = list(map(int, values.split()))
        if len(values) != 16:
            raise ValueError("network row must have sixteen counters")
        result[name.strip()] = {"rx": dict(zip(NET_FIELDS, values[:8])),
                                "tx": dict(zip(("bytes", "packets", "errors", "dropped",
                                                "fifo", "collisions", "carrier", "compressed"), values[8:]))}
    return result


def parse_pressure(raw: str) -> dict:
    result = {}
    for line in raw.splitlines():
        fields = line.split()
        result[fields[0]] = {key: int(value) if key == "total" else float(value)
                             for key, value in (field.split("=", 1) for field in fields[1:])}
        if not result[fields[0]] or any(not math.isfinite(value) or value < 0
                                       for value in result[fields[0]].values()):
            raise ValueError("invalid pressure data")
    if not result:
        raise ValueError("empty pressure data")
    return result


class HostReader:
    def __init__(self, proc: Path, sysfs: Path):
        self.proc, self.sysfs = proc, sysfs
        self.previous_cpu = None

    def files(self, paths: list[str]) -> dict:
        return {path: read_file(self.sysfs, path) for path in paths}

    def matches(self, pattern: str) -> list[str]:
        # Only fixed kernel interface patterns are passed here, never user paths.
        return sorted(str(path.relative_to(self.sysfs)) for path in self.sysfs.glob(pattern))[:4096]

    def settings(self) -> dict:
        paths = ["devices/system/cpu/online", "devices/system/cpu/offline",
                 "devices/system/cpu/possible"]
        for cpu in self.matches("devices/system/cpu/cpu[0-9]*"):
            paths += [f"{cpu}/{name}" for name in ("online", "cpufreq/scaling_governor",
                      "cpufreq/scaling_driver", "cpufreq/scaling_min_freq", "cpufreq/scaling_max_freq",
                      "cpufreq/cpuinfo_min_freq", "cpufreq/cpuinfo_max_freq")]
        for dev in self.matches("class/devfreq/*"):
            paths += [f"{dev}/{name}" for name in ("name", "governor", "min_freq", "max_freq",
                                                   "available_frequencies")]
        return {"scope": "host", "sysfs": self.files(paths)}

    def metrics(self) -> dict:
        cpu = parsed(self.proc, "stat", parse_cpu)
        if cpu["status"] == "ok":
            cpu["utilization"] = cpu_utilization(cpu["value"], self.previous_cpu)
            self.previous_cpu = cpu["value"]
        else:
            self.previous_cpu = None
        paths = self.matches("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")
        # Jetson exposes GPU load outside its devfreq directory. Preserve the raw
        # driver value; its scale is not a portable percentage contract.
        jetson_load = "devices/platform/gpu.0/load"
        if (self.sysfs / jetson_load).exists():
            paths.append(jetson_load)
        for zone in self.matches("class/thermal/thermal_zone*"):
            paths += [f"{zone}/type", f"{zone}/temp"]
        for dev in self.matches("class/devfreq/*"):
            paths += [f"{dev}/name", f"{dev}/cur_freq", f"{dev}/load"]
        for hw in self.matches("class/hwmon/hwmon*"):
            paths.append(f"{hw}/name")
            for sensor in ("temp", "power", "in", "curr", "fan"):
                for path in self.matches(f"{hw}/{sensor}[0-9]*_input"):
                    paths += [path, path.removesuffix("_input") + "_label"]
        return {"scope": "host", "cpu": cpu,
                "load": parsed(self.proc, "loadavg", parse_load),
                "memory": parsed(self.proc, "meminfo", parse_mem),
                "disks": parsed(self.proc, "diskstats", parse_disks),
                "network": parsed(self.proc, "net/dev", parse_network),
                "pressure": {key: parsed(self.proc, f"pressure/{key}", parse_pressure)
                             for key in ("cpu", "memory", "io")},
                "sysfs": {"status": "ok" if paths else "unavailable", "files": self.files(paths)}}


def recording_filesystem(path: Path) -> dict:
    result = {"scope": "host_recording_filesystem", "source": str(path)}
    try:
        stat = os.statvfs(path)
        total = stat.f_blocks * stat.f_frsize
        available = stat.f_bavail * stat.f_frsize
        used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
        result.update(status="ok", value={"total_bytes": total, "available_bytes": available,
                      "used_bytes": used, "used_percent": 100 * used / (used + available)
                      if used + available else None})
    except FileNotFoundError:
        result["status"] = "missing"
    except PermissionError:
        result["status"] = "permission_denied"
    except OSError as exc:
        result.update(status="error", error=str(exc))
    return result


def resolve_run(env: dict) -> Path:
    if env.get("RIG_OBSERVER_RUN_DIR"):
        return Path(env["RIG_OBSERVER_RUN_DIR"]).resolve()
    root = Path(env.get("RIG_BAG_ROOT") or "/data")
    current = root / "current"
    return (current if current.exists() else root).resolve()


def create_capture(run: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", ".."):
        raise ValueError("invalid system snapshot name")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    result = run / "system" / name / f"{stamp}-{uuid.uuid4().hex[:12]}"
    result.mkdir(parents=True)
    return result


def host_snapshot(run: Path, name: str, boot: dict, native_probe_file: str | None = None) -> dict:
    result = {"schema_version": 1, "scope": "host", **timestamp(), "boot_id": boot,
              "native_probe": {"status": "missing", "reason": "no readable native host probe"},
              "observer_runtime": {"scope": "observer_container", "python": platform.python_version()}}
    candidates = [Path(native_probe_file)] if native_probe_file else sorted(
        (run / "system" / name / "host-probes").glob("*.json"), reverse=True)
    for path in candidates:
        try:
            with path.open() as stream:
                text = stream.read(4 * 1024 * 1024 + 1)
            if len(text) > 4 * 1024 * 1024:
                raise ValueError("host probe exceeds read limit")
            probe = json.loads(text)
            collected = dt.datetime.fromisoformat(probe["collected_at"].replace("Z", "+00:00"))
            if probe.get("scope") != "host" or collected.tzinfo is None:
                raise ValueError("probe lacks host scope or timestamp timezone")
            age = (dt.datetime.now(dt.timezone.utc) - collected).total_seconds()
            native_boot = probe.get("boot_id")
            if isinstance(native_boot, dict):
                native_boot = native_boot.get("value")
            current_boot = boot.get("value") if boot.get("status") == "ok" else None
            has_current = isinstance(current_boot, str) and bool(current_boot.strip())
            has_native = isinstance(native_boot, str) and bool(native_boot.strip())
            changed_boot = has_current and has_native and native_boot != current_boot
            stale = age > 300 or age < -5
            if not (has_current and has_native):
                association, association_status = "unverified", "unverified"
                reason = "both collector-host and launcher-host boot IDs are required to verify association"
            elif changed_boot:
                association, association_status = "mismatched", "stale"
                reason = "collector-host and launcher-host boot IDs differ"
            else:
                association, association_status = "verified", "stale" if stale else "ok"
                reason = "collector-host and launcher-host boot IDs match"
            result["native_probe"] = {"status": association_status, "scope": "launcher_host",
                                      "association": association, "reason": reason,
                                      "source": str(path), "age_s": age,
                                      "stale": stale, "boot_id_mismatch": bool(changed_boot),
                                      "snapshot": probe}
            break
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            result["native_probe"] = {"status": "error", "source": str(path), "error": str(exc)}
            # Do not silently substitute an older, potentially unrelated snapshot.
            break
    return result


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class StreamCorruptionError(RuntimeError):
    """Cannot safely resume this capture's JSONL stream; supervisor must restart."""


def append_json(path: Path, data: dict) -> None:
    payload = (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    # Unbuffered bytes make the saved offset and rollback boundary unambiguous,
    # including when the filesystem accepts only part of a write before ENOSPC.
    with path.open("ab", buffering=0) as stream:
        boundary = stream.tell()
        try:
            remaining = memoryview(payload)
            while remaining:
                count = stream.write(remaining)
                if not count:
                    raise OSError("append made no progress")
                remaining = remaining[count:]
        except Exception:
            try:
                stream.truncate(boundary)
            except OSError as rollback_error:
                raise StreamCorruptionError(f"cannot restore JSONL boundary in {path}: {rollback_error}") from rollback_error
            raise


class Writer:
    def __init__(self, out: Path, ensure_directory=False):
        self.out, self.errors, self.last_warning = out, 0, -float("inf")
        self.ensure_directory, self.last_error = ensure_directory, None

    def write(self, name: str, value: dict, append=False) -> bool:
        try:
            if self.ensure_directory:
                self.out.mkdir(parents=True, exist_ok=True)
            (append_json if append else atomic_json)(self.out / name, value)
            return True
        except (OSError, ValueError) as exc:
            self.errors += 1
            self.last_error = str(exc)
            now = time.monotonic()
            if now - self.last_warning >= 60:
                print(f"system-snapshot: write failed ({exc}); retrying on next tick", file=sys.stderr)
                self.last_warning = now
            return False


def interval(env: dict, key: str, default: float) -> float:
    value = float(env.get(key) or default)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be finite and positive")
    return max(value, 0.05)  # malformed/adversarial config cannot create an unbounded hot loop


def recording_enabled(env: dict) -> bool:
    value = str(env.get("OBSERVER_RECORDING_ENABLED", "true")).lower()
    if value not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        raise ValueError("OBSERVER_RECORDING_ENABLED must be true or false")
    return value in ("true", "1", "yes", "on")


def main() -> int:
    env = os.environ
    try:
        sample_s = interval(env, "SYSTEM_SNAPSHOT_INTERVAL_S", 1)
        settings_s = interval(env, "SYSTEM_SNAPSHOT_SETTINGS_INTERVAL_S", 30)
        name = env.get("SYSTEM_SNAPSHOT_NAME") or env.get("GRAPH_SNAPSHOT_NAME") or env.get("OBSERVER_NAME") or "observer"
        recording = recording_enabled(env)
        run = resolve_run(env)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", ".."):
            raise ValueError("invalid system snapshot name")
    except (OSError, ValueError) as exc:
        print(f"system-snapshot: cannot start: {exc}", file=sys.stderr)
        return 1
    reader = HostReader(Path(env.get("SYSTEM_SNAPSHOT_PROC_ROOT") or "/host/proc"),
                        Path(env.get("SYSTEM_SNAPSHOT_SYS_ROOT") or "/host/sys"))
    writer = None
    cache = Writer(Path(env.get("OBSERVER_STATE_DIR") or "/run/observer"), ensure_directory=True)
    collector_id = uuid.uuid4().hex
    sequence = 0
    stopping = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = signal.Signals(signum).name

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    boot = read_file(reader.proc, "sys/kernel/random/boot_id")
    status = {"schema_version": 1, "started": timestamp(), "state": "running", "samples_written": 0,
              "settings_events": 0, "run": str(run), "boot_id": boot,
              "interval_s": sample_s, "settings_interval_s": settings_s,
              "host_mounts": {"proc": str(reader.proc), "sys": str(reader.sysfs)}}
    previous_settings = None
    current_settings = None
    settings_monotonic_s = 0.0
    next_settings = 0.0
    snapshot = host_snapshot(run, name, boot, env.get("OBSERVER_NATIVE_PROBE_FILE"))
    host_written = False
    failure = None
    next_record_retry = 0.0
    record_errors = 0
    record_error = None
    latest = None
    print(f"system-snapshot: recording={'enabled' if recording else 'disabled'} run={run}", file=sys.stderr, flush=True)
    try:
        while not stopping:
            started = time.monotonic()
            if started >= next_settings:
                current_settings = reader.settings()
                # Repeated partial `rig up` can refresh host probes without recreating
                # this container. Retain new evidence in settings events. Exclude the
                # continuously increasing age from identity; source/time remain in
                # the copied native probe, so every actual new observation is kept.
                snapshot = host_snapshot(run, name, boot, env.get("OBSERVER_NATIVE_PROBE_FILE"))
                native = snapshot["native_probe"]
                current_settings["native_probe"] = {key: value for key, value in native.items() if key != "age_s"}
                settings_monotonic_s = time.monotonic()
                next_settings = started + settings_s
            if recording and writer is None and started >= next_record_retry:
                try:
                    writer = Writer(create_capture(run, name))
                    host_written, previous_settings = False, None
                    record_error = None
                    status.update(state="running")
                    status.pop("error", None)
                except (OSError, ValueError) as exc:
                    record_errors += 1
                    record_error = str(exc)
                    next_record_retry = started + max(1, settings_s)
            sample = {"schema_version": 1, **timestamp(), "boot_id": boot,
                      "metrics": reader.metrics()}
            sample["metrics"]["recording_filesystem"] = recording_filesystem(
                Path(env.get("RIG_BAG_ROOT") or "/data"))
            sequence += 1
            if writer is not None:
                errors_before = writer.errors
                try:
                    if not host_written:
                        host_written = writer.write("host.json", snapshot)
                    digest = hashlib.sha256(json.dumps(current_settings, sort_keys=True).encode()).hexdigest()
                    if digest != previous_settings:
                        event = {"schema_version": 1, **timestamp(), "sha256": digest, "settings": current_settings}
                        if writer.write("settings-events.jsonl", event, append=True):
                            previous_settings = digest
                            status["settings_events"] += 1
                    if writer.write("metrics.jsonl", sample, append=True):
                        status["samples_written"] += 1
                    status.update(last_observed=sample["at"], write_errors=record_errors + writer.errors)
                    writer.write("status.json", status)
                    record_error = writer.last_error if writer.errors > errors_before else None
                except StreamCorruptionError as exc:
                    # Never append after an unrepairable partial record. Keep live
                    # sampling and retry with a fresh capture after bounded backoff.
                    record_errors += writer.errors + 1
                    record_error = str(exc)
                    status.update(state="failed", error=record_error, write_errors=record_errors)
                    writer.write("status.json", status)
                    writer = None
                    next_record_retry = started + max(1, settings_s)
            latest = {"schema_version": 1, "collector_id": collector_id, "sequence": sequence,
                      "sample": sample, "settings": current_settings,
                      "settings_monotonic_s": settings_monotonic_s, "host": snapshot,
                      "recording": {"enabled": recording,
                                    "state": "disabled" if not recording else "error" if record_error else "recording",
                                    "path": str(writer.out) if writer else None,
                                    "write_errors": record_errors + (writer.errors if writer else 0),
                                    "last_error": record_error}}
            cache.write("system.json", latest)
            deadline = started + sample_s
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except Exception as exc:  # supervisor may restart; retain an honest failure marker
        failure = f"{type(exc).__name__}: {exc}"
        print(f"system-snapshot: failed: {failure}", file=sys.stderr)
    finally:
        status.update(state="failed" if failure else "stopped", ended=timestamp(),
                      stop_signal=stopping, write_errors=record_errors + (writer.errors if writer else 0))
        if failure:
            status["error"] = failure
        if writer is not None:
            writer.write("status.json", status)
        if latest is not None:
            latest["recording"]["state"] = "failed" if failure else "stopped" if recording else "disabled"
            cache.write("system.json", latest)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 1 if failure else 0


if __name__ == "__main__":
    sys.exit(main())
