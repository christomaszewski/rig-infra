"""Pure counting/schema and isolated Docker command regressions; no ROS or daemon needed."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import inspect_calls
import inspect_calls_host


class Inspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bag = self.root / "bag"
        self.bag.mkdir()
        (self.bag / "metadata.yaml").write_text("test")
        self.request = {"version": 1, "session": str(self.bag), "start_ns": 100_000_000_000,
                        "from_s": 0, "to_s": None, "services": ["/test/set_bool"],
                        "source_mode": "service"}
        self.path = self.root / "request.json"
        self.path.write_text(json.dumps(self.request))
        self.env = {"BAG_PLAYER_IMAGE": "registry:5000/custom:tag", "RIG_TARGET_ARCH": "arm64",
                    "RIG_RUNTIME_IMAGE_PINS": json.dumps({"version": 1, "images": {
                        "registry:5000/custom:tag": "sha256:" + "a" * 64}})}

    def test_origin_counts_duplicates_observationally(self):
        summary = inspect_calls.ServiceSummary(type="std_srvs/srv/SetBool")
        identity = (b"a" * 16, 9)
        summary.observe(1, True, identity, "service")
        summary.observe(1, True, identity, "service")
        summary.observe(0, True, identity, "service")
        result = summary.result()
        self.assertEqual(result["status"], "usable")
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["other_origin_request_count"], 1)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertIn("will also be replayed", result["reason"])

    def test_nonusable_statuses_always_have_zero_selected_requests(self):
        cases = [(inspect_calls.ServiceSummary(), "missing"),
                 (inspect_calls.ServiceSummary(type="std_srvs/srv/SetBool"), "empty_window"),
                 (inspect_calls.ServiceSummary(type="bad", failure="unavailable"), "unsupported_type")]
        for event, contents, expected in ((0, True, "other_origin"), (1, False, "metadata_only"),
                                          (2, False, "response_only"), (3, False, "response_only")):
            summary = inspect_calls.ServiceSummary(type="std_srvs/srv/SetBool")
            summary.observe(event, contents, (b"a", 1), "service")
            cases.append((summary, expected))
        for summary, expected in cases:
            self.assertEqual(summary.result()["status"], expected)
            self.assertEqual(summary.result()["request_count"], 0)
        summary = inspect_calls.ServiceSummary(type="std_srvs/srv/SetBool")
        summary.observe(0, True, (b"a", 1), "client")
        self.assertEqual(summary.result()["request_count"], 1)
        with self.assertRaisesRegex(ValueError, "unknown service event"):
            summary.observe(99, False, None, "service")

    def test_schema_rejects_ambiguous_or_unbounded_input(self):
        for changes in ({"version": True}, {"session": "relative"}, {"start_ns": True},
                        {"from_s": float("nan")}, {"to_s": float("inf")}, {"to_s": 0},
                        {"from_s": -1}, {"from_s": 1e30}, {"services": ["relative"]},
                        {"services": ["/a", "/a"]}, {"services": ["/a//b"]},
                        {"services": ["/0bad"]}, {"source_mode": "auto"}, {"extra": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inspect_calls.validate_request({**self.request, **changes})
        self.assertEqual(inspect_calls.load_request(self.path), self.request)
        self.path.write_bytes(b" " * (inspect_calls.MAX_REQUEST_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "256 KiB"):
            inspect_calls.load_request(self.path)

    def test_helper_mounts_exact_sources_readonly_and_uses_native_pinned_image(self):
        cmd = inspect_calls_host.command(self.path, self.env, "test-unique")
        for flag in ("--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--platform=linux/arm64"):
            self.assertIn(flag, cmd)
        self.assertIn("sha256:" + "a" * 64, cmd)
        self.assertNotIn("registry:5000/custom:tag", cmd)
        mounts = [cmd[i + 1] for i, item in enumerate(cmd) if item == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertIn(f"type=bind,src={self.bag},dst=/bag,readonly", mounts)
        self.assertNotIn("compose", cmd)
        self.assertNotIn("/var/run/docker.sock", " ".join(cmd))

    def test_helper_rejects_missing_pins_native_arch_mismatch_and_changed_image(self):
        for env in ({k: v for k, v in self.env.items() if k != "RIG_RUNTIME_IMAGE_PINS"},
                    {**self.env, "RIG_RUNTIME_IMAGE_PINS": ""},
                    {**self.env, "BAG_PLAYER_IMAGE": "other:tag"},
                    {**self.env, "DOCKER_DEFAULT_PLATFORM": "linux/amd64"},
                    {k: v for k, v in self.env.items() if k != "RIG_TARGET_ARCH"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                inspect_calls_host.command(self.path, env, "test-unique")
        alias = self.root / "alias"
        alias.symlink_to(self.bag)
        self.path.write_text(json.dumps({**self.request, "session": str(alias)}))
        with self.assertRaisesRegex(ValueError, "canonical exact bag"):
            inspect_calls_host.command(self.path, self.env, "test-unique")

    def test_timeout_cleans_only_unique_helper_and_propagates_failure(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(
                inspect_calls_host.uuid, "uuid4") as uuid, patch.object(
                inspect_calls_host.subprocess, "run") as run:
            uuid.return_value.hex = "unique123"
            run.side_effect = [subprocess.TimeoutExpired("docker", 90), subprocess.CompletedProcess([], 0)]
            with self.assertRaisesRegex(TimeoutError, "90 seconds"):
                inspect_calls_host.main([str(self.path)])
            self.assertEqual(run.call_args_list[0].kwargs["timeout"], 90)
            self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "-f", "rig-replay-inspect-unique123"])
            self.assertEqual(run.call_args_list[1].kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
