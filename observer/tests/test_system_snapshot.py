"""Host fixtures plus real worker lifecycle/signals; no Docker, Jetson, or ROS needed."""
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import system_snapshot as ss


class SystemSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.proc, self.sysfs = self.root / "proc", self.root / "sys"
        self.proc.mkdir()
        self.sysfs.mkdir()
        self.data = self.root / "data"
        self.run = self.data / "runs" / "first"
        self.run.mkdir(parents=True)
        (self.data / "current").symlink_to("runs/first")

    def put(self, root, relative, value):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def fixtures(self):
        self.put(self.proc, "stat", "cpu 100 10 20 1000 30 5 5 0 40 4\n"
                 "cpu0 50 5 10 500 15 2 3 0 20 2\n")
        self.put(self.proc, "sys/kernel/random/boot_id", "fixture-boot\n")
        self.put(self.proc, "loadavg", "1.23 2.34 3.45 2/345 678\n")
        self.put(self.proc, "meminfo", "MemTotal: 10000 kB\nMemAvailable: 6000 kB\nHugePages_Total: 0\n")
        self.put(self.proc, "diskstats", "259 0 nvme0n1 1 2 3 4 5 6 7 8 9 10 11\n")
        self.put(self.proc, "net/dev", "Inter-| Receive | Transmit\n"
                 "eth0: 10 11 12 13 14 15 16 17 20 21 22 23 24 25 26 27\n")
        self.put(self.proc, "pressure/cpu", "some avg10=0.10 avg60=0.20 avg300=0.30 total=123\n")
        self.put(self.sysfs, "devices/system/cpu/online", "0\n")
        self.put(self.sysfs, "devices/system/cpu/cpu0/cpufreq/scaling_governor", "schedutil\n")
        self.put(self.sysfs, "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "1200000\n")
        self.put(self.sysfs, "class/thermal/thermal_zone0/type", "cpu-thermal\n")
        self.put(self.sysfs, "class/thermal/thermal_zone0/temp", "45000\n")
        self.put(self.sysfs, "class/devfreq/gpu/name", "gpu\n")
        self.put(self.sysfs, "class/devfreq/gpu/cur_freq", "600000000\n")
        self.put(self.sysfs, "class/hwmon/hwmon0/name", "ina3221\n")
        self.put(self.sysfs, "class/hwmon/hwmon0/power1_input", "12500000\n")

    def test_cpu_guest_is_not_double_counted_and_iowait_is_separate(self):
        previous = ss.parse_cpu("cpu 100 10 20 1000 30 5 5 0 40 4\ncpu0 100 0 0 100 0 0 0 0 0 0")
        current = ss.parse_cpu("cpu 140 10 30 1040 50 5 5 0 80 4\ncpu0 110 0 0 110 0 0 0 0 0 0")
        util = ss.cpu_utilization(current, previous)
        self.assertAlmostEqual(util["cpu"]["busy_percent"], 100 * 50 / 110)
        self.assertAlmostEqual(util["cpu"]["iowait_percent"], 100 * 20 / 110)
        self.assertEqual(util["cpu0"]["busy_percent"], 50)
        self.assertEqual(ss.cpu_utilization(current, None)["cpu"]["status"], "warming_up")
        self.assertEqual(ss.cpu_utilization(current, current)["cpu"]["status"], "unavailable")
        self.assertEqual(ss.cpu_utilization(previous, current)["cpu"]["status"], "unavailable")

    def test_host_metrics_preserve_units_and_optional_missing(self):
        self.fixtures()
        metrics = ss.HostReader(self.proc, self.sysfs).metrics()
        self.assertEqual(metrics["scope"], "host")
        self.assertEqual(metrics["load"]["value"]["runnable_tasks"], 2)
        self.assertEqual(metrics["memory"]["value"]["MemAvailable"], {"value": 6000, "unit": "kB"})
        self.assertEqual(metrics["disks"]["value"]["nvme0n1"]["sectors_written"], 7)
        self.assertEqual(metrics["network"]["value"]["eth0"]["tx"]["collisions"], 25)
        self.assertEqual(metrics["pressure"]["cpu"]["value"]["some"]["total"], 123)
        self.assertEqual(metrics["pressure"]["io"]["status"], "missing")
        self.assertNotIn("value", metrics["pressure"]["io"])
        self.assertEqual(metrics["sysfs"]["files"]["class/thermal/thermal_zone0/temp"]["value"], "45000")
        self.assertEqual(metrics["sysfs"]["files"]["class/hwmon/hwmon0/power1_input"]["value"], "12500000")
        self.assertEqual(metrics["sysfs"]["files"]["class/devfreq/gpu/cur_freq"]["value"], "600000000")

    def test_missing_and_malformed_sources_do_not_report_zero_success(self):
        reader = ss.HostReader(self.proc, self.sysfs)
        self.assertEqual(reader.metrics()["cpu"]["status"], "missing")
        self.put(self.proc, "stat", "cpu bad data")
        self.put(self.proc, "loadavg", "broken")
        self.put(self.proc, "net/dev", "eth0: 1 2")
        result = reader.metrics()
        for key in ("cpu", "load", "network"):
            self.assertEqual(result[key]["status"], "error")
            self.assertNotIn("value", result[key])
        self.assertEqual(result["sysfs"]["status"], "unavailable")
        self.put(self.proc, "pressure/cpu", "some avg10=broken total=123")
        self.assertEqual(reader.metrics()["pressure"]["cpu"]["status"], "error")
        self.put(self.proc, "pressure/cpu", "some avg10=nan total=123")
        self.put(self.proc, "loadavg", "nan 0 0 1/2 3")
        result = reader.metrics()
        self.assertEqual(result["pressure"]["cpu"]["status"], "error")
        self.assertEqual(result["load"]["status"], "error")

    def test_normal_sysfs_relative_symlinks_are_readable(self):
        self.put(self.sysfs, "devices/gpu/cur_freq", "123000000")
        (self.sysfs / "class/devfreq").mkdir(parents=True)
        (self.sysfs / "class/devfreq/gpu").symlink_to("../../devices/gpu")
        result = ss.HostReader(self.proc, self.sysfs).metrics()
        self.assertEqual(result["sysfs"]["files"]["class/devfreq/gpu/cur_freq"]["value"], "123000000")

    def test_jetson_gpu_load_alias_preserves_raw_zero_and_driver_scale(self):
        target = "devices/platform/bus@0/17000000.gpu/load"
        self.put(self.sysfs, target, "0\n")
        (self.sysfs / "devices/platform/gpu.0").symlink_to("bus@0/17000000.gpu")
        reader = ss.HostReader(self.proc, self.sysfs)
        key = "devices/platform/gpu.0/load"
        result = reader.metrics()["sysfs"]["files"][key]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], "0")
        self.put(self.sysfs, target, "457\n")
        self.assertEqual(reader.metrics()["sysfs"]["files"][key]["value"], "457")

    def test_reads_reject_symlinks_outside_declared_host_root(self):
        outside = self.root / "outside"
        outside.write_text("not a host observation")
        (self.proc / "stat").symlink_to(outside)
        self.assertEqual(ss.read_file(self.proc, "stat")["status"], "error")

    def test_run_resolution_pinning_and_restart_uniqueness(self):
        run = ss.resolve_run({"RIG_BAG_ROOT": str(self.data)})
        self.assertEqual(run, self.run)
        self.assertEqual(ss.resolve_run({"RIG_BAG_ROOT": str(self.root)}), self.root)
        other = self.data / "runs" / "other"
        other.mkdir()
        (self.data / "current").unlink()
        (self.data / "current").symlink_to("runs/other")
        self.assertEqual(ss.resolve_run({"RIG_BAG_ROOT": str(self.data),
                                       "RIG_OBSERVER_RUN_DIR": str(run)}), self.run)
        first = ss.create_capture(run, "logger")
        second = ss.create_capture(run, "logger")
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())
        with self.assertRaises(ValueError):
            ss.create_capture(run, "../escape")

    def test_native_probe_provenance_missing_stale_and_boot_mismatch(self):
        boot = {"status": "ok", "value": "boot-1"}
        self.assertEqual(ss.host_snapshot(self.run, "logger", boot)["native_probe"]["status"], "missing")
        directory = self.run / "system" / "logger" / "host-probes"
        directory.mkdir(parents=True)
        probe = {"scope": "host", "collected_at": ss.timestamp()["at"], "boot_id": "boot-1",
                 "probes": {"nvpmodel": {"status": "permission_denied"}}}
        path = directory / "20261113T100000.json"
        path.write_text(json.dumps(probe))
        result = ss.host_snapshot(self.run, "logger", boot)
        self.assertEqual(result["native_probe"]["status"], "ok")
        self.assertEqual(result["native_probe"]["association"], "verified")
        self.assertEqual(result["native_probe"]["scope"], "launcher_host")
        self.assertEqual(result["native_probe"]["snapshot"], probe)
        self.assertEqual(result["observer_runtime"]["scope"], "observer_container")
        probe["boot_id"] = "boot-2"
        path.write_text(json.dumps(probe))
        result = ss.host_snapshot(self.run, "logger", boot)["native_probe"]
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["association"], "mismatched")
        probe["boot_id"] = "boot-1"
        probe["collected_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(probe))
        self.assertEqual(ss.host_snapshot(self.run, "logger", boot)["native_probe"]["status"], "stale")

    def test_fresh_native_probe_without_both_boot_ids_is_unverified(self):
        directory = self.run / "system/logger/host-probes"
        directory.mkdir(parents=True)
        path = directory / "new.json"
        for current_boot, native_boot in ((None, None), (None, "boot-1"), ("boot-1", None), ("boot-1", "")):
            probe = {"scope": "host", "collected_at": ss.timestamp()["at"], "boot_id": native_boot}
            path.write_text(json.dumps(probe))
            boot = {"status": "ok", "value": current_boot} if current_boot else {"status": "missing"}
            result = ss.host_snapshot(self.run, "logger", boot)["native_probe"]
            self.assertEqual(result["status"], "unverified")
            self.assertEqual(result["association"], "unverified")
            self.assertEqual(result["snapshot"], probe)
            self.assertIn("boot IDs", result["reason"])

    def test_newest_invalid_probe_is_not_silently_replaced_with_old_probe(self):
        directory = self.run / "system" / "logger" / "host-probes"
        directory.mkdir(parents=True)
        (directory / "a.json").write_text(json.dumps({"scope": "host", "collected_at": ss.timestamp()["at"]}))
        (directory / "z.json").write_text("not-json")
        self.assertEqual(ss.host_snapshot(self.run, "logger", {})["native_probe"]["status"], "error")

    def test_writer_recovers_from_failures_and_atomic_json_is_complete(self):
        writer = ss.Writer(self.root)
        with mock.patch.object(ss, "append_json", side_effect=OSError("disk full")):
            self.assertFalse(writer.write("metrics.jsonl", {"sample": 1}, append=True))
        self.assertEqual(writer.errors, 1)
        self.assertTrue(writer.write("metrics.jsonl", {"sample": 2}, append=True))
        self.assertEqual(json.loads((self.root / "metrics.jsonl").read_text()), {"sample": 2})
        self.assertTrue(writer.write("status.json", {"state": "running"}))
        self.assertEqual(json.loads((self.root / "status.json").read_text()), {"state": "running"})
        self.assertFalse(list(self.root.glob("*.tmp.*")))

    def test_partial_append_is_rolled_back_before_retry(self):
        path = self.root / "metrics.jsonl"
        ss.append_json(path, {"sample": 1})
        original = path.read_bytes()
        actual_open = Path.open

        class PartialWrite:
            def __init__(self, stream, rollback_fails=False):
                self.stream, self.rollback_fails = stream, rollback_fails

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def tell(self):
                return self.stream.tell()

            def write(self, data):
                self.stream.write(data[:5])
                raise OSError("disk full after partial write")

            def truncate(self, offset):
                if self.rollback_fails:
                    raise OSError("truncate unavailable")
                return self.stream.truncate(offset)

        writer = ss.Writer(self.root)
        with mock.patch.object(Path, "open", lambda target, *a, **kw: PartialWrite(actual_open(target, *a, **kw))):
            self.assertFalse(writer.write("metrics.jsonl", {"sample": 2}, append=True))
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(writer.write("metrics.jsonl", {"sample": 3}, append=True))
        self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], [{"sample": 1}, {"sample": 3}])
        with mock.patch.object(Path, "open", lambda target, *a, **kw: PartialWrite(actual_open(target, *a, **kw), True)):
            # Must escape Writer's ordinary retry handling: main marks failed and
            # supervisor opens a fresh capture instead of extending this fragment.
            with self.assertRaises(ss.StreamCorruptionError):
                writer.write("metrics.jsonl", {"sample": 4}, append=True)

    def wait_for(self, predicate, child, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            if child.poll() is not None:
                self.fail(f"worker exited early: {child.communicate()[1]}")
            time.sleep(0.02)
        self.fail(message)

    def start(self, interval="0.05", **overrides):
        child = subprocess.Popen([sys.executable, str(TOOLS / "system_snapshot.py")],
                                 env={**os.environ, "RIG_BAG_ROOT": str(self.data),
                                      "SYSTEM_SNAPSHOT_NAME": "test", "SYSTEM_SNAPSHOT_INTERVAL_S": interval,
                                      "SYSTEM_SNAPSHOT_SETTINGS_INTERVAL_S": "0.05",
                                      "SYSTEM_SNAPSHOT_PROC_ROOT": str(self.proc),
                                      "SYSTEM_SNAPSHOT_SYS_ROOT": str(self.sysfs),
                                      "OBSERVER_STATE_DIR": str(self.root / "state"),
                                      "OBSERVER_RECORDING_ENABLED": "true",
                                      "OBSERVER_NATIVE_PROBE_FILE": "",
                                      "RIG_OBSERVER_RUN_DIR": "", **overrides},
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate()
        self.addCleanup(cleanup)
        return child

    def test_live_settings_change_and_symlink_flip_keep_original_run(self):
        self.fixtures()
        child = self.start()
        captures = lambda: list((self.run / "system/test").glob("*/metrics.jsonl"))
        self.wait_for(lambda: bool(captures()), child, "missing first sample")
        capture = captures()[0].parent
        events = capture / "settings-events.jsonl"
        self.wait_for(lambda: events.exists(), child, "missing initial settings")
        initial = json.loads(events.read_text().splitlines()[0])
        self.put(self.sysfs, "devices/system/cpu/cpu0/cpufreq/scaling_governor", "performance\n")
        other = self.data / "runs/second"
        other.mkdir()
        (self.data / "current").unlink()
        (self.data / "current").symlink_to("runs/second")
        self.wait_for(lambda: len(events.read_text().splitlines()) >= 2, child, "settings change not captured")
        child.send_signal(signal.SIGTERM)
        _, stderr = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0, stderr)
        docs = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual(len(docs), 2)
        self.assertNotEqual(initial["sha256"], docs[1]["sha256"])
        self.assertEqual(docs[1]["settings"]["sysfs"]["devices/system/cpu/cpu0/cpufreq/scaling_governor"]["value"], "performance")
        self.assertFalse((other / "system").exists())
        status = json.loads((capture / "status.json").read_text())
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["stop_signal"], "SIGTERM")
        self.assertEqual(status["settings_events"], 2)

    def test_real_signals_interrupt_long_wait_and_preserve_stream(self):
        self.fixtures()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                before = set((self.run / "system/test").glob("*/metrics.jsonl"))
                child = self.start(interval="60")
                new = lambda: set((self.run / "system/test").glob("*/metrics.jsonl")) - before
                self.wait_for(lambda: bool(new()), child, "missing first sample")
                metrics = new().pop()
                child.send_signal(sig)
                _, stderr = child.communicate(timeout=3)
                self.assertEqual(child.returncode, 0, stderr)
                lines = [json.loads(line) for line in metrics.read_text().splitlines()]
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["boot_id"]["value"], "fixture-boot")
                self.assertGreater(lines[0]["monotonic_s"], 0)
                status = json.loads((metrics.parent / "status.json").read_text())
                self.assertEqual(status["state"], "stopped")
                self.assertEqual(status["samples_written"], 1)
                self.assertEqual(status["stop_signal"], sig.name)

    def test_intervals_reject_invalid_numbers(self):
        for value in ("0", "-1", "inf", "nan", "bad"):
            with self.assertRaises(ValueError):
                ss.interval({"interval": value}, "interval", 1)

    def test_running_worker_records_new_native_probe_without_age_churn(self):
        self.fixtures()
        child = self.start()
        files = lambda: list((self.run / "system/test").glob("*/settings-events.jsonl"))
        self.wait_for(lambda: bool(files()), child, "missing initial settings")
        events = files()[0]
        directory = self.run / "system/test/host-probes"
        directory.mkdir()
        probe = {"scope": "host", "collected_at": ss.timestamp()["at"], "boot_id": "fixture-boot",
                 "probes": {"nvpmodel": {"status": "ok", "value": "MAXN"}}}
        (directory / "new.json").write_text(json.dumps(probe))
        self.wait_for(lambda: len(events.read_text().splitlines()) >= 2, child, "new native probe not recorded")
        self.wait_for(lambda: len((events.parent / "metrics.jsonl").read_text().splitlines()) >= 5,
                      child, "worker did not continue")
        child.send_signal(signal.SIGTERM)
        child.communicate(timeout=3)
        docs = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[1]["settings"]["native_probe"]["snapshot"], probe)

    def test_unexpected_reader_failure_marks_capture_failed(self):
        self.fixtures()
        env = {"RIG_BAG_ROOT": str(self.data), "SYSTEM_SNAPSHOT_NAME": "failure",
               "SYSTEM_SNAPSHOT_PROC_ROOT": str(self.proc), "SYSTEM_SNAPSHOT_SYS_ROOT": str(self.sysfs)}
        with mock.patch.dict(os.environ, env), mock.patch.object(ss.HostReader, "metrics", side_effect=RuntimeError("fixture failure")):
            self.assertEqual(ss.main(), 1)
        status = json.loads(next((self.run / "system/failure").glob("*/status.json")).read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIn("fixture failure", status["error"])

    def test_no_recording_writes_only_bounded_live_cache_and_uses_explicit_probe(self):
        self.fixtures()
        native = self.root / "native-host.json"
        probe = {"scope": "host", "collected_at": ss.timestamp()["at"], "boot_id": "fixture-boot"}
        native.write_text(json.dumps(probe))
        absent_data = self.root / "no-data-root"
        child = self.start(OBSERVER_RECORDING_ENABLED="false", RIG_BAG_ROOT=str(absent_data),
                           OBSERVER_NATIVE_PROBE_FILE=str(native))
        cache = self.root / "state/system.json"
        self.wait_for(lambda: cache.exists() and json.loads(cache.read_text())["sequence"] >= 3,
                      child, "live-only sampling did not continue")
        value = json.loads(cache.read_text())
        self.assertEqual(value["recording"]["state"], "disabled")
        self.assertFalse(value["recording"]["enabled"])
        self.assertIsNone(value["recording"]["path"])
        self.assertEqual(value["sample"]["metrics"]["cpu"]["status"], "ok")
        self.assertEqual(value["host"]["native_probe"]["snapshot"], probe)
        self.assertEqual(value["host"]["native_probe"]["association"], "verified")
        self.assertEqual(value["sample"]["metrics"]["recording_filesystem"]["status"], "missing")
        self.assertFalse(absent_data.exists())
        self.assertEqual([p.name for p in cache.parent.iterdir()], ["system.json"])
        child.terminate()
        child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0)

    def test_failed_run_directory_does_not_stop_live_samples(self):
        self.fixtures()
        blocked = self.root / "blocked"
        blocked.write_text("not a directory")
        child = self.start(RIG_OBSERVER_RUN_DIR=str(blocked))
        cache = self.root / "state/system.json"
        self.wait_for(lambda: cache.exists() and json.loads(cache.read_text())["sequence"] >= 3,
                      child, "storage failure stopped sampling")
        value = json.loads(cache.read_text())
        self.assertEqual(value["recording"]["state"], "error")
        self.assertGreater(value["recording"]["write_errors"], 0)
        self.assertTrue(value["recording"]["last_error"])
        self.assertEqual(value["sample"]["metrics"]["cpu"]["status"], "ok")
        self.assertEqual(value["sample"]["metrics"]["recording_filesystem"]["status"], "ok")
        self.assertFalse(list(self.run.glob("system/*")))
        child.terminate()
        child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0)

    def test_failed_stream_writes_do_not_stop_live_samples(self):
        self.fixtures()
        env = {"RIG_BAG_ROOT": str(self.data), "SYSTEM_SNAPSHOT_NAME": "failure",
               "SYSTEM_SNAPSHOT_PROC_ROOT": str(self.proc), "SYSTEM_SNAPSHOT_SYS_ROOT": str(self.sysfs),
               "SYSTEM_SNAPSHOT_INTERVAL_S": "0.05", "OBSERVER_RECORDING_ENABLED": "true",
               "OBSERVER_STATE_DIR": str(self.root / "state"), "RIG_OBSERVER_RUN_DIR": ""}
        original = ss.HostReader.metrics
        samples = []

        def metrics(reader):
            sample = original(reader)
            samples.append(sample)
            if len(samples) == 3:
                os.kill(os.getpid(), signal.SIGTERM)
            return sample

        with mock.patch.dict(os.environ, env), mock.patch.object(ss.HostReader, "metrics", metrics), \
                mock.patch.object(ss, "append_json", side_effect=OSError("disk full")):
            self.assertEqual(ss.main(), 0)
        value = json.loads((self.root / "state/system.json").read_text())
        self.assertEqual(value["sequence"], 3)
        self.assertGreaterEqual(value["recording"]["write_errors"], 3)
        self.assertIn("disk full", value["recording"]["last_error"])

    def test_recording_filesystem_reports_real_capacity_and_missing_path(self):
        result = ss.recording_filesystem(self.data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope"], "host_recording_filesystem")
        self.assertGreater(result["value"]["total_bytes"], 0)
        self.assertLessEqual(result["value"]["available_bytes"], result["value"]["total_bytes"])
        self.assertEqual(ss.recording_filesystem(self.root / "absent")["status"], "missing")


if __name__ == "__main__":
    unittest.main()
