"""Real Compose rendering, intercepted launches: no daemon, images or containers required."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("ros2-bag-player", "ros2-bag-logger", "ros1-bag-logger", "zenoh-router")
sys.path.insert(0, str(ROOT / "ros2-bag-player/tools"))
import reindex

DOCKER = '''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
record = {"args": args}
if args[0] != "compose":
    raise SystemExit("only Compose is allowed")
if "config" in args:
    result = subprocess.run([os.environ["REAL_DOCKER"], *args], capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    record["model"] = json.loads(result.stdout)
    print(result.stdout)
elif args[:3] == ["compose", "-f", "-"] and args[3] in ("up", "run"):
    raw = sys.stdin.read()
    record["raw"] = json.loads(raw)
    result = subprocess.run([os.environ["REAL_DOCKER"], "compose", "-f", "-", "config", "--format", "json"],
                            input=raw, capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    record["model"] = json.loads(result.stdout)
else:
    raise SystemExit("unexpected Docker operation: " + repr(args))
with open(os.environ["DOCKER_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
'''


class RuntimeImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docker = shutil.which("docker")
        if not cls.docker or subprocess.run([cls.docker, "compose", "version"],
                                           capture_output=True).returncode:
            raise unittest.SkipTest("Docker Compose CLI is required; the daemon is never used")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("RIG_", "COMPOSE_", "BAG_", "ZENOH_", "CALL_INJECTOR_"))
                    and k != "DOCKER_DEFAULT_PLATFORM"}
        self.env.update(PATH=f"{self.bin}:{self.env.get('PATH', '')}", REAL_DOCKER=self.docker,
                        DOCKER_LOG=str(self.root / "docker.jsonl"), BAG_PLAYER_PYTHON=sys.executable,
                        BAG_LOGGER_PYTHON=sys.executable, ZENOH_ROUTER_PYTHON=sys.executable,
                        RIG_DATA_DIR=str(self.root / "never-created-data"))
        self.bag = self.root / "run/bags/bag_logger/session"
        self.bag.mkdir(parents=True)
        (self.bag / "data.mcap").write_bytes(b"fixture")
        (self.bag / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
            "starting_time": {"nanoseconds_since_epoch": 100_000_000_000},
            "duration": {"nanoseconds": 10_000_000_000}}}))

    def service(self, name, **config):
        repo = self.root / name
        if not repo.exists():
            shutil.copytree(ROOT / name, repo, ignore=shutil.ignore_patterns("var", "__pycache__"))
        cfg = dict(name="fixture", **config)
        if name == "ros2-bag-player":
            cfg.setdefault("source", {"run": str(self.root / "run"), "logger": "bag_logger", "session": "session"})
            cfg.setdefault("play", {"topics": ["/points"]})
        path = self.root / (name + ".yaml")
        path.write_text(yaml.safe_dump(cfg))
        return repo, path

    def launch(self, name, verb, *, extra=None, args=(), **config):
        repo, cfg = self.service(name, **config)
        proc = subprocess.run([str(repo / (name + "-up")), str(cfg), verb, *args],
                              env={**self.env, **(extra or {})}, capture_output=True, text=True, timeout=20)
        return proc, repo

    def logs(self):
        path = Path(self.env["DOCKER_LOG"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def pins(self, *images):
        return {"RIG_RUNTIME_IMAGE_PINS": json.dumps({"version": 1,
                "images": {image: "sha256:" + str(i + 1) * 64 for i, image in enumerate(images)}})}

    def sidecar_image(self, name, sidecar):
        repo, _ = self.service(name)
        path = repo / "docker/compose.deploy.yaml"
        model = yaml.safe_load(path.read_text())
        model["services"][sidecar]["image"] = "sidecar:chosen"
        model["services"][sidecar]["build"] = {"context": str(self.root)}
        path.write_text(yaml.safe_dump(model))

    def test_queries_are_read_only_and_resolve_real_overrides_for_all_bundles(self):
        for name in SERVICES:
            with self.subTest(service=name):
                override = "ZENOH_ROUTER_IMAGE" if name == "zenoh-router" else (
                    "BAG_PLAYER_IMAGE" if name == "ros2-bag-player" else "BAG_LOGGER_IMAGE")
                config = {"router_config": {"mode": "router"}} if name == "zenoh-router" else {}
                extra = {override: "localhost:5000/actual@sha256:" + "a" * 64,
                         "RIG_MSGS_IMAGE": "unused:msgs", "RIG_BASE_IMAGE": "unused:base"}
                proc, repo = self.launch(name, "runtime-images", extra=extra, **config)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                expected = {"version": 1, "images": [extra[override]]}
                if name == "ros2-bag-player":
                    expected["roles"] = {"bag-player": extra[override]}
                self.assertEqual(json.loads(proc.stdout), expected)
                proc, _ = self.launch(name, "images", extra=extra, **config)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(yaml.safe_load(proc.stdout), [extra[override]])
                self.assertFalse((repo / "var").exists())
        self.assertFalse(Path(self.env["RIG_DATA_DIR"]).exists())
        self.assertTrue(all("config" in row["args"] for row in self.logs()))

    def test_profiles_include_only_selected_sidecars(self):
        for name, sidecar, config in (("ros2-bag-player", "call-injector", {"calls": "/calls.yaml"}),
                ("ros2-bag-logger", "graph-snapshotter", {"graph": {"enabled": True}})):
            self.sidecar_image(name, sidecar)
            proc, _ = self.launch(name, "runtime-images")
            self.assertEqual(json.loads(proc.stdout)["images"], ["fleet-ros:latest"], proc.stderr)
            proc, _ = self.launch(name, "runtime-images", **config)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["images"], ["fleet-ros:latest", "sidecar:chosen"])

    def test_plan_query_needs_no_bag_and_clears_standalone_calls(self):
        self.sidecar_image("ros2-bag-player", "call-injector")
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"version": 1, "source": "/missing-source", "topics": [],
            "bag": {"logger": "logger", "session": "exact"}, "services": []}))
        proc, repo = self.launch("ros2-bag-player", "runtime-images", calls="/ignored.yaml",
                                extra={"RIG_REPLAY_PLAN": str(plan)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["images"], ["fleet-ros:latest"])
        self.assertFalse((repo / "var").exists())

    def test_pinned_up_freezes_active_model_and_preserves_shell_dollars(self):
        self.sidecar_image("ros2-bag-player", "call-injector")
        proc, _ = self.launch("ros2-bag-player", "up", calls="/calls.yaml", args=("-d",),
                             extra=self.pins("fleet-ros:latest", "sidecar:chosen"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inspection, launch = self.logs()
        self.assertEqual(launch["args"], ["compose", "-f", "-", "up", "--no-build", "--pull", "never", "-d"])
        services = launch["model"]["services"]
        self.assertEqual(set(services), {"bag-player", "call-injector"})
        self.assertEqual(services["bag-player"]["image"], "sha256:" + "1" * 64)
        self.assertEqual(services["call-injector"]["image"], "sha256:" + "2" * 64)
        for name, service in services.items():
            self.assertEqual(service["pull_policy"], "never")
            self.assertNotIn("build", service)
            self.assertNotIn("profiles", service)
            self.assertEqual(service["command"], inspection["model"]["services"][name]["command"])

    def test_coordinated_launch_has_session_labels_and_bound_control_directory(self):
        extra = dict(self.pins("fleet-ros:latest"), RIG_REPLAY_CONTROL_DIR=str(self.root / "control"),
            RIG_REPLAY_SESSION_ID="a"*32, RIG_REPLAY_PLAN_ID="b"*64, RIG_REPLAY_PARTICIPANT="fixture")
        proc, repo = self.launch("ros2-bag-player", "up", calls="/calls.yaml", play={"topics": ["/points"], "rate": 2},
            extra=dict(extra, CALL_INJECTOR_RATE="999", ZENOH_CONFIG_OVERRIDE='connect/endpoints=["tcp/localhost:17747"]'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        services = self.logs()[-1]["model"]["services"]
        for service in services.values():
            self.assertEqual(service["labels"]["io.rig.replay.session"], "a"*32)
            self.assertEqual(service["labels"]["io.rig.replay.plan"], "b"*64)
            self.assertEqual(service["environment"]["RIG_REPLAY_CONTROL_DIR"], "/replay-control")
            self.assertTrue(any(m["target"] == "/replay-control" for m in service["volumes"]))
        self.assertEqual(str(services["call-injector"]["environment"]["CALL_INJECTOR_RATE"]), "2")
        self.assertEqual(services["call-injector"]["environment"]["ZENOH_CONFIG_OVERRIDE"],
                         'connect/endpoints=["tcp/localhost:17747"]')
        self.assertEqual(services["call-injector"]["command"], ["/bin/bash", "/calls.sh"])
        self.assertTrue((repo / "var/run/fixture/calls.sh").is_file())
        script = (repo / "var/run/fixture/play.sh").read_text()
        self.assertIn("exec python3 /coordinated_player.py", script)
        self.assertIn("--start-paused", script)
        self.assertNotIn("python3 /release_gate.py", script)

    def test_other_bundles_enforce_image_pins(self):
        for name in SERVICES[1:]:
            image = "ros1-bridge:latest" if name == "ros1-bag-logger" else "fleet-ros:latest"
            proc, _ = self.launch(name, "up", args=("-d",), extra=self.pins(image))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(all(service["image"] == "sha256:" + "1" * 64
                                for service in self.logs()[-1]["model"]["services"].values()))

    def test_invalid_mismatched_pins_and_override_flags_never_launch(self):
        invalid = ["", "null", json.dumps({"version": True, "images": {}}),
                   json.dumps({"version": 1, "images": {"fleet-ros:latest": "tag:mutable"}}),
                   self.pins("different:tag")["RIG_RUNTIME_IMAGE_PINS"],
                   self.pins("fleet-ros:latest", "inactive:tag")["RIG_RUNTIME_IMAGE_PINS"]]
        for raw in invalid:
            proc, _ = self.launch("zenoh-router", "up", extra={"RIG_RUNTIME_IMAGE_PINS": raw})
            self.assertNotEqual(proc.returncode, 0, (raw, proc.stdout, proc.stderr))
        for args in (("--build",), ("--build=true",), ("--no-recreate",), ("--no-start",),
                     ("--scale", "router=0"), ("--pull", "always"), ("--pull=always",), ("router",)):
            proc, _ = self.launch("zenoh-router", "up", args=args, extra=self.pins("fleet-ros:latest"))
            self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(all("config" in row["args"] for row in self.logs()))

    def test_export_calls_uses_frozen_image_without_pull_or_build(self):
        # A one-shot may use a subset of the already verified up-set's images.
        proc, _ = self.launch("ros2-bag-player", "export-calls",
                             extra=self.pins("fleet-ros:latest", "sidecar:chosen"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        launch = self.logs()[-1]
        self.assertEqual(launch["args"][:6], ["compose", "-f", "-", "run", "--pull", "never"])
        self.assertEqual(launch["model"]["services"]["bag-player"]["image"], "sha256:" + "1" * 64)

    def test_reindex_uses_same_pinned_image_and_rejects_missing_pin(self):
        args = reindex.command(self.bag, self.pins("fleet-ros:latest"))
        self.assertIn("sha256:" + "1" * 64, args)
        self.assertIn("--pull=never", args)
        with self.assertRaisesRegex(SystemExit, "no pin"):
            reindex.command(self.bag, self.pins("different:tag"))

    def test_platform_cannot_override_validated_native_architecture(self):
        repo, _ = self.service("zenoh-router")
        path = repo / "docker/compose.deploy.yaml"
        model = yaml.safe_load(path.read_text())
        for platform, valid in (("linux/amd64", False), ("linux/arm64/v8", True)):
            model["services"]["router"]["platform"] = platform
            path.write_text(yaml.safe_dump(model))
            for verb in ("runtime-images", "up"):
                proc, _ = self.launch("zenoh-router", verb,
                    extra={"RIG_TARGET_ARCH": "arm64", **self.pins("fleet-ros:latest")})
                self.assertEqual(proc.returncode == 0, valid, proc.stderr)
                if not valid:
                    self.assertIn("does not match native", proc.stderr)
        self.assertEqual(sum("up" in row["args"] for row in self.logs()), 1)
        self.assertEqual(self.logs()[-1]["model"]["services"]["router"]["platform"], "linux/arm64")
        model["services"]["router"].pop("platform")
        path.write_text(yaml.safe_dump(model))
        for verb in ("runtime-images", "up"):
            proc, _ = self.launch("zenoh-router", verb,
                extra={"RIG_TARGET_ARCH": "arm64", "DOCKER_DEFAULT_PLATFORM": "linux/amd64",
                       **self.pins("fleet-ros:latest")})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not match native", proc.stderr)
        with self.assertRaisesRegex(SystemExit, "does not match native"):
            reindex.command(self.bag, {"RIG_TARGET_ARCH": "arm64", "DOCKER_DEFAULT_PLATFORM": "linux/amd64",
                                      **self.pins("fleet-ros:latest")})

    def test_standalone_bundles_include_identical_helpers_and_declared_verbs(self):
        helper = (ROOT / "ros2-bag-player/tools/runtime_images.py").read_bytes()
        for name in SERVICES:
            descriptor = yaml.safe_load((ROOT / name / "rigging.yaml").read_text())
            self.assertEqual(descriptor["verbs"]["runtime-images"], "runtime-images")
            self.assertEqual(descriptor["verbs"]["images"], "images")
            self.assertIn("tools/runtime_images.py", descriptor["launch_surface"])
            self.assertEqual((ROOT / name / "tools/runtime_images.py").read_bytes(), helper)


if __name__ == "__main__":
    unittest.main()
