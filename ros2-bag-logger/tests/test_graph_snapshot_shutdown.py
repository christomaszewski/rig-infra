"""Real process signals + real atomic writes, with only ROS stubbed out.

Run: python3 tests/test_graph_snapshot_shutdown.py (no ROS needed).
Children announce the exact shutdown boundary; no timing guesses or long interval sleeps.
"""
import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import tempfile
import types
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import graph_snapshot as gs


def child(mode, root):
    state = {"ok": False, "walks": 0, "writes": 0, "destroyed": False, "shutdown": False}

    def pause():
        print("ready", flush=True)
        assert sys.stdin.readline() == "continue\n"
        assert state["ok"]  # a signal must not invalidate ROS before the write finishes

    class Node:
        context = types.SimpleNamespace(ok=lambda: state["ok"])

        def get_fully_qualified_name(self):
            return "/graph_snapshotter_test"

        def get_node_names_and_namespaces(self):
            assert state["ok"]
            state["walks"] += 1
            nodes = [("talker", "/"), ("graph_snapshotter_test", "/")]
            if mode.endswith("change") and state["walks"] == 2:
                nodes.append(("listener", "/"))
            return nodes

        def get_publisher_names_and_types_by_node(self, name, ns):
            if mode.startswith("walk") and state["walks"] == 2 and name == "talker":
                pause()
            assert state["ok"]
            return [("/chatter", ["std_msgs/msg/String"])] if name == "talker" else []

        def get_subscriber_names_and_types_by_node(self, name, ns):
            assert state["ok"]
            return [("/chatter", ["std_msgs/msg/String"])] if name == "listener" else []

        def get_service_names_and_types_by_node(self, name, ns):
            assert state["ok"]
            return []

        get_client_names_and_types_by_node = get_service_names_and_types_by_node

        def destroy_node(self):
            assert state["ok"]
            state["destroyed"] = True

    rclpy = types.ModuleType("rclpy")
    signals = types.ModuleType("rclpy.signals")
    signals.SignalHandlerOptions = types.SimpleNamespace(NO=object())

    def init(*, signal_handler_options):
        assert signal_handler_options is signals.SignalHandlerOptions.NO
        state["ok"] = True

    def shutdown(*, uninstall_handlers):
        assert not uninstall_handlers
        assert state["destroyed"]
        state["ok"] = False
        state["shutdown"] = True

    rclpy.init = init
    rclpy.create_node = lambda name: Node()
    rclpy.ok = lambda: state["ok"]
    rclpy.try_shutdown = shutdown
    sys.modules.update({"rclpy": rclpy, "rclpy.signals": signals})

    wait = gs.wait_for_stop
    waits = 0

    def announced_wait(seconds, stopping):
        nonlocal waits
        waits += 1
        if mode == "settle" or (mode == "interval" and waits == 2):
            print("ready", flush=True)
            return wait(seconds, stopping)
        # Move straight to the next observation in the in-flight tests.
        return wait(0, stopping)

    gs.wait_for_stop = announced_wait
    replace = gs.os.replace

    def paused_replace(src, dst):
        state["writes"] += 1
        if (mode.startswith("write") and state["writes"] == 2) or mode == "first-write":
            pause()  # tmp is closed; destination still contains the preceding complete epoch
        assert state["ok"]
        replace(src, dst)

    gs.os.replace = paused_replace
    gs._utc = lambda: (f"2026-10-16T12:00:0{state['walks']}Z",
                       f"20261016T12000{state['walks']}Z")
    os.environ.update(RIG_BAG_ROOT=root, GRAPH_SNAPSHOT_NAME="test",
                      GRAPH_SNAPSHOT_SETTLE_S="60" if mode == "settle" else "0",
                      GRAPH_SNAPSHOT_INTERVAL_S="60")
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    assert gs.main() == 0
    assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())
    pathlib.Path(root, "cleanup.json").write_text(json.dumps(state))


class ShutdownTests(unittest.TestCase):
    def check_shutdown(self, mode, sig):
        with tempfile.TemporaryDirectory() as root:
            proc = subprocess.Popen([sys.executable, __file__, "--child", mode, root],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 5)
                self.assertTrue(ready, "child did not reach shutdown boundary")
                self.assertEqual(proc.stdout.readline(), "ready\n")
                out_dir = pathlib.Path(root, "graph", "test")
                before = {p.name: p.read_bytes() for p in out_dir.glob("epoch_*.yaml")}
                self.assertEqual(len(before), 0 if mode in ("settle", "first-write") else 1)
                proc.send_signal(sig)
                # Repeated stop requests must remain graceful while a save is in flight.
                blocked = mode.startswith(("walk", "write")) or mode == "first-write"
                if blocked:
                    proc.send_signal(sig)
                stdout, stderr = proc.communicate("continue\n" if blocked else None, timeout=3)
                self.assertEqual(proc.returncode, 0, stdout + stderr)
                state = json.loads(pathlib.Path(root, "cleanup.json").read_text())
                self.assertTrue(state["destroyed"] and state["shutdown"])
                after = {p.name: p.read_bytes() for p in out_dir.glob("epoch_*.yaml")}
                self.assertFalse(list(out_dir.glob("*.tmp.*")))
                expected_ticks = 0 if mode == "settle" else 1 if mode in ("interval", "first-write") else 2
                self.assertEqual(state["walks"], expected_ticks)
                self.assertEqual(state["writes"], expected_ticks)
                if mode in ("settle", "interval"):
                    self.assertEqual(after, before)  # idle stop cannot invent a new `last:`
                    return
                self.assertEqual(len(after), 2 if mode.endswith("change") else 1)
                docs = [yaml.safe_load(raw) for _, raw in sorted(after.items())]
                newest = docs[-1]
                self.assertEqual(str(newest["last"]), f"2026-10-16 12:00:0{expected_ticks}+00:00")
                self.assertEqual(newest["nodes"]["/talker"]["pubs"],
                                 [{"topic": "/chatter", "type": "std_msgs/msg/String"}])
                self.assertNotIn("/graph_snapshotter_test", newest["nodes"])
                if mode.endswith("change"):
                    self.assertEqual(after[next(iter(before))], next(iter(before.values())))
                    self.assertEqual(newest["nodes"]["/listener"]["subs"],
                                     [{"topic": "/chatter", "type": "std_msgs/msg/String"}])
                else:
                    self.assertEqual(str(newest["first"]), "2026-10-16 12:00:01+00:00")
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()

    def test_sigterm_and_sigint_preserve_observations_and_exit_promptly(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            for mode in ("settle", "interval", "first-write", "walk-bump", "walk-change",
                         "write-bump", "write-change"):
                with self.subTest(signal=sig.name, phase=mode):
                    self.check_shutdown(mode, sig)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
