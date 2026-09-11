"""Execute rendered play.sh against a fake ros2 to check the publisher's transport environment.

No ROS or Docker needed. Run: python3 ros2-bag-player/tests/test_play_cmd_zenoh.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import play_cmd

ENABLED = "transport/shared_memory/enabled"
POOL = "transport/shared_memory/transport_optimization/pool_size"


def _render(tmp, config=None, env=None):
    session = tmp / "source" / "bags" / "bag_logger" / "bag_logger_20260911T100000Z"
    session.mkdir(parents=True, exist_ok=True)
    (session / "bag_0.mcap").touch()
    (session / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n"
        "  duration: {nanoseconds: 120000000000}\n"
        "  starting_time: {nanoseconds_since_epoch: 1789120800000000000}\n"
    )
    cfg = {"source": {"run": str(tmp / "source")}, **(config or {})}
    result = play_cmd.render(cfg, env or {}, tmp)
    return pathlib.Path(result[1])


def _run(tmp, config=None, *, inherited="", rmw="rmw_zenoh_cpp"):
    script = _render(tmp, config)
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    ros2 = bin_dir / "ros2"
    ros2.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], "
        "'override': os.environ.get('ZENOH_CONFIG_OVERRIDE', '')}))\n"
    )
    ros2.chmod(0o755)
    # Do not source any installed ROS or inherit transport settings from the test machine.
    env = {"PATH": f"{bin_dir}:{os.defpath}", "RMW_IMPLEMENTATION": rmw,
           "ZENOH_CONFIG_OVERRIDE": inherited}
    proc = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout), proc.stderr


def _effective(raw):
    return {key: json.loads(value) for key, value in
            (pair.split("=", 1) for pair in raw.split(";"))}


def test_existing_configs_enable_shm_and_256_mib_at_player_start():
    with tempfile.TemporaryDirectory() as tmp:
        inherited = 'connect/endpoints=["tcp/localhost:7447"];' + ENABLED + '=false'
        result, log = _run(pathlib.Path(tmp), inherited=inherited)
        assert result["argv"] == ["bag", "play",
                                  "/replay/bags/bag_logger/bag_logger_20260911T100000Z"]
        assert result["override"].startswith(inherited + ";")
        assert _effective(result["override"]) == {
            "connect/endpoints": ["tcp/localhost:7447"], ENABLED: True, POOL: 268435456}
        assert "ZENOH_CONFIG_OVERRIDE=" + result["override"] in log


def test_custom_pool_and_explicit_disable_override_inherited_settings():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        result, _ = _run(tmp, {"zenoh": {"shared_memory": True, "shm_pool_mb": 512}})
        assert _effective(result["override"])[POOL] == 536870912
        result, _ = _run(tmp, {"zenoh": {"shared_memory": False}},
                         inherited=ENABLED + "=true")
        assert _effective(result["override"]) == {ENABLED: False}
        warns = []
        play_cmd.zenoh_env_lines({"zenoh": {"shared_memory": False, "shm_pool_mb": 256}}, warns)
        assert len(warns) == 1 and "ignored" in warns[0]


def test_nested_overrides_win_and_are_shell_quoted():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        marker = tmp / "must-not-exist"
        literal = f"literal ' $(touch {marker})"
        overrides = {"transport": {"shared_memory": {
            "enabled": False, "transport_optimization": {"pool_size": 16777216}}},
            "connect": {"endpoints": [literal]}}
        result, _ = _run(tmp, {"zenoh": {"overrides": overrides}})
        assert _effective(result["override"]) == {
            ENABLED: False, POOL: 16777216, "connect/endpoints": [literal]}
        assert not marker.exists()


def test_other_rmws_leave_the_environment_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        for inherited in ("", ENABLED + "=false"):
            result, log = _run(pathlib.Path(tmp), inherited=inherited, rmw="rmw_fastrtps_cpp")
            assert result["override"] == inherited
            assert "ZENOH_CONFIG_OVERRIDE=" not in log


def test_invalid_zenoh_settings_fail_before_writing_a_script():
    cases = [
        ([], "`zenoh` must be a mapping"),
        ({"shared_memroy": True}, "unknown zenoh key"),
        ({"shared_memory": "false"}, "zenoh.shared_memory"),
        *[({"shm_pool_mb": value}, "zenoh.shm_pool_mb")
          for value in (0, -1, True, 1.5, "256", None)],
        ({"overrides": []}, "zenoh.overrides"),
        ({"overrides": {"connect": {"endpoints": ["tcp/a;b"]}}}, "contains ';'"),
        ({"overrides": {"bad;path": True}}, "contains ';'"),
    ]
    for zenoh, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            try:
                _render(tmp, {"zenoh": zenoh})
            except SystemExit as exc:
                assert expected in str(exc), str(exc)
            else:
                raise AssertionError(f"accepted invalid zenoh config: {zenoh!r}")
            assert not (tmp / "var").exists()


def test_transport_settings_precede_both_replay_helpers():
    with tempfile.TemporaryDirectory() as tmp:
        script = _render(pathlib.Path(tmp), {"play": {"start_offset_s": 5}},
                         {"RIG_REPLAY_START_AT_UNIX_S": "1789120820.000"})
        body = script.read_text()
        assert body.index("export ZENOH_CONFIG_OVERRIDE=") < body.index("python3 /latch_restore.py")
        assert body.index("export ZENOH_CONFIG_OVERRIDE=") < body.index("python3 /release_gate.py")
        proc = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
