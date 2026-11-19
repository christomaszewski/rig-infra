"""Observer health mapping and cache provenance; python3 tests/test_diagnostics.py.

No ROS is imported: assertions cover the exact rows consumed by the ROS shell.
"""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import diagnostics_publish as dp


def value(data):
    return {"status": "ok", "value": data}


def fixture(sequence=1, collector="collector-a", stamp=100, native_age=2):
    at = dt.datetime.fromtimestamp(2000 - native_age, dt.timezone.utc).isoformat()
    files = {
        "class/thermal/thermal_zone0/type": value("cpu-thermal"),
        "class/thermal/thermal_zone0/temp": value("45000"),
        "class/devfreq/17000000.gpu/name": value("gpu"),
        "class/devfreq/17000000.gpu/cur_freq": value("612000000"),
        "class/devfreq/17000000.gpu/load": value("384"),
        "devices/platform/gpu.0/load": value("96"),
        "class/hwmon/hwmon0/power1_input": value("12500000"),
        "class/hwmon/hwmon0/power1_label": value("VDD_IN"),
        "class/hwmon/hwmon0/in1_input": value("12000"),
        "class/hwmon/hwmon0/in1_label": value("VDD_IN"),
        "class/hwmon/hwmon0/curr1_input": value("1000"),
        "class/hwmon/hwmon0/curr1_label": value("VDD_IN"),
    }
    native = {"association": "verified", "status": "ok", "source": "/data/host-probes/probe.json",
              "snapshot": {"collected_at": at, "hostname": "orin", "boot_id": "boot-a",
                           "probes": {"uname": value({"release": "5.15-tegra", "machine": "aarch64"}),
                                      "os_release": value({"PRETTY_NAME": "Ubuntu 22.04", "VERSION_ID": "22.04"}),
                                      "jetpack": value({"version": "6.2.1"}),
                                      "nvpmodel": value("NV Power Mode: MAXN\n0\n"),
                                      "packages": value([{"huge": "unused"}] * 100)}}}
    return {"schema_version": 1, "collector_id": collector, "sequence": sequence,
            "sample": {"at": "2026-11-13T20:00:00Z", "monotonic_s": stamp,
                       "boot_id": value("boot-a"), "metrics": {
                           "cpu": {"status": "ok", "utilization": {
                               "cpu": {"status": "ok", "busy_percent": 0},
                               "cpu0": {"status": "ok", "busy_percent": 25}}},
                           "load": value({"one": 0, "five": 1.5, "fifteen": 2}),
                           "memory": value({"MemTotal": {"value": 1000000, "unit": "kB"},
                                            "MemAvailable": {"value": 250000, "unit": "kB"},
                                            "MemFree": {"value": 100000, "unit": "kB"}}),
                           "recording_filesystem": value({"total_bytes": 10000000000,
                                                          "available_bytes": 2000000000}),
                           "sysfs": {"status": "ok", "files": files}}},
            "host": {"native_probe": native}, "settings": {}, "settings_monotonic_s": stamp,
            "recording": {"enabled": True, "state": "recording", "path": "/data/run/system/capture",
                          "write_errors": 0, "last_error": None}}


def supervisor(stamp=100):
    return {"schema_version": 1, "supervisor_id": "supervisor-a", "monotonic_s": stamp,
            "state": "running", "expected": ["system", "graph", "diagnostics"],
            "workers": {name: {"state": "running", "restarts": 0, "last_exit_code": None,
                               "last_error": None} for name in ("system", "graph", "diagnostics")}}


def rows(reporter, state=None, workers=None, now=100, wall=2000, **kwargs):
    return {row["name"].split(": ", 1)[1]: row for row in
            reporter.publish(state, workers, now=now, wall=wall, **kwargs)}


