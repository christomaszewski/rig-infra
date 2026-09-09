"""The launcher (ros2-bag-player-up) against the verbs that must work WITHOUT a replay to render.

rig strips RIG_REPLAY_* on every verb but `replay`, so the `rig down` that follows a finished or
half-failed replay reaches the launcher with no source run -- and used to die in play_cmd before
`docker compose down` ever ran, leaving the player up with no good way to bring the stack down.
down / ps / logs / config now continue on the compose MODEL alone (placeholders for the required
interpolations, the name from the config); up / export-calls still refuse, naming the reason.
`docker` is a stub on PATH that records its argv + the env it saw -- no daemon, no ROS.
Run: `python3 tests/test_launcher_verbs.py`."""
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "ros2-bag-player-up"

CONFIG = """service: ros2-bag-player
name: bag_player
source: {run: "", logger: bag_logger, session: latest}
play: {topics: [], exclude: [], rate: 1.0, loop: false}
"""

STUB = """#!/usr/bin/env bash
# a docker stub: record argv and the env the launcher exported, exit 0
{
  printf 'ARGV:%s\\n' "$*"
  printf 'ENV:COMPOSE_PROJECT_NAME=%s\\n' "${COMPOSE_PROJECT_NAME-}"
  printf 'ENV:RIG_REPLAY_SOURCE=%s\\n' "${RIG_REPLAY_SOURCE-}"
  printf 'ENV:BAG_PLAY_SCRIPT=%s\\n' "${BAG_PLAY_SCRIPT-}"
  printf 'ENV:BAG_PLAYER_NAME=%s\\n' "${BAG_PLAYER_NAME-}"
} >> "$DOCKER_STUB_LOG"
"""


def _run(verb, *, env_extra=None, config=CONFIG, source_tree=False):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "bin").mkdir()
        stub = tmp / "bin" / "docker"
        stub.write_text(STUB)
        stub.chmod(0o755)
        cfg = tmp / "bag_player.yaml"
        cfg.write_text(config)
        log = tmp / "docker.log"
        env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_REPLAY") and k != "COMPOSE_PROJECT_NAME"}
        env["PATH"] = f"{tmp / 'bin'}:{env.get('PATH', '')}"
        env["DOCKER_STUB_LOG"] = str(log)
        if source_tree:
            run = tmp / "run"
            session = run / "bags" / "bag_logger" / "bag_logger_20260827T100000Z"
            session.mkdir(parents=True)
            (session / "metadata.yaml").write_text("rosbag2_bagfile_information:\n  duration: {nanoseconds: 120000000000}\n"
                                                   "  starting_time: {nanoseconds_since_epoch: 1788361659604888347}\n")
            (session / "bag_logger_0.mcap").write_bytes(b"")   # a session is its bag files
            env["RIG_REPLAY_SOURCE"] = str(run)
            env["RIG_REPLAY_EXCLUDE"] = "^/never$"
        env.update(env_extra or {})
        proc = subprocess.run([str(LAUNCHER), str(cfg), verb], env=env, capture_output=True, text=True)
        calls = log.read_text() if log.exists() else ""
        return proc, calls


def test_down_without_a_replay_source_tears_the_project_down_on_placeholders():
    proc, calls = _run("down", env_extra={"COMPOSE_PROJECT_NAME": "bag_player-vehicle-7"})
    assert proc.returncode == 0, proc.stderr
    assert "no replay to render" in proc.stderr and "no source run" in proc.stderr, proc.stderr
    assert "ARGV:compose -f " in calls and calls.strip().endswith("ENV:BAG_PLAYER_NAME=bag_player"), calls
    assert "--profile calls down" in calls, calls           # the injector's profile rides down unconditionally
    assert "ENV:COMPOSE_PROJECT_NAME=bag_player-vehicle-7" in calls, calls   # rig's project honored
    assert "ENV:RIG_REPLAY_SOURCE=/replay-source-unset" in calls, calls      # the compose's :? interpolations satisfied
    assert "ENV:BAG_PLAY_SCRIPT=/play.sh-unset" in calls, calls


def test_ps_logs_and_config_continue_the_same_way_and_standalone_keeps_its_own_project():
    for verb, expect in (("status", "--profile calls ps"), ("logs", "--profile calls logs"), ("config", " config")):
        proc, calls = _run(verb)
        assert proc.returncode == 0, (verb, proc.stderr)
        assert expect in calls, (verb, calls)
        assert "ENV:COMPOSE_PROJECT_NAME=ros2-bag-player_bag_player" in calls, calls


def test_up_and_export_calls_still_refuse_without_a_source_and_never_reach_docker():
    for verb in ("up", "export-calls"):
        proc, calls = _run(verb)
        assert proc.returncode == 1, (verb, proc.returncode, proc.stderr)
        assert "play_cmd: no source run" in proc.stderr, proc.stderr
        assert "no replay to render" not in proc.stderr
        assert calls == "", calls


def test_with_a_replay_to_render_down_takes_the_real_path():
    proc, calls = _run("down", source_tree=True)
    assert proc.returncode == 0, proc.stderr
    assert "no replay to render" not in proc.stderr, proc.stderr
    assert "/replay-source-unset" not in calls and "ENV:RIG_REPLAY_SOURCE=" in calls, calls
    assert "/run" in calls.split("ENV:RIG_REPLAY_SOURCE=")[1].splitlines()[0], calls


def test_play_cmd_name_mode_is_a_pure_config_read():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = pathlib.Path(tmp) / "c.yaml"
        cfg.write_text("name: replay_a\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_REPLAY")}
        out = subprocess.run([sys.executable, str(REPO / "tools" / "play_cmd.py"), str(cfg), "-", "--name"],
                             env=env, capture_output=True, text=True)
        assert out.returncode == 0 and out.stdout.strip() == "replay_a", (out.stdout, out.stderr)
        cfg.write_text("service: ros2-bag-player\n")
        out = subprocess.run([sys.executable, str(REPO / "tools" / "play_cmd.py"), str(cfg), "-", "--name"],
                             capture_output=True, text=True)
        assert out.stdout.strip() == "bag_player"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
