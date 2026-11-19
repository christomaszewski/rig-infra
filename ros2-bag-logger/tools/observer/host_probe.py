#!/usr/bin/env python3
"""Best-effort, read-only host evidence collected by the launcher's actual ``up``.

This runs on the launcher host, not inside the recorder image. It deliberately does
not infer JetPack from a Rig target or claim that host packages are service-loaded
libraries. Commands share a short deadline; unavailable evidence stays explicit.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

SCHEMA_VERSION = 1
BUDGET_SECONDS = 3.0
MAX_TEXT = 65536
# Exact BSP pairings for the Orin/Thor JetPack generations in NVIDIA's release archive.
# A revision absent from this table stays unknown, even when its minor release is known.
JETPACK_ARCHIVE = "https://developer.nvidia.com/embedded/jetpack-archive"
JETPACK_BY_L4T = {
    "39.2.1": "7.2.1", "39.2.0": "7.2", "38.4.0": "7.1",
    "38.2.1": "7.0", "38.2.0": "7.0",
    "36.5.2": "6.2.3", "36.5.0": "6.2.2", "36.4.4": "6.2.1",
    "36.4.3": "6.2", "36.4.0": "6.1", "36.3.0": "6.0", "36.2.0": "6.0 DP",
    "35.7.0": "5.1.8", "35.6.5": "5.1.7", "35.6.4": "5.1.6",
    "35.6.2": "5.1.5", "35.6.1": "5.1.5", "35.6.0": "5.1.4",
    "35.5.0": "5.1.3", "35.4.1": "5.1.2", "35.3.1": "5.1.1",
    "35.2.1": "5.1", "35.1.0": "5.0.2",
    "34.1.1": "5.0.1 Developer Preview", "34.1.0": "5.0 Developer Preview",
}
PACKAGE_PATTERNS = (
    "nvidia-jetpack", "nvidia-l4t-*", "nvidia-container-toolkit*", "libnvidia-container*",
    "cuda-*", "libcudnn*", "libnvinfer*", "tensorrt*", "gstreamer1.0-*", "libgstreamer*",
    "ros-*-ros-core", "ros-*-rmw-*", "ros-*-zenoh*", "zenoh*",
)
ENV_KEYS = (
    "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "ROS_DISTRO", "ZENOH_CONFIG_OVERRIDE",
    "ZENOH_SESSION_CONFIG_URI", "RMW_ZENOH_BUFFER_POOL_MAX_SIZE_BYTES", "ZENOH_SHM_ALLOC_SIZE",
    "GST_PLUGIN_FEATURE_RANK", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
)
SYSCTL_KEYS = (
    "vm/swappiness", "vm/dirty_ratio", "vm/dirty_background_ratio", "vm/overcommit_memory",
    "kernel/sched_rt_runtime_us", "kernel/sched_rt_period_us", "kernel/shmmax", "kernel/shmall",
    "net/core/rmem_max", "net/core/wmem_max", "net/core/rmem_default", "net/core/wmem_default",
    "net/core/netdev_max_backlog", "net/ipv4/udp_rmem_min", "net/ipv4/udp_wmem_min",
)
_SENSITIVE = re.compile(r"password|passwd|secret|token|credential|private.?key|usrpwd|(?:^|[/_])auth(?:$|[/_])", re.I)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _text(value, limit=MAX_TEXT):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[:limit]


def sanitized_environment(env):
    """Only supported tuning knobs; redact credential-shaped override fields/URLs."""
    result = {}
    for key in ENV_KEYS:
        if key not in env:
            continue
        value = str(env[key])[:4096]
        if key == "ZENOH_CONFIG_OVERRIDE":
            fragments = []
            for fragment in value.split(";"):
                path, separator, setting = fragment.partition("=")
                fragments.append(path + separator + ("<redacted>" if _SENSITIVE.search(fragment) else setting))
            value = ";".join(fragments)
        # Connection URLs can carry credentials even in an otherwise safe setting.
        value = re.sub(r"(://)[^/\s;@]+@", r"\1<redacted>@", value)
        value = re.sub(r"([?&](?:password|passwd|secret|token|credential|key|api[_-]?key|access[_-]?token)=)[^&;\s]*",
                       r"\1<redacted>", value, flags=re.I)
        result[key] = value
    return result


class Probe:
    def __init__(self, root=Path("/"), budget=BUDGET_SECONDS):
        self.root = Path(root)
        self.deadline = time.monotonic() + budget

    def path(self, path):
        return self.root / str(path).lstrip("/")

    def read(self, path, limit=MAX_TEXT):
        result = {"source": str(path)}
        if time.monotonic() >= self.deadline:
            return dict(result, status="skipped", error="host probe deadline exhausted")
        try:
            with self.path(path).open("rb") as source:
                raw = source.read(limit + 1)
            result.update(status="ok", value=_text(raw[:limit]).rstrip("\0\n"))
            if len(raw) > limit:
                result["truncated"] = True
        except FileNotFoundError:
            result.update(status="missing")
        except PermissionError:
            result.update(status="permission_denied")
        except OSError as exc:
            result.update(status="error", error=str(exc))
        return result

    def command(self, argv, max_seconds=0.9, limit=MAX_TEXT):
        result = {"source": argv}
        remaining = min(max_seconds, self.deadline - time.monotonic())
        if remaining <= 0:
            return dict(result, status="skipped", error="host probe deadline exhausted")
        try:
            # A private process group lets a timed-out shell helper and its children
            # be reaped together, rather than leaving an inherited stdout pipe open.
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL, start_new_session=True,
                                       env={**os.environ, "LC_ALL": "C"})
        except FileNotFoundError:
            return dict(result, status="missing")
        except PermissionError:
            return dict(result, status="permission_denied")
        except OSError as exc:
            return dict(result, status="error", error=str(exc))
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            try:
                stdout, stderr = process.communicate(timeout=0.1)
            except subprocess.TimeoutExpired:
                stdout, stderr = exc.output, exc.stderr
            return dict(result, status="timeout", value=_text(stdout, limit), error=_text(stderr, 4096))
        error = _text(stderr, 4096)
        status = "ok" if process.returncode == 0 else "error"
        diagnostics = error + "\n" + _text(stdout, 4096)
        if process.returncode and re.search(
                r"permission denied|operation not permitted|must be root|root privilege|as (?:a )?root(?: user)?",
                diagnostics, re.I):
            status = "permission_denied"
        result.update(status=status, returncode=process.returncode, value=_text(stdout, limit))
        if error:
            result["error"] = error
        if len(stdout or b"") > limit:
            result["truncated"] = True
        return result


def _os_release(probe):
    value = probe.read("/etc/os-release")
    if value["status"] == "ok":
        wanted = {"NAME", "VERSION", "ID", "ID_LIKE", "PRETTY_NAME", "VERSION_ID", "VERSION_CODENAME"}
        value["value"] = {key: text.strip().strip('"') for line in value["value"].splitlines()
                          if "=" in line for key, text in [line.split("=", 1)] if key in wanted}
    return value


def _cpuinfo(probe):
    value = probe.read("/proc/cpuinfo")
    if value["status"] == "ok":
        fields = {}
        for line in value["value"].splitlines():
            key, separator, text = line.partition(":")
            key, text = key.strip(), text.strip()
            if separator and key in {"model name", "Hardware", "Revision", "CPU architecture", "CPU implementer", "CPU part"}:
                fields.setdefault(key, [])
                if text not in fields[key]:
                    fields[key].append(text)
        value["value"] = fields
    return value


def _packages(probe):
    result = probe.command(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n",
                            *PACKAGE_PATTERNS], limit=262144)
    packages = []
    for line in result.get("value", "").splitlines():
        fields = line.split("\t")
        if len(fields) == 4 and fields[3] == "installed":
            packages.append(dict(name=fields[0], version=fields[1], architecture=fields[2]))
    result["value"] = sorted(packages, key=lambda row: row["name"])
    # Unmatched patterns make dpkg-query exit 1 even when other requested packages
    # were reported. Keep both the installed evidence and the incomplete status.
    if packages and result["status"] == "error":
        result["status"] = "partial"
    return result


def jetpack_probe(release, packages):
    installed = {row["name"].partition(":")[0]: row for row in packages}
    if "nvidia-jetpack" in installed:
        return {"status": "ok", "source": "installed nvidia-jetpack package",
                "value": installed["nvidia-jetpack"]}

    # /etc/nv_tegra_release uses e.g. R39 / REVISION: 2.1, while dpkg reports
    # 39.2.1-20260806224157. Only strip a Debian packaging revision, never a BSP patch.
    versions = {}
    if release["status"] == "ok":
        header = re.match(r"^#[ \t]*R([0-9]+)[ \t]+\(release\),[ \t]*REVISION:[ \t]*"
                          r"([0-9]+)(?:\.([0-9]+))?[ \t]*(?=,|$)", release["value"], re.MULTILINE)
        if header:
            versions["/etc/nv_tegra_release"] = ".".join(header.groups(default="0"))
    core = installed.get("nvidia-l4t-core")
    if core:
        version = re.fullmatch(r"(?:[0-9]+:)?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?:-[0-9A-Za-z.+~]+)?",
                               core["version"])
        if version:
            versions["installed nvidia-l4t-core package"] = ".".join(version.groups(default="0"))

    source = " and ".join(versions)
    if len(set(versions.values())) > 1:
        reason = "L4T release sources disagree: " + ", ".join(f"{k}={v}" for k, v in versions.items())
    elif versions:
        l4t = next(iter(versions.values()))
        if l4t in JETPACK_BY_L4T:
            return {"status": "inferred", "source": source, "reference": JETPACK_ARCHIVE,
                    "value": {"version": JETPACK_BY_L4T[l4t], "l4t_version": l4t}}
        reason = f"No verified JetPack mapping for L4T {l4t}"
    else:
        reason = "No readable L4T version or installed JetPack metapackage was observed"
    return {"status": "unknown", "source": source or "native host inventory", "value": None, "error": reason}


def collect(root=Path("/"), budget=BUDGET_SECONDS, env=None):
    probe = Probe(root, budget)
    uname = platform.uname()
    probes = {
        "uname": {"status": "ok", "source": "uname", "value": {
            "system": uname.system, "release": uname.release, "version": uname.version,
            "machine": uname.machine}},
        "os_release": _os_release(probe),
        "device_model": probe.read("/proc/device-tree/model"),
        "cpuinfo": _cpuinfo(probe),
        "online_cpus": probe.read("/sys/devices/system/cpu/online"),
        "memory": probe.read("/proc/meminfo"),
        "l4t_release": probe.read("/etc/nv_tegra_release"),
        "nvpmodel_config": probe.read("/etc/nvpmodel.conf"),
        "nvpmodel_status": probe.read("/var/lib/nvpmodel/status"),
        "boot_id": probe.read("/proc/sys/kernel/random/boot_id", 256),
        "launcher_environment": {"status": "ok", "source": "allowlisted launcher environment",
                                 "scope": "launcher", "value": sanitized_environment(os.environ if env is None else env)},
    }
    sysctls = {key.replace("/", "."): probe.read("/proc/sys/" + key) for key in SYSCTL_KEYS}
    probes["sysctls"] = {"status": "ok" if any(row["status"] == "ok" for row in sysctls.values()) else "unavailable",
                         "source": "allowlisted /proc/sys settings", "value": sysctls}
    frequency = {}
    try:
        policies = sorted(probe.path("/sys/devices/system/cpu/cpufreq").glob("policy*"))[:256]
        for policy in policies:
            if not re.fullmatch(r"policy\d+", policy.name):
                continue
            prefix = "/sys/devices/system/cpu/cpufreq/" + policy.name + "/"
            frequency[policy.name] = {key: probe.read(prefix + key) for key in (
                "affected_cpus", "scaling_governor", "scaling_min_freq", "scaling_max_freq", "scaling_cur_freq")}
        probes["cpu_frequency"] = {"status": "ok" if frequency else "missing", "source": "/sys/devices/system/cpu/cpufreq", "value": frequency}
    except OSError as exc:
        probes["cpu_frequency"] = {"status": "error", "source": "/sys/devices/system/cpu/cpufreq", "error": str(exc)}
    shm = {"source": "/dev/shm"}
    try:
        stat = os.statvfs(probe.path("/dev/shm"))
        shm.update(status="ok", value={"total_bytes": stat.f_blocks * stat.f_frsize,
                                      "available_bytes": stat.f_bavail * stat.f_frsize})
    except FileNotFoundError:
        shm["status"] = "missing"
    except PermissionError:
        shm["status"] = "permission_denied"
    except OSError as exc:
        shm.update(status="error", error=str(exc))
    probes["shared_memory"] = shm
    probes["nvpmodel"] = probe.command(["nvpmodel", "-q", "--verbose"])
    probes["jetson_clocks"] = probe.command(["jetson_clocks", "--show"])
    probes["packages"] = _packages(probe)
    probes["jetpack"] = jetpack_probe(probes["l4t_release"], probes["packages"]["value"])
    return {"schema_version": SCHEMA_VERSION, "collected_at": utc_now(), "hostname": socket.gethostname(),
            "boot_id": probes["boot_id"].get("value"), "scope": "host", "probes": probes}


def output_directory(data_root, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("system instance name must be a single safe path component")
    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    current = root / "current"
    # Pin once, just like the recorder. A broken pointer is an error rather than a
    # new flat-layout collection that looks like evidence for the intended run.
    pinned = current.resolve(strict=True) if current.exists() or current.is_symlink() else root
    if not pinned.is_dir():
        raise ValueError("run data root is not a directory")
    return pinned / "system" / name / "host-probes"


def write_snapshot(directory, snapshot):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".host-probe-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        # A hard-link publish is atomic and fails instead of replacing an existing
        # file, including in the unlikely event of a generated-name collision.
        for _ in range(3):
            destination = directory / (stamp + "-" + uuid.uuid4().hex + ".json")
            try:
                os.link(temporary, destination)
                return destination
            except FileExistsError:
                continue
        raise OSError("could not allocate a unique host probe filename")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--data-root")
    destination.add_argument("--output", help="replace one launcher runtime cache file atomically")
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    if args.data_root and not args.name:
        parser.error("--data-root requires --name")
    try:
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                                 prefix=".host-probe-", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(collect(), stream, sort_keys=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), 0o644)
                os.replace(temporary, output)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        else:
            directory = output_directory(args.data_root, args.name)
            write_snapshot(directory, collect())
    except Exception as exc:  # Evidence must never prevent the authorized service launch.
        print(f"host-probe: capture unavailable: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
