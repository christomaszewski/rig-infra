"""The v1.13.0 release-gate half of the player's pure core (tools/play_cmd.py) against
rig-sensor-replay-plan §5 / rig-replay-player-handoff §1.1 (amended 2026-09-07; rig >= v0.2.52):
`RIG_REPLAY_START_AT_UNIX_S` arms the gate — `--start-paused` is rendered WHATEVER the config's
`play.start_paused` says and play.sh backgrounds `tools/release_gate.py --at <instant>` (the
instant spelled as rig spelled it) before `exec ros2 bag play`; absent (or empty) env keeps
today's behaviour byte for byte (the config knob alone decides the flag, no companion line); a
garbled instant refuses by name; the seek (`--start-offset`) and the hold compose so a windowed
gate releases AT `from`, with the latch pre-pass still ahead of both; and the match-nothing
exclude rig's reproduce sends (`^$`) plays EVERYTHING — emitted verbatim, fully matching no topic,
and the latch pre-pass keeps every latched topic under it. rig v0.2.52+'s `rig replay` is built
against exactly this behavior — a change that breaks these tests is a contract renegotiation,
not a refactor.
Run: `python3 tests/test_play_cmd_gate.py` (no ROS, no filesystem — lister + reader injected)."""
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import latch_restore
import play_cmd

RUN = "/data/runs/run-042"
TREE = os.path.join(RUN, "bags", "bag_logger")
S1 = "bag_logger_20260827T100000Z"
PATH = "/replay/bags/bag_logger/" + S1
META_PATH = os.path.join(TREE, S1, "metadata.yaml")
START_NS = 1788361659604888347
META = f"""rosbag2_bagfile_information:
  version: 9
  storage_identifier: mcap
  duration: {{nanoseconds: 49908202370}}
  starting_time: {{nanoseconds_since_epoch: {START_NS}}}
  topics_with_message_count: []
"""
AT = "1788788622.342"             # the handoff's example instant, rig's f"{t:.3f}"
GATE_LINE = f"python3 /release_gate.py --at {AT} &"


def _build(cfg=None, env=None):
    cfg = {"service": "ros2-bag-player", "name": "bag_player",
           "source": {"run": RUN}, **(cfg or {})}
    fs = {TREE: [S1], os.path.join(TREE, S1): ["metadata.yaml", "x_0.mcap"]}
    return play_cmd.build_args(cfg, env or {}, lambda p: fs.get(p),
                               lambda p: META if p == META_PATH else None)


def _script(cfg=None, env=None):
    _, _, _, args, _, extras = _build(cfg, env)
    return play_cmd.play_script(PATH, args, extras["latch"], extras["start_at"]).splitlines()


def _expect_exit(needle, **kw):
    try:
        _build(**kw)
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


# --- the gate arms from env ----------------------------------------------------------------------

def test_gate_arms_from_env_and_forces_start_paused_over_a_false_config():
    cfg = {"play": {"start_paused": False}}
    _, _, _, args, warns, extras = _build(cfg=cfg, env={"RIG_REPLAY_START_AT_UNIX_S": AT})
    assert args == ["--start-paused"] and not warns          # the env wins silently: no conflict
    assert extras["start_at"] == 1788788622.342
    _, _, _, args, _, _ = _build(cfg=cfg, env={"RIG_REPLAY_START_AT_UNIX_S": AT,
                                               "RIG_SIM_TIME": "1"})
    assert args == ["--start-paused", "--clock"]              # the gate does not care about /clock


def test_config_true_and_the_env_render_one_flag():
    _, _, _, args, _, extras = _build(cfg={"play": {"start_paused": True}},
                                      env={"RIG_REPLAY_START_AT_UNIX_S": AT})
    assert args == ["--start-paused"] and extras["start_at"] == 1788788622.342


def test_absent_env_keeps_todays_behaviour():
    _, _, _, args, _, extras = _build()
    assert args == [] and extras["start_at"] is None
    _, _, _, args, _, extras = _build(env={"RIG_REPLAY_START_AT_UNIX_S": ""})   # compose default
    assert args == [] and extras["start_at"] is None
    _, _, _, args, _, extras = _build(cfg={"play": {"start_paused": True}})    # standalone hold
    assert args == ["--start-paused"] and extras["start_at"] is None           # ... no companion
    lines = _script(cfg={"play": {"start_paused": True}})
    assert not any("release_gate" in ln for ln in lines)
    assert lines[-1] == f"exec ros2 bag play {PATH} --start-paused"


def test_garbled_instant_refuses_by_name():
    for raw in ("soon", "-1788788622.342", "0", "nan", "20s"):
        _expect_exit("RIG_REPLAY_START_AT_UNIX_S", env={"RIG_REPLAY_START_AT_UNIX_S": raw})


