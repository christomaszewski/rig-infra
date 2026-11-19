#!/usr/bin/env python3
"""Supervise independent graph, host and diagnostics workers in the observer service.

The run directory is resolved once for the container, including worker restarts. ROS
initialization and middleware cleanup stay in the graph/diagnostics children, and
graph walks stay in the graph child: none
can stall host sampling or the supervisor. Each child owns a process group, and stop
signals reach every group before we wait for any one worker. A shared three-second
grace period lets in-flight atomic writes finish without letting ROS cleanup hang a
container stop. Compose's init process reaps any orphaned worker descendants.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


STOP_GRACE_S = 3.0
KILL_REAP_S = 0.5
POLL_S = 0.05
RESTART_MIN_S = 0.25
RESTART_MAX_S = 30.0
STABLE_S = 30.0


def enabled(env: dict, key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None or value == "":
        return default
    value = value.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{key} must be true or false, got {value!r}")


def resolve_run_dir(root: str) -> str:
    """Pin the current run, or the standalone data root, before starting workers."""
    current = os.path.join(root, "current")
    return os.path.realpath(current if os.path.exists(current) else root)


def log(message: str) -> None:
    print(f"run-observer: {message}", file=sys.stderr, flush=True)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


@dataclass
class Worker:
    name: str
    script: Path
    process: subprocess.Popen | None = None
    started: float = 0.0
    retry_at: float = 0.0
    retry_delay: float = RESTART_MIN_S
    starts: int = 0
    started_at: str | None = None
    last_exit_code: int | None = None
    last_error: str | None = None

    def health(self, state: str) -> dict:
        exit_code = self.process.poll() if self.process is not None else None
        running = self.process is not None and exit_code is None
        return {"state": "stopped" if state == "stopped" and not running else
                         "running" if running else "restarting" if self.retry_at else "starting",
                "pid": self.process.pid if running else None,
                "started_at": self.started_at,
                "started_monotonic_s": self.started or None,
                "restarts": max(0, self.starts - 1),
                "last_exit_code": exit_code if exit_code is not None else self.last_exit_code,
                "last_error": self.last_error,
                "retry_at_monotonic_s": self.retry_at if not running and self.retry_at else None}

    def retry(self, now: float) -> None:
        self.retry_at = now + self.retry_delay
        self.retry_delay = min(self.retry_delay * 2, RESTART_MAX_S)

    def maintain(self, now: float, env: dict) -> None:
        if self.process is not None:
            rc = self.process.poll()
            if rc is None:
                return
            # A crashed worker must not leave helper processes behind on restart.
            signal_group(self.process, signal.SIGKILL)
            if now - self.started >= STABLE_S:
                self.retry_delay = RESTART_MIN_S
            log(f"{self.name} exited ({rc}); restarting in {self.retry_delay:g}s")
            self.last_exit_code, self.last_error = rc, f"worker exited with code {rc}"
            self.process = None
            self.retry(now)
        if now < self.retry_at:
            return
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-u", str(self.script)], env=env, start_new_session=True)
            self.started = now
            self.started_at = utc_now()
            self.starts += 1
            self.last_error = None
            log(f"started {self.name} (pid {self.process.pid})")
        except OSError as exc:
            self.last_error = str(exc)
            log(f"cannot start {self.name}: {exc}; retrying in {self.retry_delay:g}s")
            self.retry(now)


class HealthWriter:
    def __init__(self, directory: Path):
        self.directory = directory
        self.supervisor_id = uuid.uuid4().hex
        self.last_warning = -float("inf")
        self.last_written = -float("inf")
        self.last_identity = None

    def publish(self, workers: list[Worker], state: str) -> None:
        now = time.monotonic()
        identities = {worker.name: worker.health(state) for worker in workers}
        identity = json.dumps([state, identities], sort_keys=True)
        if identity == self.last_identity and now - self.last_written < 1:
            return
        value = {"schema_version": 1, "supervisor_id": self.supervisor_id, "at": utc_now(),
                 "monotonic_s": now, "state": state,
                 "expected": [worker.name for worker in workers], "workers": identities}
        temporary = self.directory / f"workers.tmp.{os.getpid()}"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
            temporary.replace(self.directory / "workers.json")
            self.last_written, self.last_identity = now, identity
        except OSError as exc:
            if now - self.last_warning >= 60:
                log(f"worker health cache unavailable: {exc}")
                self.last_warning = now
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def stop_workers(workers: list[Worker], sig: int) -> None:
    processes = [(w.name, w.process) for w in workers if w.process is not None]
    # Signal ALL workers first: a stuck graph must not consume the system worker's
    # opportunity to flush and write its final status.
    for _, process in processes:
        signal_group(process, sig)
    deadline = time.monotonic() + STOP_GRACE_S
    pending = processes
    while pending and time.monotonic() < deadline:
        alive = []
        for name, process in pending:
            if process.poll() is None:
                alive.append((name, process))
            else:
                signal_group(process, signal.SIGKILL)  # no helpers outlive their worker
        pending = alive
        if pending:
            time.sleep(min(POLL_S, max(0, deadline - time.monotonic())))
    for name, process in pending:
        log(f"{name} did not stop within {STOP_GRACE_S:g}s; killing its process group")
        signal_group(process, signal.SIGKILL)
    reap_deadline = time.monotonic() + KILL_REAP_S
    for name, process in pending:
        try:
            process.wait(timeout=max(0, reap_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # An uninterruptible kernel wait cannot be fixed by another signal. Do
            # not let it hold the supervisor indefinitely; container init owns reap.
            log(f"{name} has not reaped after SIGKILL")


def main() -> int:
    env = os.environ.copy()
    scripts = Path(__file__).resolve().parent
    try:
        recording = enabled(env, "OBSERVER_RECORDING_ENABLED", True)
        graph = enabled(env, "GRAPH_SNAPSHOT_ENABLED", True)
        if graph and not recording:
            raise ValueError("graph capture requires recording enabled")
        workers = [Worker(name, scripts / script) for name, script, active in (
            ("graph", "graph_snapshot.py", graph),
            ("system", "system_snapshot.py", enabled(env, "SYSTEM_SNAPSHOT_ENABLED", False)),
            ("diagnostics", "diagnostics_publish.py", enabled(env, "OBSERVER_DIAGNOSTICS_ENABLED", False)))
            if active]
    except ValueError as exc:
        log(str(exc))
        return 2
    health = HealthWriter(Path(env.get("OBSERVER_STATE_DIR") or "/run/observer"))
    if not workers:
        health.publish(workers, "stopped")
        return 0
    # Ignore inherited pins: this process establishes a new container lifetime.
    env["RIG_OBSERVER_RUN_DIR"] = resolve_run_dir(env.get("RIG_BAG_ROOT") or "/data")
    log(f"pinned output to {env['RIG_OBSERVER_RUN_DIR']}")
    stopping = 0

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = stopping or signum

    previous = {sig: signal.signal(sig, request_stop)
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        while not stopping:
            for worker in workers:
                if stopping:
                    break
                worker.maintain(time.monotonic(), env)
            health.publish(workers, "running")
            if not stopping:
                time.sleep(POLL_S)
    finally:
        try:
            health.publish(workers, "stopping")
            stop_workers(workers, stopping or signal.SIGTERM)
            health.publish(workers, "stopped")
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
