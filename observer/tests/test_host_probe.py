"""Read-only host evidence: python3 tests/test_host_probe.py."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

MODULE = Path(__file__).resolve().parents[1] / "tools/host_probe.py"
SPEC = importlib.util.spec_from_file_location("host_probe", MODULE)
host_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host_probe)


def process(stdout=b"", stderr=b"", returncode=0):
    result = Mock()
    result.communicate.return_value = stdout, stderr
    result.returncode = returncode
    result.pid = 123456
    return result


class HostProbeTests(unittest.TestCase):
    def test_output_cache_mode_needs_no_run_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "runtime/native-host.json"
            for sequence in (1, 2):
                with patch.object(host_probe, "collect", return_value={"sequence": sequence}):
                    self.assertEqual(host_probe.main(["--output", str(output)]), 0)
                self.assertEqual(json.loads(output.read_text()), {"sequence": sequence})
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertFalse(list(Path(root).rglob("system")))
            self.assertFalse(list(output.parent.glob(".host-probe-*")))

    def test_only_read_only_commands_are_run_and_package_scope_is_host(self):
        packages = b"nvidia-jetpack\t6.2.1-b38\tarm64\tinstalled\nlibnvinfer10\t10.3\tarm64\tinstalled\nremoved\t1\tarm64\tconfig-files\n"
        with tempfile.TemporaryDirectory() as root, patch.object(host_probe.subprocess, "Popen", side_effect=[
            process(b"NV Power Mode: MAXN\n0\n"), process(b"CPU MinFreq=MaxFreq\n"), process(packages, b"no packages found matching ros-*-zenoh*", 1)
        ]) as popen:
            os_release = Path(root, "etc/os-release")
            os_release.parent.mkdir()
            os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\nSECRET=no\n')
            (os_release.parent / "nv_tegra_release").write_text("# R36 (release), REVISION: 4.3\n")
            snapshot = host_probe.collect(root=root, env={"ROS_DOMAIN_ID": "42", "TOKEN": "do not save"})
        self.assertEqual(snapshot["scope"], "host")
        self.assertEqual(snapshot["probes"]["os_release"]["value"], {"ID": "ubuntu", "VERSION_ID": "22.04"})
        self.assertEqual(snapshot["probes"]["packages"]["status"], "partial")
        self.assertEqual(len(snapshot["probes"]["packages"]["value"]), 2)
        self.assertEqual(snapshot["probes"]["jetpack"]["status"], "ok")
        self.assertEqual(snapshot["probes"]["jetpack"]["value"]["version"], "6.2.1-b38")
        self.assertEqual(snapshot["probes"]["launcher_environment"]["scope"], "launcher")
        self.assertNotIn("TOKEN", snapshot["probes"]["launcher_environment"]["value"])
        self.assertEqual(popen.call_args_list[0].args[0], ["nvpmodel", "-q", "--verbose"])
        self.assertEqual(popen.call_args_list[1].args[0], ["jetson_clocks", "--show"])
        self.assertEqual(popen.call_args_list[2].args[0][0], "dpkg-query")
        self.assertNotIn("*", popen.call_args_list[2].args[0])

    def test_missing_permissions_timeout_and_deadline_are_distinct(self):
        probe = host_probe.Probe()
        with patch.object(host_probe.subprocess, "Popen", side_effect=FileNotFoundError):
            self.assertEqual(probe.command(["absent"])["status"], "missing")
        with patch.object(host_probe.subprocess, "Popen", side_effect=PermissionError):
            self.assertEqual(probe.command(["denied"])["status"], "permission_denied")
        with patch.object(host_probe.subprocess, "Popen", return_value=process(stderr=b"permission denied", returncode=1)):
            self.assertEqual(probe.command(["denied"])["status"], "permission_denied")
        child = process()
        child.communicate.side_effect = [subprocess.TimeoutExpired(["slow"], 0.1), (b"partial", b"")]
        with patch.object(host_probe.subprocess, "Popen", return_value=child), patch.object(host_probe.os, "killpg") as kill:
            result = probe.command(["slow"], max_seconds=0.1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["value"], "partial")
        kill.assert_called_once()
        probe.deadline = 0
        with patch.object(host_probe.subprocess, "Popen") as popen:
            self.assertEqual(probe.command(["skipped"])["status"], "skipped")
            self.assertEqual(probe.read("/etc/os-release")["status"], "skipped")
            popen.assert_not_called()

    def test_jetson_clocks_root_requirement_on_stdout_is_permission_denied(self):
        output = (b"SOC family:tegra234 Machine:NVIDIA Jetson AGX Orin Developer Kit\n"
                  b"Error: Run this script(/usr/bin/jetson_clocks) as a root user\n")
        with patch.object(host_probe.subprocess, "Popen", return_value=process(stdout=output, returncode=1)) as popen:
            result = host_probe.Probe().command(["jetson_clocks", "--show"])
        self.assertEqual(result["status"], "permission_denied")
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["value"], output.decode())
        self.assertNotIn("error", result)
        self.assertEqual(popen.call_args.args[0], ["jetson_clocks", "--show"])

    def test_jetpack_inferred_from_exact_release_without_package_query(self):
        with tempfile.TemporaryDirectory() as root, patch.object(host_probe.subprocess, "Popen", side_effect=FileNotFoundError):
            release = Path(root, "etc/nv_tegra_release")
            release.parent.mkdir()
            release.write_text("# R36 (release), REVISION: 4.3\n")
            snapshot = host_probe.collect(root=root, env={"RIG_TARGET_PLATFORM": "jp7"})
        jetpack = snapshot["probes"]["jetpack"]
        self.assertEqual(jetpack["status"], "inferred")
        self.assertEqual(jetpack["source"], "/etc/nv_tegra_release")
        self.assertEqual(jetpack["value"], {"version": "6.2", "l4t_version": "36.4.3"})

    def test_jetpack_uses_or_cross_checks_l4t_core(self):
        core = [{"name": "nvidia-l4t-core:arm64", "version": "39.2.1-20260806224157"}]
        for release in ({"status": "missing"}, {"status": "ok", "value": "# R39 (release), REVISION: 2.1, BOARD: generic"}):
            result = host_probe.jetpack_probe(release, core)
            self.assertEqual(result["status"], "inferred")
            self.assertEqual(result["value"], {"version": "7.2.1", "l4t_version": "39.2.1"})
            self.assertIn("nvidia-l4t-core", result["source"])
            self.assertEqual("/etc/nv_tegra_release" in result["source"], release["status"] == "ok")
            self.assertEqual(result["reference"], host_probe.JETPACK_ARCHIVE)
        earlier = {"status": "ok", "value": "# R39 (release), REVISION: 2.0"}
        result = host_probe.jetpack_probe(earlier, core)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("disagree", result["error"])
        self.assertIsNone(result["value"])
        self.assertEqual(host_probe.jetpack_probe(earlier, [])["value"]["version"], "7.2")

    def test_jetpack_unknown_for_unmapped_ambiguous_or_malformed_releases(self):
        for major, revision in (("39", "2.10"), ("32", "2.1"), ("39", "2.1rc1"), ("UNKNOWN", "2.1")):
            with self.subTest(major=major, revision=revision):
                result = host_probe.jetpack_probe({"status": "ok", "value": f"# R{major} (release), REVISION: {revision}"}, [])
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["value"])
        result = host_probe.jetpack_probe({"status": "missing"}, [])
        self.assertEqual(result["status"], "unknown")

    def test_commands_share_one_deadline(self):
        first, second = process(), process()
        with patch.object(host_probe.time, "monotonic", side_effect=[100.0, 100.7, 102.9, 103.1]), \
                patch.object(host_probe.subprocess, "Popen", side_effect=[first, second]) as popen:
            probe = host_probe.Probe(budget=3)
            probe.command(["first"])
            probe.command(["second"])
            self.assertEqual(probe.command(["third"])["status"], "skipped")
        self.assertEqual(popen.call_count, 2)
        self.assertAlmostEqual(first.communicate.call_args.kwargs["timeout"], 0.9)
        self.assertAlmostEqual(second.communicate.call_args.kwargs["timeout"], 0.1)

    def test_pin_run_once_and_publish_new_files_without_replacing(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            one, two = root / "runs/one", root / "runs/two"
            one.mkdir(parents=True)
            two.mkdir()
            (root / "current").symlink_to("runs/one")
            directory = host_probe.output_directory(root, "logger")
            (root / "current").unlink()
            (root / "current").symlink_to("runs/two")
            first = host_probe.write_snapshot(directory, {"which": 1})
            second = host_probe.write_snapshot(directory, {"which": 2})
            self.assertEqual(first.parent, one / "system/logger/host-probes")
            self.assertNotEqual(first, second)
            self.assertEqual(json.loads(first.read_text()), {"which": 1})
            self.assertEqual(json.loads(second.read_text()), {"which": 2})
            self.assertEqual(first.stat().st_mode & 0o777, 0o644)
            self.assertFalse(list(directory.glob(".host-probe-*")))
            self.assertFalse((two / "system").exists())

    def test_flat_layout_invalid_names_and_failed_capture_are_best_effort(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(host_probe.output_directory(root, "logger"), Path(root).resolve() / "system/logger/host-probes")
            for name in ("../escape", ".", "..", "bad/name"):
                with self.assertRaises(ValueError):
                    host_probe.output_directory(root, name)
            with patch.object(host_probe, "write_snapshot", side_effect=PermissionError("read only")), \
                    patch.object(host_probe, "collect", return_value={}), patch.object(host_probe.sys, "stderr"):
                self.assertEqual(host_probe.main(["--data-root", root, "--name", "logger"]), 0)
            with patch.object(host_probe, "collect", return_value={"example": True}):
                self.assertEqual(host_probe.main(["--data-root", root, "--name", "logger"]), 0)
            self.assertEqual(len(list(Path(root).glob("system/logger/host-probes/*.json"))), 1)
            missing = Path(root) / "new-standalone-root"
            directory = host_probe.output_directory(missing, "logger")
            self.assertTrue(missing.is_dir())
            self.assertEqual(directory, missing.resolve() / "system/logger/host-probes")

    def test_allowlisted_tuning_redacts_secret_fragments_and_url_credentials(self):
        env = {"ZENOH_CONFIG_OVERRIDE": "transport/shared_memory/enabled=true;auth/password=hidden;link/url=tcp://user:pass@host",
               "ZENOH_SESSION_CONFIG_URI": "https://user:pass@host/config?token=private&normal=1",
               "AWS_SECRET_ACCESS_KEY": "private", "OMP_NUM_THREADS": "4"}
        result = host_probe.sanitized_environment(env)
        self.assertEqual(result["OMP_NUM_THREADS"], "4")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", result)
        self.assertIn("enabled=true", result["ZENOH_CONFIG_OVERRIDE"])
        self.assertNotIn("hidden", result["ZENOH_CONFIG_OVERRIDE"])
        self.assertNotIn("user:pass", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