# --- play.sh: the companion, its spelling, its place ----------------------------------------------

def test_play_script_backgrounds_the_companion_before_exec():
    lines = _script(env={"RIG_REPLAY_START_AT_UNIX_S": AT})
    assert GATE_LINE in lines
    assert lines[-1] == f"exec ros2 bag play {PATH} --start-paused"    # exec is always last
    assert lines.index(GATE_LINE) < len(lines) - 1


def test_the_instant_is_spelled_as_rig_spells_it():
    lines = _script(env={"RIG_REPLAY_START_AT_UNIX_S": "1788788622.3"})
    assert "python3 /release_gate.py --at 1788788622.300 &" in lines
    lines = _script(env={"RIG_REPLAY_START_AT_UNIX_S": "1788788622"})
    assert "python3 /release_gate.py --at 1788788622.000 &" in lines


def test_windowed_gate_seeks_then_holds_with_the_pre_pass_ahead_of_both():
    env = {"RIG_REPLAY_FROM_S": "30", "RIG_REPLAY_TOPICS": "/a", "RIG_REPLAY_START_AT_UNIX_S": AT}
    _, _, _, args, _, extras = _build(env=env)
    assert args == ["--start-offset", "30", "--start-paused", "--topics", "/a"]
    assert extras["latch"] == ["--from", "30", "--topics", "/a"]
    lines = _script(env=env)
    latch = next(i for i, ln in enumerate(lines) if ln.startswith("python3 /latch_restore.py"))
    assert latch < lines.index("LATCH_PID=$!") < lines.index(GATE_LINE) < len(lines) - 1
    assert lines[-1] == f"exec ros2 bag play {PATH} --start-offset 30 --start-paused --topics /a"


def test_flag_order_with_every_knob_and_the_gate():
    cfg = {"play": {"rate": 2.0, "loop": True, "start_paused": False, "topics": ["/a"]}}
    env = {"RIG_REPLAY_FROM_S": "5", "RIG_REPLAY_TO_S": "15", "RIG_SIM_TIME": "1",
           "RIG_REPLAY_START_AT_UNIX_S": AT}
    _, _, _, args, _, _ = _build(cfg=cfg, env=env)
    assert args == ["-r", "2", "--loop", "--start-offset", "5", "--playback-duration", "10",
                    "--start-paused", "--clock", "--topics", "/a"]   # selector still LAST


# --- a match-nothing exclude plays everything -----------------------------------------------------

def test_match_nothing_exclude_plays_everything():
    _, _, _, args, warns, _ = _build(env={"RIG_REPLAY_EXCLUDE": "^$"})
    assert args == ["--exclude-regex", "(?:^$)"] and not warns
    for topic in ("/toy/tick", "/tf_static", "/rosout", "/"):
        assert re.fullmatch("(?:^$)", topic) is None        # a full match, as rosbag2's -x is
    latched = [{"name": "/tf_static", "type": "tf2_msgs/msg/TFMessage", "reliability": "reliable"}]
    assert latch_restore.select(latched, None, "(?:^$)") == latched   # the pre-pass keeps them too
    # config excludes still merge into the alternation, and bite on their own
    _, _, _, args, _, _ = _build(cfg={"play": {"exclude": [r"^/cam/.*"]}},
                                 env={"RIG_REPLAY_EXCLUDE": "^$"})
    assert args == ["--exclude-regex", "(?:^$)|(?:^/cam/.*)"]
    assert re.fullmatch("(?:^$)|(?:^/cam/.*)", "/cam/image_raw") is not None
    assert re.fullmatch("(?:^$)|(?:^/cam/.*)", "/toy/tick") is None


# --- render: the launcher's fields are unchanged; play.sh on disk carries the gate ---------------

def test_render_fields_unchanged_and_the_script_carries_the_gate():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / "run"
        sess = src / "bags" / "bag_logger" / S1
        sess.mkdir(parents=True)
        (sess / "x_0.mcap").write_bytes(b"")
        (sess / "metadata.yaml").write_text(META)
        cfg = {"name": "gated", "source": {"run": str(src)}}
        out = play_cmd.render(cfg, {"RIG_REPLAY_START_AT_UNIX_S": AT}, pathlib.Path(d))
        assert len(out) == 9 and out[0] == "gated" and out[6:] == ("", "", "")
        text = pathlib.Path(out[1]).read_text()
        assert GATE_LINE in text.splitlines()
        assert text.endswith(f"exec ros2 bag play /replay/bags/bag_logger/{S1} --start-paused\n")
        out = play_cmd.render(cfg, {}, pathlib.Path(d))
        assert "release_gate" not in pathlib.Path(out[1]).read_text()


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
