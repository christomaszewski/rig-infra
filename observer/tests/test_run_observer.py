"""Process-level collector isolation and shutdown tests; no Docker or ROS needed.

Run: python3 tests/test_run_observer.py
The hung-cleanup fixture runs the real graph worker with a stub ROS runtime. Other
fixtures expose worker launch/restart/signal boundaries through files.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest


TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import graph_snapshot
import run_observer


def record(path, value):
    with path.open("a") as handle:
        handle.write(json.dumps(value) + "\n")


def worker(kind):
    root = Path(os.environ["OBSERVER_TEST_ROOT"])
    mode = os.environ.get(f"OBSERVER_TEST_{kind.upper()}", "normal")
    launches = root / f"{kind}.launches"
    record(launches, dict(pid=os.getpid(), at=time.monotonic(),
                          run=os.environ["RIG_OBSERVER_RUN_DIR"]))
    generation = len(launches.read_text().splitlines())
    if mode == "ros_cleanup":
        graph_with_stuck_cleanup(root)
        return
    if mode == "forbidden":
        raise AssertionError("disabled collector was launched")
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        record(root / f"{kind}.signals", dict(signal=signum, at=time.monotonic()))
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    (root / f"{kind}.ready").touch()
    while not stopping:
        (root / f"{kind}.heartbeat").write_text(str(time.monotonic()))
        if mode == "crash_once" and generation == 1 and (root / "crash").exists():
            sys.exit(17)
        time.sleep(0.025)
    (root / f"{kind}.stopped").touch()


def graph_with_stuck_cleanup(root):
    """Actual graph_snapshot.main(), with destroy_node deliberately never returning."""
    rclpy = types.ModuleType("rclpy")
    signals = types.ModuleType("rclpy.signals")
    signals.SignalHandlerOptions = types.SimpleNamespace(NO=object())

    class Node:
        context = types.SimpleNamespace(ok=lambda: True)

        def get_fully_qualified_name(self):
            return "/graph_snapshotter_test"

        def get_node_names_and_namespaces(self):
            return []

        def destroy_node(self):
            (root / "graph.cleanup").touch()
            while True:
                time.sleep(0.05)

    rclpy.init = lambda **kwargs: None
    rclpy.create_node = lambda name: Node()
    rclpy.ok = lambda: True
    rclpy.try_shutdown = lambda **kwargs: None
    sys.modules.update({"rclpy": rclpy, "rclpy.signals": signals})
    # The first interval means a complete epoch is already durable and both signal
    # handlers are installed. Simulate a context requesting exit at this boundary,
    # before the supervisor receives any stop signal.
    original_wait = graph_snapshot.wait_for_stop

    def wait(seconds, stopping):
        if seconds:
            (root / "graph.ready").touch()
            return True
        return original_wait(seconds, stopping)

    graph_snapshot.wait_for_stop = wait
    os.environ.update(GRAPH_SNAPSHOT_SETTLE_S="0", GRAPH_SNAPSHOT_INTERVAL_S="60",
                      GRAPH_SNAPSHOT_NAME="test")
    graph_snapshot.main()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        shutil.copy(TOOLS / "run_observer.py", self.scripts / "run_observer.py")
        for kind in ("graph", "system", "diagnostics"):
            script = "diagnostics_publish.py" if kind == "diagnostics" else f"{kind}_snapshot.py"
            (self.scripts / script).write_text(
                "import runpy\n"
                f"scope = runpy.run_path({str(Path(__file__).resolve())!r})\n"
                f"scope['worker']({kind!r})\n")
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.temp.cleanup()

    def start(self, **overrides):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GRAPH_SNAPSHOT_", "SYSTEM_SNAPSHOT_", "RIG_OBSERVER_", "OBSERVER_"))}
        env.update(OBSERVER_TEST_ROOT=str(self.root), RIG_BAG_ROOT=str(self.root / "data"),
                   GRAPH_SNAPSHOT_ENABLED="true", SYSTEM_SNAPSHOT_ENABLED="true",
                   OBSERVER_STATE_DIR=str(self.root / "state"))
        env.update(overrides)
        process = subprocess.Popen([sys.executable, str(self.scripts / "run_observer.py")],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        self.processes.append(process)
        return process

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("collector did not reach expected boundary")

    def rows(self, name):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def ready(self, *kinds):
        self.wait_for(lambda: all((self.root / f"{kind}.ready").exists() for kind in kinds))

    def stop(self, process, sig=signal.SIGTERM, timeout=5):
        process.send_signal(sig)
        stdout, stderr = process.communicate(timeout=timeout)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        return stderr

    def test_both_disabled_exit_without_workers(self):
        process = self.start(GRAPH_SNAPSHOT_ENABLED="false", SYSTEM_SNAPSHOT_ENABLED="false")
        process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(list(self.root.glob("*.launches")), [])

    def test_system_only_never_launches_ros_worker(self):
        process = self.start(GRAPH_SNAPSHOT_ENABLED="false", OBSERVER_TEST_GRAPH="forbidden")
        self.ready("system")
        self.stop(process)
        self.assertEqual(self.rows("graph.launches"), [])
        self.assertTrue((self.root / "system.stopped").exists())

    def test_default_flags_preserve_graph_only(self):
        self.assertTrue(run_observer.enabled({}, "GRAPH_SNAPSHOT_ENABLED", True))
        self.assertFalse(run_observer.enabled({}, "SYSTEM_SNAPSHOT_ENABLED", False))
        process = self.start(GRAPH_SNAPSHOT_ENABLED="", SYSTEM_SNAPSHOT_ENABLED="")
        self.ready("graph")
        self.stop(process)
        self.assertEqual(self.rows("system.launches"), [])

    def test_diagnostics_worker_has_independent_restart_and_health(self):
        process = self.start(GRAPH_SNAPSHOT_ENABLED="false", OBSERVER_RECORDING_ENABLED="false",
                             OBSERVER_DIAGNOSTICS_ENABLED="true", OBSERVER_TEST_DIAGNOSTICS="crash_once")
        self.ready("system", "diagnostics")
        cache = self.root / "state/workers.json"
        self.wait_for(cache.exists)
        value = json.loads(cache.read_text())
        self.assertEqual(value["expected"], ["system", "diagnostics"])
        self.assertEqual(value["workers"]["diagnostics"]["state"], "running")
        self.assertEqual(value["workers"]["diagnostics"]["restarts"], 0)
        (self.root / "crash").touch()
        self.wait_for(lambda: len(self.rows("diagnostics.launches")) == 2)
        self.wait_for(lambda: json.loads(cache.read_text())["workers"]["diagnostics"]["restarts"] == 1)
        value = json.loads(cache.read_text())
        self.assertEqual(value["workers"]["diagnostics"]["last_exit_code"], 17)
        self.assertEqual(len(self.rows("system.launches")), 1)
        self.assertFalse((self.root / "data").exists())
        self.stop(process)
        value = json.loads(cache.read_text())
        self.assertEqual(value["state"], "stopped")
        self.assertTrue(all(row["state"] == "stopped" for row in value["workers"].values()))

    def test_recording_disabled_rejects_graph_without_starting_workers(self):
        process = self.start(OBSERVER_RECORDING_ENABLED="false")
        _, stderr = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 2)
        self.assertIn("graph capture requires recording", stderr)
        self.assertFalse(list(self.root.glob("*.launches")))

    def test_signals_reach_both_workers_without_serial_wait(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                # Separate fixture files for the second signal run.
                for path in self.root.glob("*.ready"):
                    path.unlink()
                process = self.start()
                self.ready("graph", "system")
                t0 = time.monotonic()
                self.stop(process, sig, timeout=2)
                times = []
                for kind in ("graph", "system"):
                    received = self.rows(f"{kind}.signals")[-1]
                    self.assertEqual(received["signal"], sig)
                    times.append(received["at"])
                    self.assertTrue((self.root / f"{kind}.stopped").exists())
                self.assertLess(max(times) - t0, 0.75)
                self.assertLess(abs(times[0] - times[1]), 0.5)

    def test_each_worker_restarts_without_restarting_peer_and_keeps_run_pin(self):
        for crashing, peer in (("graph", "system"), ("system", "graph")):
            with self.subTest(crashing=crashing):
                for path in self.root.glob("*.launches"):
                    path.unlink()
                for path in self.root.glob("*.ready"):
                    path.unlink()
                (self.root / "crash").unlink(missing_ok=True)
                data = self.root / f"data-{crashing}"
                first, second = data / "runs/one", data / "runs/two"
                first.mkdir(parents=True)
                second.mkdir()
                (data / "current").symlink_to(first)
                process = self.start(**{f"OBSERVER_TEST_{crashing.upper()}": "crash_once",
                                        "RIG_BAG_ROOT": str(data)})
                self.ready("graph", "system")
                (data / "current").unlink()
                (data / "current").symlink_to(second)
                (self.root / "crash").touch()
                self.wait_for(lambda: len(self.rows(f"{crashing}.launches")) == 2)
                self.assertEqual(len(self.rows(f"{peer}.launches")), 1)
                for kind in (crashing, peer):
                    self.assertTrue(all(row["run"] == str(first.resolve())
                                        for row in self.rows(f"{kind}.launches")))
                self.stop(process)

    def test_stalled_ros_cleanup_is_bounded_and_system_flushes_immediately(self):
        process = self.start(OBSERVER_TEST_GRAPH="ros_cleanup")
        self.ready("graph", "system")
        epochs = list((self.root / "data/graph/test").glob("epoch_*.yaml"))
        self.assertEqual(len(epochs), 1)
        before = epochs[0].read_bytes()
        graph_pid = self.rows("graph.launches")[0]["pid"]
        self.wait_for(lambda: (self.root / "graph.cleanup").exists())
        # Even a ROS worker already stuck in native cleanup cannot stop CPU samples.
        heartbeat = self.root / "system.heartbeat"
        self.wait_for(heartbeat.exists)
        first_heartbeat = float(heartbeat.read_text() or "0")
        self.wait_for(lambda: float(heartbeat.read_text() or "0") > first_heartbeat + 0.1)
        t0 = time.monotonic()
        process.terminate()
        self.wait_for(lambda: (self.root / "system.stopped").exists(), timeout=1)
        self.assertIsNone(process.poll(), "fixture graph should still be stuck in cleanup")
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertLess(time.monotonic() - t0, 4.5)
        self.assertIn("graph did not stop within 3s", stderr)
        self.assertEqual(epochs[0].read_bytes(), before)
        self.assertFalse(list(epochs[0].parent.glob("*.tmp.*")))
        with self.assertRaises(ProcessLookupError):
            os.kill(graph_pid, 0)

    def test_graph_output_uses_supervisor_pin_over_new_current(self):
        old = os.environ.get("RIG_OBSERVER_RUN_DIR")
        try:
            os.environ["RIG_OBSERVER_RUN_DIR"] = str(self.root / "pinned")
            self.assertEqual(graph_snapshot.resolve_out_dir(str(self.root), "logger"),
                             str(self.root / "pinned/graph/logger"))
        finally:
            if old is None:
                os.environ.pop("RIG_OBSERVER_RUN_DIR", None)
            else:
                os.environ["RIG_OBSERVER_RUN_DIR"] = old


if __name__ == "__main__":
    unittest.main()
