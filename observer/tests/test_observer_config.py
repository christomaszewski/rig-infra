"""Observer modes, pure config rendering and launcher boundaries; no live Docker needed."""
import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import observer_config


class ConfigTests(unittest.TestCase):
    def env(self, cfg=None, sim_time=None):
        return observer_config.config_env(cfg or {}, REPO, sim_time)

    def test_defaults_and_independent_capture_only_and_diagnostics_only(self):
        defaults = self.env()
        for key in ("GRAPH_SNAPSHOT_ENABLED", "SYSTEM_SNAPSHOT_ENABLED", "OBSERVER_RECORDING_ENABLED", "OBSERVER_DIAGNOSTICS_ENABLED"):
            self.assertEqual(defaults[key], "true")
        self.assertEqual(defaults["GRAPH_SNAPSHOT_INTERVAL_S"], "60")
        self.assertEqual(defaults["GRAPH_SNAPSHOT_ENDPOINTS_ENABLED"], "true")
        self.assertEqual(defaults["SYSTEM_SNAPSHOT_INTERVAL_S"], "1")
        capture = self.env({"graph": {"enabled": False}, "diagnostics": {"enabled": False}})
        self.assertEqual(capture["OBSERVER_DIAGNOSTICS_ENABLED"], "false")
        self.assertEqual(capture["OBSERVER_RECORDING_ENABLED"], "true")
        live = self.env({"graph": {"enabled": False}, "recording": {"enabled": False}})
        self.assertEqual(live["OBSERVER_RECORDING_ENABLED"], "false")
        self.assertEqual(live["OBSERVER_DIAGNOSTICS_ENABLED"], "true")
        graph = self.env({"system": {"enabled": False}, "diagnostics": {"enabled": False}})
        self.assertEqual(graph["GRAPH_SNAPSHOT_ENABLED"], "true")
        self.assertEqual(graph["SYSTEM_SNAPSHOT_ENABLED"], "false")

    def test_invalid_modes_and_config_are_rejected(self):
        for cfg in (
            {"recording": {"enabled": False}},
            {"system": {"enabled": False}},
            {"graph": {"enabled": False}, "system": {"enabled": False}, "diagnostics": {"enabled": False}},
            {"graph": []}, {"system": {"enabled": "false"}}, {"recording": {"enable": True}},
            {"service": "bag-logger"}, {"name": "../escape"}, {"name": "has space"},
            {"name": "2health"}, {"name": "rig-health"},
            {"graph": {"interval_s": 0}}, {"graph": {"settle_s": -1}},
            {"graph": {"endpoints": "false"}},
            {"system": {"interval_s": .05}}, {"system": {"settings_interval_s": .5}},
            {"system": {"interval_s": True}}, {"system": {"settings_interval_s": float("nan")}},
            {"diagnostics": {"stale_after_s": float("inf")}},
            {"diagnostics": {"topic": "relative"}}, {"diagnostics": {"topic": "/bad//topic"}},
            {"diagnostics": {"components": ["cpu", "cpu"]}}, {"diagnostics": {"components": ["unknown"]}},
            {"diagnostics": {"publish_during_replay": "false"}},
        ):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                self.env(cfg)

    def test_endpoint_toggle_and_system_minimum_intervals(self):
        env = self.env({"graph": {"endpoints": False},
                        "system": {"interval_s": .1, "settings_interval_s": 1}})
        self.assertEqual(env["GRAPH_SNAPSHOT_ENDPOINTS_ENABLED"], "false")
        self.assertEqual(env["SYSTEM_SNAPSHOT_INTERVAL_S"], "0.1")
        self.assertEqual(env["SYSTEM_SNAPSHOT_SETTINGS_INTERVAL_S"], "1")

    def test_threshold_schema_and_finite_ordering(self):
        config = {"diagnostics": {"thresholds": {
            "cpu.utilization_pct": {"warn": 80, "error": 95},
            "storage.free_bytes": {"warn": 100, "error": 10, "direction": "below"}}}}
        thresholds = json.loads(self.env(config)["OBSERVER_DIAGNOSTICS_THRESHOLDS"])
        self.assertEqual(thresholds["cpu.utilization_pct"]["direction"], "above")
        for setting in ({}, {"error": True}, {"warn": float("nan")}, {"warn": 1, "bad": 2},
                        {"warn": 90, "error": 80}, {"warn": 80, "error": 90, "direction": "below"},
                        {"error": 1, "direction": "around"}):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                self.env({"diagnostics": {"thresholds": {"cpu.utilization_pct": setting}}})
        with self.assertRaises(ValueError):
            self.env({"diagnostics": {"thresholds": {"bad metric": {"warn": 1}}}})

    def test_replay_defaults_suppress_live_publication_with_distinct_optin_identity(self):
        replay = self.env({"name": "rig_health"}, sim_time="true")
        self.assertEqual(replay["OBSERVER_RECORDING_ENABLED"], "true")
        self.assertEqual(replay["OBSERVER_DIAGNOSTICS_ENABLED"], "false")
        opted = self.env({"name": "rig_health", "diagnostics": {"publish_during_replay": True}}, sim_time="true")
        self.assertEqual(opted["OBSERVER_DIAGNOSTICS_ENABLED"], "true")
        self.assertEqual(opted["OBSERVER_DIAGNOSTICS_INSTANCE"], "rig_health_replay_host")
        self.assertEqual(opted["OBSERVER_DIAGNOSTICS_SCOPE"], "replay_host")
        self.assertEqual(self.env(sim_time="false")["OBSERVER_DIAGNOSTICS_ENABLED"], "true")

    def test_existing_native_cache_write_failure_does_not_block_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = root / "observer.yaml"
            cfg.write_text("service: observer\nname: observer\n")
            cache = root / "var/run/observer/native/native-host.json"
            cache.parent.mkdir(parents=True)
            old = '{"collected_at": "2026-11-13T00:00:00Z", "scope": "host"}'
            cache.write_text(old)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(observer_config, "ensure_placeholder", side_effect=PermissionError("read only")), \
                    patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                rc = observer_config.main([str(cfg), str(root), "--mode", "up"])
            self.assertEqual(rc, 0)
            self.assertIn("export OBSERVER_NAME=observer", stdout.getvalue())
            self.assertIn("retaining existing evidence with its original timestamp", stderr.getvalue())
            self.assertEqual(cache.read_text(), old)
            cache.unlink()
            cache.parent.rmdir()
            with patch.object(observer_config, "ensure_placeholder", side_effect=PermissionError("read only")), \
                    patch.object(sys, "stderr", io.StringIO()) as err:
                self.assertEqual(observer_config.main([str(cfg), str(root), "--mode", "up"]), 2)
            self.assertIn("native cache directory must exist and be readable", err.getvalue())

    def test_graph_only_up_does_not_reset_existing_native_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = root / "observer.yaml"
            cfg.write_text("service: observer\nname: observer\nsystem: {enabled: false}\ndiagnostics: {enabled: false}\n")
            with patch.object(observer_config, "ensure_placeholder") as ensure, patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(observer_config.main([str(cfg), str(root), "--mode", "up"]), 0)
            self.assertFalse(ensure.call_args.kwargs["reset"])


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "observer"
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns("var", "__pycache__"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "docker.jsonl"
        self.probes = self.root / "probes.jsonl"
        docker = self.bin / "docker"
        docker.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
stdin = sys.stdin.read() if "-" in sys.argv else None
with open(os.environ["OBSERVER_TEST_DOCKER_LOG"], "a") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin,
      "env": {key: value for key, value in os.environ.items() if key.startswith(("OBSERVER_", "GRAPH_SNAPSHOT_", "SYSTEM_SNAPSHOT_"))}}) + "\\n")