class DiagnosticsTests(unittest.TestCase):
    def test_ros_conversion_uses_generated_byte_constants_and_wall_header(self):
        class Status:
            OK, WARN, ERROR, STALE = b"\x00", b"\x01", b"\x02", b"\x03"

        def array():
            return types.SimpleNamespace(header=types.SimpleNamespace(stamp=types.SimpleNamespace()), status=[])
        reports = dp.Reporter().publish(fixture(), supervisor(), now=100, wall=2000)
        message = dp.diagnostic_array(reports, 123000000456, array, Status, types.SimpleNamespace)
        self.assertEqual(message.header.stamp.sec, 123)
        self.assertEqual(message.header.stamp.nanosec, 456)
        self.assertTrue(all(type(row.level) is bytes and row.level == Status.OK for row in message.status))
        self.assertTrue(all(type(value.value) is str for row in message.status for value in row.values))

    def test_missing_observations_are_stale_not_zero(self):
        result = rows(dp.Reporter())
        self.assertTrue(all(row["level"] == dp.STALE for row in result.values()))
        self.assertNotIn("cpu.utilization_pct", result["cpu"]["values"])
        self.assertNotIn("disk.free_gb", result["storage"]["values"])
        self.assertEqual(result["cpu"]["values"]["health.availability"], "unavailable")
        self.assertNotIn("health.last_level", result["cpu"]["values"])

    def test_units_and_zero_are_observed_and_no_thresholds_are_invented(self):
        result = rows(dp.Reporter(), fixture(), supervisor())
        self.assertEqual(result["cpu"]["values"]["cpu.utilization_pct"], "0")
        self.assertEqual(result["cpu"]["values"]["cpu.cpu0.utilization_pct"], "25")
        self.assertEqual(result["cpu"]["values"]["system.load_1m"], "0")
        self.assertEqual(result["memory"]["values"]["system.memory_total_mb"], "1024")
        self.assertEqual(result["memory"]["values"]["system.memory_available_mb"], "256")
        self.assertEqual(result["memory"]["values"]["system.memory_used_pct"], "75")
        self.assertEqual(result["storage"]["values"]["disk.free_gb"], "2")
        self.assertEqual(result["storage"]["values"]["disk.free_pct"], "20")
        self.assertEqual(result["temperature"]["values"]["temp.cpu_thermal_c"], "45")
        self.assertEqual(result["gpu"]["values"]["gpu.gpu.clock_mhz"], "612")
        self.assertEqual(result["gpu"]["values"]["gpu.gpu.load_raw"], "384")
        self.assertEqual(result["gpu"]["values"]["gpu.jetson.load_raw"], "96")
        self.assertNotIn("gpu.utilization_pct", result["gpu"]["values"])
        self.assertEqual(result["power"]["values"]["power.vdd_in_w"], "12.5")
        self.assertEqual(result["power"]["values"]["power.vdd_in_voltage_v"], "12")
        self.assertEqual(result["power"]["values"]["power.vdd_in_current_a"], "1")
        self.assertNotIn("supply.power_w", result["power"]["values"])
        self.assertTrue(all(row["level"] == dp.OK for row in result.values()))
        self.assertNotIn("packages", json.dumps(result["host"]))

    def test_expiry_retains_values_sample_identity_and_last_level(self):
        reporter = dp.Reporter(thresholds={"temp.cpu_thermal_c": {"warn": 40, "error": 60}})
        state = fixture()
        first = rows(reporter, state, supervisor())
        stale = rows(reporter, state, supervisor(), now=107, wall=2007)
        self.assertEqual(first["temperature"]["level"], dp.WARN)
        self.assertEqual(stale["temperature"]["level"], dp.STALE)
        self.assertEqual(stale["temperature"]["values"]["health.last_level"], str(dp.WARN))
        self.assertEqual(stale["temperature"]["values"]["temp.cpu_thermal_c"], "45")
        self.assertEqual(first["cpu"]["values"]["health.sample_id"], stale["cpu"]["values"]["health.sample_id"])
        self.assertEqual(stale["cpu"]["values"]["health.sample_age_s"], "7")
        self.assertEqual(stale["cpu"]["values"]["health.sequence"], "2")

    def test_warming_cpu_retains_previous_observation_without_inventing_zero(self):
        state = fixture()
        state["sample"]["metrics"]["cpu"]["utilization"] = {"cpu": {"status": "warming_up"}}
        initial = rows(dp.Reporter(), state, supervisor())["cpu"]
        self.assertEqual(initial["level"], dp.STALE)
        self.assertEqual(initial["values"]["health.availability"], "retrying")
        self.assertNotIn("cpu.utilization_pct", initial["values"])
        reporter = dp.Reporter()
        rows(reporter, fixture(), supervisor())
        state.update(collector_id="collector-b")
        state["sample"]["monotonic_s"] = 102
        restarted = rows(reporter, state, supervisor(102), now=102)["cpu"]
        self.assertEqual(restarted["values"]["cpu.utilization_pct"], "0")
        self.assertEqual(restarted["values"]["health.sample_id"], "collector-a:1")
        self.assertEqual(restarted["values"]["health.sample_age_s"], "2")

    def test_configured_thresholds_and_invalid_numbers(self):
        reporter = dp.Reporter(thresholds={"disk.free_pct": {"warn": 30, "error": 10, "direction": "below"}})
        result = rows(reporter, fixture(), supervisor())
        self.assertEqual(result["storage"]["level"], dp.WARN)
        state = fixture(sequence=2, stamp=101)
        state["sample"]["metrics"]["recording_filesystem"]["value"]["available_bytes"] = 0
        self.assertEqual(rows(reporter, state, supervisor(101), now=101)["storage"]["level"], dp.ERROR)
        for thresholds in ({"x": {"warn": float("nan")}}, {"x": {"warn": True}},
                           {"x": {"warn": "10"}}, {"x": {"warn": 20, "error": 10}},
                           {"x": {"warn": 10, "error": 20, "direction": "below"}}, {"x": {}}):
            with self.subTest(thresholds=thresholds), self.assertRaises(ValueError):
                dp.validate_thresholds(thresholds)
        self.assertEqual(dp.threshold_level({"x": True}, {"x": {"warn": 0}})[0], dp.OK)
        self.assertIsNone(dp.scalar(float("inf")))

    def test_duplicate_source_id_never_freshens_or_replaces_values(self):
        reporter = dp.Reporter()
        rows(reporter, fixture(), supervisor())
        changed = fixture(stamp=104)
        changed["sample"]["metrics"]["cpu"]["utilization"]["cpu"]["busy_percent"] = 99
        result = rows(reporter, changed, supervisor(104), now=104)["cpu"]
        self.assertEqual(result["values"]["cpu.utilization_pct"], "0")
        self.assertEqual(result["values"]["health.sample_age_s"], "4")
        self.assertEqual(result["values"]["health.sample_id"], "collector-a:1")

    def test_collector_restart_changes_source_identity_not_publisher_identity(self):
        reporter = dp.Reporter(publisher_id="publisher-one")
        first = rows(reporter, fixture(sequence=20), supervisor())["cpu"]
        new = rows(reporter, fixture(collector="collector-b", stamp=102), supervisor(102), now=102)["cpu"]
        self.assertNotEqual(first["values"]["health.sample_id"], new["values"]["health.sample_id"])
        self.assertEqual(first["values"]["health.publisher_id"], new["values"]["health.publisher_id"])
        delayed = rows(reporter, fixture(sequence=21, stamp=103), supervisor(103), now=103)["cpu"]
        self.assertEqual(delayed["level"], dp.STALE)
        self.assertEqual(delayed["values"]["health.sample_id"], "collector-b:1")
        self.assertIn("retired", delayed["values"]["health.error"])
        other = rows(dp.Reporter(), fixture(), supervisor())["cpu"]
        self.assertNotEqual(first["values"]["health.publisher_id"], other["values"]["health.publisher_id"])

    def test_file_failure_retains_cached_fault_with_original_age(self):
        reporter = dp.Reporter(thresholds={"cpu.utilization_pct": {"error": 0}})
        rows(reporter, fixture(), supervisor())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "system.json"
            for content in (None, "broken JSON", '{"schema_version":1,"sample":{"monotonic_s":NaN}}'):
                if content is not None:
                    path.write_text(content)
                cache, error = dp.read_cache(path, "system")
                self.assertIsNone(cache)
                self.assertTrue(error)
                result = rows(reporter, cache, supervisor(102), now=102, error=error)["cpu"]
                self.assertEqual(result["level"], dp.STALE)
                self.assertEqual(result["values"]["health.last_level"], str(dp.ERROR))
                self.assertEqual(result["values"]["health.sample_id"], "collector-a:1")
                self.assertEqual(result["values"]["health.sample_age_s"], "2")

    def test_startup_inventory_age_is_independent_without_expected_stale_alarm(self):
        reporter = dp.Reporter()
        first = rows(reporter, fixture(native_age=4), supervisor())
        self.assertNotIn("power_mode", first)
        self.assertEqual(first["power"]["values"]["power.startup_mode_id"], "0")
        self.assertNotIn("power.mode_id", first["power"]["values"])
        self.assertEqual(first["host"]["values"]["host.inventory.age_s"], "4")
        self.assertEqual(first["host"]["values"]["host.inventory.jetpack"], "6.2.1")
        self.assertEqual(first["host"]["values"]["host.inventory.jetpack_status"], "installed")
        self.assertNotIn("host.inventory.jetpack_l4t_version", first["host"]["values"])
        # Startup context retains original time through new CPU samples and backwards
        # wall-clock adjustment. Live host health follows observed boot continuity.
        state = fixture(sequence=2, stamp=103, native_age=4)
        second = rows(reporter, state, supervisor(103), now=103, wall=1500)
        self.assertEqual(second["power"]["level"], dp.OK)
        self.assertEqual(second["host"]["level"], dp.OK)
        self.assertEqual(second["host"]["values"]["host.inventory.age_s"], "7")
        self.assertEqual(second["host"]["values"]["health.sample_age_s"], "0")
        self.assertEqual(second["power"]["values"]["power.startup_mode_age_s"], "7")
        self.assertEqual(second["power"]["values"]["power.mode_availability"], "not_live_sampled")
        excluded = rows(dp.Reporter(components=("host",)), state, supervisor(103), now=103)
        self.assertNotIn("startup_mode", json.dumps(excluded))

    def test_jetpack_inference_keeps_source_and_l4t_release(self):
        state = fixture()
        probes = state["host"]["native_probe"]["snapshot"]["probes"]
        probes["jetpack"] = {"status": "inferred", "source": "/etc/nv_tegra_release",
                             "value": {"version": "6.2.1", "l4t_version": "36.4.4"}}
        result = rows(dp.Reporter(), state, supervisor())["host"]["values"]
        self.assertEqual(result["host.inventory.jetpack"], "6.2.1")
        self.assertEqual(result["host.inventory.jetpack_status"], "inferred")
        self.assertEqual(result["host.inventory.jetpack_source"], "/etc/nv_tegra_release")
        self.assertEqual(result["host.inventory.jetpack_l4t_version"], "36.4.4")

        probes["jetpack"] = {"status": "unknown", "value": None}
        result = rows(dp.Reporter(), state, supervisor())["host"]["values"]
        self.assertEqual(result["host.inventory.jetpack_status"], "unknown")
        self.assertNotIn("host.inventory.jetpack", result)

    def test_unsupported_optional_metrics_and_unverified_host_are_not_fabricated(self):
        state = fixture()
        state["sample"]["metrics"]["sysfs"]["files"] = {}
        state["host"]["native_probe"].update(association="unverified", reason="missing host boot ID")
        result = rows(dp.Reporter(), state, supervisor())
        for name in ("temperature", "gpu", "power"):
            self.assertEqual(result[name]["level"], dp.OK)
            self.assertEqual(result[name]["values"]["health.availability"], "unsupported")
        self.assertEqual(result["host"]["level"], dp.OK)
        self.assertEqual(result["host"]["values"]["host.inventory.availability"], "unverified")
        self.assertNotIn("host.inventory.jetpack", result["host"]["values"])
        self.assertNotIn("host.inventory.jetpack_status", result["host"]["values"])
        self.assertNotIn("power.startup_mode_name", result["power"]["values"])

    def test_recording_failures_and_restarting_workers_remain_visible(self):
        state, workers = fixture(), supervisor()
        state["recording"].update(state="error", write_errors=1, last_error="disk full")
        workers["workers"]["graph"].update(state="restarting", restarts=2, last_exit_code=1)
        result = rows(dp.Reporter(), state, workers)
        self.assertEqual(result["cpu"]["level"], dp.OK)
        self.assertEqual(result["recording"]["level"], dp.ERROR)
        self.assertIn("disk full", result["recording"]["message"])
        self.assertEqual(result["collectors"]["level"], dp.WARN)
        self.assertEqual(result["collectors"]["values"]["collector.graph.restarts"], "2")
        state["recording"].update(enabled=False, state="disabled", write_errors=0, last_error=None)
        self.assertEqual(rows(dp.Reporter(), state, workers)["recording"]["level"], dp.OK)

    def test_identity_and_component_ownership(self):
        result = rows(dp.Reporter(instance="orin", vehicle_id="robot_1", components=("cpu", "host", "recording")), fixture(), supervisor())
        self.assertEqual(set(result), {"cpu", "host", "recording"})
        self.assertEqual(result["cpu"]["name"], "orin: cpu")
        self.assertEqual(result["cpu"]["values"]["health.vehicle_id"], "robot_1")
        self.assertEqual(result["cpu"]["values"]["health.service"], "observer")
        generic = rows(dp.Reporter(), fixture(), supervisor())["cpu"]
        self.assertNotIn("health.vehicle_id", generic["values"])
        config = dp.configuration({"OBSERVER_NAME": "orin", "OBSERVER_DIAGNOSTICS_INSTANCE": "orin_replay_host",
                                   "OBSERVER_DIAGNOSTICS_SCOPE": "replay_host"})
        self.assertEqual(config["instance"], "orin_replay_host")
        self.assertEqual(config["scope"], "replay_host")
        for row in result.values():
            self.assertTrue(all(isinstance(v, str) for v in row["values"].values()))


if __name__ == "__main__":
    unittest.main()
