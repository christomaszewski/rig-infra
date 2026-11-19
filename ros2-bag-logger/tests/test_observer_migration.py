"""Legacy capture keeps working while migration is deprecated, without ROS/Docker startup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import bag_cmd


class LegacyConfigTests(unittest.TestCase):
    def test_defaults_and_original_intervals(self):
        graph, system = bag_cmd.legacy_observer_params({})
        self.assertFalse(graph["enabled"])
        self.assertFalse(system["enabled"])
        graph, system = bag_cmd.legacy_observer_params({
            "name": "9-old.logger", "graph": {"enabled": True, "interval_s": 15, "settle_s": 0, "endpoints": False},
            "system": {"enabled": True, "interval_s": .25, "settings_interval_s": 10}})
        self.assertEqual(graph, {"enabled": True, "interval_s": 15, "settle_s": 0, "endpoints": False})
        self.assertEqual(system, {"enabled": True, "interval_s": .25, "settings_interval_s": 10})
        graph, _ = bag_cmd.legacy_observer_params({"graph": {"interval_s": None, "settle_s": None}})
        self.assertEqual((graph["interval_s"], graph["settle_s"]), (None, None))

    def test_released_graph_gate_and_timings(self):
        # Old graph.enabled tested the truthiness of the parsed YAML, even for quoted strings.
        for gate, enabled in ((None, False), (0, False), ("", False), (1, True),
                              ("true", True), ("false", True)):
            with self.subTest(gate=gate):
                graph, _ = bag_cmd.legacy_observer_params({"graph": {"enabled": gate}})
                self.assertIs(graph["enabled"], enabled)
        # A positive infinite delay is an interruptible wait in the released graph worker.
        graph, _ = bag_cmd.legacy_observer_params({"graph": {"interval_s": float("inf"),
                                                             "settle_s": float("inf")}})
        self.assertEqual((graph["interval_s"], graph["settle_s"]), (float("inf"), float("inf")))

    def test_strict_keys_boolean_types_and_finite_intervals(self):
        cases = (
            {"graph": []}, {"graph": {"enable": True}},
            {"graph": {"interval_s": .5}}, {"graph": {"interval_s": True}},
            {"graph": {"interval_s": float("nan")}}, {"graph": {"settle_s": -1}},
            {"graph": {"settle_s": float("-inf")}}, {"graph": {"endpoints": "false"}},
            {"system": []}, {"system": {"enable": True}}, {"system": {"enabled": 1}},
            {"system": {"interval_s": .05}}, {"system": {"settings_interval_s": .5}},
            {"system": {"settings_interval_s": float("inf")}},
            {"name": "../escape", "graph": {"enabled": True}},
        )
        for cfg in cases:
            with self.subTest(cfg=cfg), self.assertRaises(SystemExit):
                bag_cmd.legacy_observer_params(cfg)
            bag_cmd.legacy_observer_params(cfg, control=True)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "logger"
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns("var", "__pycache__"))
        binaries = self.root / "bin"
        binaries.mkdir()
        docker = binaries / "docker"
        docker.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
stdin = sys.stdin.read() if "-" in sys.argv else None
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "project": os.environ["COMPOSE_PROJECT_NAME"],
       "stdin": stdin, "env": {key: value for key, value in os.environ.items()
       if key.startswith(("GRAPH_SNAPSHOT_", "SYSTEM_SNAPSHOT_", "OBSERVER_"))}}) + "\\n")
if "config" in sys.argv:
    services = {"bag-logger": {"image": "fleet-ros:test"}}
    if "legacy-observer" in sys.argv:
        services["graph-snapshotter"] = {"image": "fleet-ros:test",
            "environment": {"GRAPH_SNAPSHOT_NAME": os.environ["GRAPH_SNAPSHOT_NAME"],
                            "OBSERVER_DIAGNOSTICS_ENABLED": "false"},
            "volumes": [{"type": "bind", "source": os.environ["MIRROR_ROOT"] + "/graph_snapshot.py",
                         "target": "/graph_snapshot.py", "read_only": True}]}
    print(json.dumps({"services": services}))
''')
        docker.chmod(0o755)
        (self.repo / "tools/observer/host_probe.py").write_text('''import json, os, sys
with open(os.environ["PROBE_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("PROBE_RC", "0")))
''')
        self.log = self.root / "calls.jsonl"
        self.probes = self.root / "probes.jsonl"
        self.env = {**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"],
                    "CALL_LOG": str(self.log), "BAG_LOGGER_PYTHON": sys.executable,
                    "COMPOSE_PROJECT_NAME": "logger-vehicle-test", "PROBE_LOG": str(self.probes),
                    "MIRROR_ROOT": str(self.repo / "tools/observer"), "RIG_DATA_DIR": str(self.root / "data"),
                    "GRAPH_SNAPSHOT_INTERVAL_S": "999", "GRAPH_SNAPSHOT_ENABLED": "true",
                    "SYSTEM_SNAPSHOT_ENABLED": "true", "OBSERVER_NAME": "inherited_invalid-name",
                    "OBSERVER_RECORDING_ENABLED": "false", "OBSERVER_DIAGNOSTICS_ENABLED": "true"}
        for key in ("RIG_RUNTIME_IMAGE_PINS", "RIG_TARGET_ARCH", "DOCKER_DEFAULT_PLATFORM", "RIG_SIM_TIME"):
            self.env.pop(key, None)

    def launch(self, cfg, verb, *args):
        config = self.root / "config.yaml"
        config.write_text(yaml.safe_dump({"service": "ros2-bag-logger", "name": "logger", **cfg}))
        self.log.unlink(missing_ok=True)
        result = subprocess.run([str(self.repo / "ros2-bag-logger-up"), str(config), verb, *args],
                                env=self.env, capture_output=True, text=True, timeout=10)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_enabled_legacy_config_warns_and_keeps_existing_sidecar(self):
        for key in ("graph", "system"):
            for verb in ("up", "config", "images", "runtime-images"):
                with self.subTest(key=key, verb=verb):
                    result, calls = self.launch({key: {"enabled": True}}, verb)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("DEPRECATED", result.stderr)
                    self.assertIn("Migrate", result.stderr)
                    self.assertEqual(len(calls), 1)
                    self.assertIn("legacy-observer", calls[0]["args"])
                    self.assertNotIn("rm", calls[0]["args"])
                    self.assertEqual(calls[0]["project"], "logger-vehicle-test")

    def test_invalid_legacy_startup_fails_before_mutation(self):
        result, calls = self.launch({"graph": {"enabled": True, "interval_s": float("nan")}}, "up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("graph.interval_s must be a number >= 1", result.stderr)
        self.assertEqual(calls, [])
        self.assertFalse((self.repo / "var").exists())
        self.assertFalse(self.probes.exists())

    def test_control_verbs_always_include_sidecar_and_never_write_or_probe(self):
        configs = ({}, {"graph": {"enabled": True}, "system": {"enabled": True}},
                   {"graph": {"interval_s": "broken"}, "system": []})
        for cfg in configs:
            for verb in ("down", "status", "logs", "ps", "stop", "rm"):
                with self.subTest(cfg=cfg, verb=verb):
                    result, calls = self.launch(cfg, verb)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]["project"], "logger-vehicle-test")
                    self.assertIn("legacy-observer", calls[0]["args"])
                    self.assertEqual(calls[0]["args"][-1], "ps" if verb == "status" else verb)
        self.assertFalse(self.probes.exists())
        self.assertFalse((self.repo / "var").exists())

    def test_migrated_or_disabled_up_removes_only_own_sidecar_then_starts_recorder(self):
        for cfg in ({}, {"graph": {"enabled": False}, "system": {"enabled": False}}):
            result, calls = self.launch(cfg, "up")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["args"][-4:], ["rm", "--stop", "--force", "graph-snapshotter"])
            self.assertNotIn("--volumes", calls[0]["args"])
            self.assertEqual(calls[1]["args"][-1], "up")
            self.assertNotIn("--profile", calls[1]["args"])
            self.assertNotIn("graph-snapshotter", calls[1]["args"])
            self.assertTrue(all(call["project"] == "logger-vehicle-test" for call in calls))
        self.assertFalse(self.probes.exists())

    def test_passthrough_verbs_can_target_the_old_service(self):
        for verb, args in (("exec", ("graph-snapshotter", "pwd")), ("restart", ("graph-snapshotter",))):
            with self.subTest(verb=verb):
                result, calls = self.launch({"graph": {"enabled": True}}, verb, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                command = calls[0]["args"]
                self.assertIn(str(self.repo / "docker/compose.legacy-observer.yaml"), command)
                self.assertNotIn("--profile", command)
                self.assertEqual(command[command.index(verb):], [verb, *args])
                if shutil.which("docker"):
                    # Resolve the same files with the real CLI, without contacting a daemon.
                    env = {**self.env, **calls[0]["env"], "PATH": os.environ["PATH"],
                           "BAG_RECORD_SCRIPT": str(self.repo / "var/run/logger/record.sh")}
                    render = subprocess.run(["docker", *command[:command.index(verb)], "config",
                                             "graph-snapshotter"], env=env, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(render.returncode, 0, render.stderr)
                    self.assertEqual(set(yaml.safe_load(render.stdout)["services"]), {"graph-snapshotter"})

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI needed for actual Compose rendering")
    def test_released_timing_precedence_with_a_legacy_gate(self):
        self.env["PATH"] = os.environ["PATH"]
        self.env["GRAPH_SNAPSHOT_INTERVAL_S"] = "8.5"
        self.env["GRAPH_SNAPSHOT_SETTLE_S"] = "0.5"
        cases = (
            ({}, ("8.5", "0.5")),
            ({"interval_s": None, "settle_s": None}, ("8.5", "0.5")),
            ({"interval_s": 3.75, "settle_s": 0}, ("3.75", "0")),
        )
        for overlay, expected in cases:
            with self.subTest(overlay=overlay):
                result, _ = self.launch({"graph": {"enabled": "true", **overlay}}, "config")
                self.assertEqual(result.returncode, 0, result.stderr)
                env = yaml.safe_load(result.stdout)["services"]["graph-snapshotter"]["environment"]
                self.assertEqual((env["GRAPH_SNAPSHOT_INTERVAL_S"], env["GRAPH_SNAPSHOT_SETTLE_S"]), expected)
        del self.env["GRAPH_SNAPSHOT_INTERVAL_S"]
        del self.env["GRAPH_SNAPSHOT_SETTLE_S"]
        result, _ = self.launch({"graph": {"enabled": True}}, "config")
        self.assertEqual(result.returncode, 0, result.stderr)
        env = yaml.safe_load(result.stdout)["services"]["graph-snapshotter"]["environment"]
        self.assertEqual((env["GRAPH_SNAPSHOT_INTERVAL_S"], env["GRAPH_SNAPSHOT_SETTLE_S"]), ("60", "2"))

    def test_host_probe_runs_only_for_actual_system_up_and_is_fail_soft(self):
        cfg = {"system": {"enabled": True}}
        for verb in ("config", "images", "runtime-images", "down", "status", "logs"):
            result, _ = self.launch(cfg, verb)
            self.assertEqual(result.returncode, 0, result.stderr)
        result, _ = self.launch({"graph": {"enabled": True}}, "up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.probes.exists())
        self.env["PROBE_RC"] = "1"
        result, calls = self.launch(cfg, "up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native host probe failed", result.stderr)
        self.assertEqual(calls[-1]["args"][-1], "up")
        self.assertEqual(json.loads(self.probes.read_text()),
                         ["--data-root", str(self.root / "data"), "--name", "logger"])

    def test_inspection_without_generated_files_and_pins_keep_both_services(self):
        cfg = {"graph": {"enabled": True}}
        result, calls = self.launch(cfg, "runtime-images")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["images"], ["fleet-ros:test"])
        self.assertFalse((self.repo / "var").exists())
        image_id = "sha256:" + "c" * 64
        self.env["RIG_RUNTIME_IMAGE_PINS"] = json.dumps({"version": 1, "images": {"fleet-ros:test": image_id}})
        result, calls = self.launch(cfg, "up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("config", calls[0]["args"])
        self.assertEqual(calls[1]["args"], ["compose", "-f", "-", "up", "--no-build", "--pull", "never"])
        services = json.loads(calls[1]["stdin"])["services"]
        self.assertEqual(set(services), {"bag-logger", "graph-snapshotter"})
        self.assertTrue(all(service["image"] == image_id for service in services.values()))
        self.assertEqual(services["graph-snapshotter"]["volumes"][0]["source"],
                         str(self.repo / "tools/observer/graph_snapshot.py"))

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI needed for actual Compose rendering")
    def test_real_compose_preserves_paths_intervals_and_forces_diagnostics_off(self):
        self.env["PATH"] = os.environ["PATH"]
        for graph, system in ((False, False), (True, False), (False, True), (True, True)):
            cfg = {"name": "9-old.logger", "control": {"node_name": "logger"},
                   "graph": {"enabled": graph, "interval_s": 13, "settle_s": 0, "endpoints": False},
                   "system": {"enabled": system, "interval_s": .25, "settings_interval_s": 10}}
            result, _ = self.launch(cfg, "config")
            self.assertEqual(result.returncode, 0, result.stderr)
            services = yaml.safe_load(result.stdout)["services"]
            self.assertEqual(set(services), {"bag-logger", "graph-snapshotter"} if graph or system else {"bag-logger"})
            if not (graph or system):
                continue
            sidecar = services["graph-snapshotter"]
            env = sidecar["environment"]
            self.assertEqual(env["GRAPH_SNAPSHOT_NAME"], "9-old.logger")
            self.assertEqual(env["SYSTEM_SNAPSHOT_NAME"], "9-old.logger")
            self.assertEqual(env["GRAPH_SNAPSHOT_ENABLED"], str(graph).lower())
            self.assertEqual(env["SYSTEM_SNAPSHOT_ENABLED"], str(system).lower())
            self.assertEqual(env["GRAPH_SNAPSHOT_INTERVAL_S"], "13")
            self.assertEqual(env["GRAPH_SNAPSHOT_SETTLE_S"], "0")
            self.assertEqual(env["GRAPH_SNAPSHOT_ENDPOINTS_ENABLED"], "false")
            self.assertEqual(env["SYSTEM_SNAPSHOT_INTERVAL_S"], "0.25")
            self.assertEqual(env["SYSTEM_SNAPSHOT_SETTINGS_INTERVAL_S"], "10")
            self.assertEqual(env["OBSERVER_DIAGNOSTICS_ENABLED"], "false")
            self.assertEqual(env["OBSERVER_RECORDING_ENABLED"], "true")
            self.assertEqual(env["OBSERVER_NAME"], "")
            self.assertEqual(env["OBSERVER_NATIVE_PROBE_FILE"], "")
            self.assertTrue(sidecar["init"])
            self.assertEqual(sidecar["stop_grace_period"], "5s")
            mounts = {row["target"]: row for row in sidecar["volumes"]}
            self.assertNotIn("/diagnostics_publish.py", mounts)
            self.assertNotIn("/native-host", mounts)
            for target in ("/host/proc", "/host/sys"):
                self.assertEqual(target in mounts, system)
                if system:
                    self.assertTrue(mounts[target]["read_only"])
                    self.assertFalse(mounts[target].get("bind", {}).get("create_host_path", False))
            self.assertEqual(mounts["/data"]["source"], str(self.root / "data"))
            self.assertTrue(all(Path(row["source"]).is_file() for target, row in mounts.items()
                                if target.endswith(".py")))
        self.assertFalse(self.probes.exists())


if __name__ == "__main__":
    unittest.main()