if "config" in sys.argv:
    print(json.dumps({"services": {"observer": {"image": "fleet-ros:test",
      "volumes": [{"type": "bind", "source": os.environ["OBSERVER_NATIVE_PROBE_DIR"], "target": "/native-host", "read_only": True}],
      "environment": {"OBSERVER_RECORDING_ENABLED": os.environ["OBSERVER_RECORDING_ENABLED"]}}}}))
''')
        docker.chmod(0o755)
        (self.repo / "tools/host_probe.py").write_text('''import json, os, sys
with open(os.environ["OBSERVER_TEST_PROBE_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("OBSERVER_TEST_PROBE_RC", "0")))
''')
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
                    "OBSERVER_PYTHON": sys.executable,
                    "OBSERVER_TEST_DOCKER_LOG": str(self.log), "OBSERVER_TEST_PROBE_LOG": str(self.probes),
                    "RIG_DATA_DIR": str(self.root / "data"), "GRAPH_SNAPSHOT_ENABLED": "false",
                    "GRAPH_SNAPSHOT_ENDPOINTS_ENABLED": "false",
                    "SYSTEM_SNAPSHOT_INTERVAL_S": "999", "OBSERVER_DIAGNOSTICS_ENABLED": "false",
                    "OBSERVER_RECORDING_ENABLED": "false", "OBSERVER_DIAGNOSTICS_INSTANCE": "inherited_wrong",
                    "OBSERVER_NATIVE_PROBE_SOURCE": "/inherited/wrong", "OBSERVER_STATE_DIR": "/inherited/state"}
        for key in ("RIG_RUNTIME_IMAGE_PINS", "RIG_SIM_TIME", "RIG_TARGET_ARCH", "DOCKER_DEFAULT_PLATFORM"):
            self.env.pop(key, None)

    def run_launcher(self, cfg=None, verb="config"):
        path = self.root / "observer.yaml"
        path.write_text(yaml.safe_dump({"service": "observer", "name": "health", **(cfg or {})}))
        self.log.unlink(missing_ok=True)
        result = subprocess.run([str(self.repo / "observer-up"), str(path), verb], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return result, calls

    def test_config_resets_inherited_settings_without_probing(self):
        _, calls = self.run_launcher()
        env = calls[-1]["env"]
        self.assertEqual(env["GRAPH_SNAPSHOT_ENABLED"], "true")
        self.assertEqual(env["GRAPH_SNAPSHOT_ENDPOINTS_ENABLED"], "true")
        self.assertEqual(env["SYSTEM_SNAPSHOT_INTERVAL_S"], "1")
        self.assertEqual(env["OBSERVER_DIAGNOSTICS_ENABLED"], "true")
        self.assertEqual(env["OBSERVER_RECORDING_ENABLED"], "true")
        self.assertEqual(env["OBSERVER_DIAGNOSTICS_INSTANCE"], "health")
        self.assertEqual(env["OBSERVER_STATE_DIR"], "/run/observer")
        self.assertFalse(self.probes.exists())
        cache = Path(env["OBSERVER_NATIVE_PROBE_SOURCE"])
        self.assertTrue(json.loads(cache.read_text())["generated"])
        cache.write_text('{"actual_host_evidence": true}')
        self.run_launcher()
        self.assertEqual(json.loads(cache.read_text()), {"actual_host_evidence": True})

    def test_image_inspection_and_controls_never_probe_or_write_cache(self):
        for verb in ("images", "runtime-images", "down", "status", "logs"):
            with self.subTest(verb=verb):
                self.run_launcher(verb=verb)
                self.assertFalse((self.repo / "var").exists())
        self.assertFalse(self.probes.exists())

    def test_actual_up_refreshes_cache_and_native_failure_is_nonfatal(self):
        self.env["OBSERVER_TEST_PROBE_RC"] = "1"
        self.run_launcher()
        cache = self.repo / "var/run/health/native/native-host.json"
        cache.write_text('{"stale_host_evidence": true}')
        result, calls = self.run_launcher(verb="up")
        self.assertIn("native host probe failed", result.stderr)
        self.assertEqual(calls[-1]["argv"][-1], "up")
        self.assertEqual(json.loads(self.probes.read_text()), ["--output", str(cache)])
        self.assertTrue(json.loads(cache.read_text())["generated"])
        self.assertFalse((self.root / "data").exists(), "launcher must not open a run")

    def test_capture_only_diagnostics_only_and_graph_only_compose_flags(self):
        capture = {"graph": {"enabled": False}, "diagnostics": {"enabled": False}}
        _, calls = self.run_launcher(capture)
        self.assertEqual(calls[-1]["env"]["OBSERVER_DIAGNOSTICS_ENABLED"], "false")
        live = {"graph": {"enabled": False}, "recording": {"enabled": False}}
        _, calls = self.run_launcher(live, verb="up")
        self.assertEqual(calls[-1]["env"]["OBSERVER_RECORDING_ENABLED"], "false")
        self.assertTrue(any(arg.endswith("compose.system.yaml") for arg in calls[-1]["argv"]))
        self.probes.unlink()
        graph = {"system": {"enabled": False}, "diagnostics": {"enabled": False}}
        _, calls = self.run_launcher(graph, verb="up")
        self.assertFalse(any(arg.endswith("compose.system.yaml") for arg in calls[-1]["argv"]))
        self.assertFalse(self.probes.exists())

    def test_pinned_up_preserves_native_cache_bind_and_freezes_image(self):
        image_id = "sha256:" + "a" * 64
        self.env["RIG_RUNTIME_IMAGE_PINS"] = json.dumps({"version": 1, "images": {"fleet-ros:test": image_id}})
        _, calls = self.run_launcher(verb="up")
        self.assertEqual(len(calls), 2)
        self.assertIn("config", calls[0]["argv"])
        self.assertEqual(calls[-1]["argv"], ["compose", "-f", "-", "up", "--no-build", "--pull", "never"])
        service = json.loads(calls[-1]["stdin"])["services"]["observer"]
        self.assertEqual(service["image"], image_id)
        self.assertEqual(service["volumes"][0]["source"], str(self.repo / "var/run/health/native"))

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI needed for real Compose rendering")
    def test_real_compose_single_observer_image_chain_and_read_only_host_mounts(self):
        cfg = self.root / "observer.yaml"
        cfg.write_text("service: observer\nname: health\n")
        env = {**self.env, "PATH": os.environ["PATH"], "RIG_IMAGE_TAG": "test", "RIG_IMAGE_REGISTRY": "registry.test",
               "ZENOH_CONFIG_OVERRIDE": 'connect/endpoints=["tcp/127.0.0.1:17450"]'}
        for key in ("OBSERVER_IMAGE", "RIG_MSGS_IMAGE", "RIG_BASE_IMAGE"):
            env.pop(key, None)
        cases = [({}, "registry.test/fleet-ros:test"), ({"RIG_BASE_IMAGE": "custom/base:1"}, "custom/base:1"),
                 ({"RIG_BASE_IMAGE": "custom/base:1", "RIG_MSGS_IMAGE": "custom/msgs:2"}, "custom/msgs:2"),
                 ({"OBSERVER_IMAGE": "custom/observer:3", "RIG_MSGS_IMAGE": "custom/msgs:2"}, "custom/observer:3")]
        for additions, expected in cases:
            result = subprocess.run([str(self.repo / "observer-up"), str(cfg), "config"], env={**env, **additions},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            model = yaml.safe_load(result.stdout)
            self.assertEqual(set(model["services"]), {"observer"})
            observer = model["services"]["observer"]
            self.assertEqual(observer["image"], expected)
            self.assertTrue(observer["init"])
            self.assertEqual(observer["stop_grace_period"], "5s")
            self.assertIn("/run/observer", observer["tmpfs"])
            self.assertEqual(observer["environment"]["ZENOH_CONFIG_OVERRIDE"], env["ZENOH_CONFIG_OVERRIDE"])
            self.assertEqual(observer["environment"]["GRAPH_SNAPSHOT_ENDPOINTS_ENABLED"], "true")
            mounts = {row["target"]: row for row in observer["volumes"]}
            for target in ("/host/proc", "/host/sys", "/native-host"):
                self.assertTrue(mounts[target]["read_only"])
                self.assertFalse(mounts[target].get("bind", {}).get("create_host_path", False))
            self.assertNotIn("/var/run/docker.sock", mounts)
        surface = yaml.safe_load((self.repo / "rigging.yaml").read_text())["launch_surface"]
        self.assertIn("tools/observer_config.py", surface)
        self.assertIn("tools/endpoint_snapshot.py", surface)
        self.assertIn("tools/diagnostics_publish.py", surface)


if __name__ == "__main__":
    unittest.main()
