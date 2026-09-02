"""The v1.12.0 window half of the player's pure core (tools/play_cmd.py) against the frozen
window contract (rig-replay-window-handoff §1.1 + §1.4): RIG_REPLAY_FROM_S / RIG_REPLAY_TO_S ->
`--start-offset <from>` + `--playback-duration <to − from>` (RECORDED seconds — pinned live on
lyrical), the standalone `play.start_offset_s` / `play.end_offset_s` siblings, env shadowing a
non-zero config value with a WARN (the topics pattern), host-side validation against the
session's metadata.yaml (from >= duration REFUSES; from >= to REFUSES; to > duration WARNs and
CLAMPS — and a clamped end emits no flag, the bag ends by itself), the bag start handed to the
launcher for the injector's one zero, the latch pre-pass argv (the SAME selector the main play
uses, only at from > 0), and the unwindowed regression: no keys, offset 0 -> argv byte-identical
to v1.10.x. rig v0.2.46's `rig replay --from/--to` is built against exactly this behavior — a
change that breaks these tests is a contract renegotiation, not a refactor.
Run: `python3 tests/test_play_cmd_window.py` (no ROS, no filesystem — lister + reader injected)."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import play_cmd

RUN = "/data/runs/run-042"
TREE = os.path.join(RUN, "bags", "bag_logger")
S1 = "bag_logger_20260827T100000Z"
META_PATH = os.path.join(TREE, S1, "metadata.yaml")
START_NS = 1788361659604888347
DURATION_NS = 49908202370          # the live toy bag: 49.908 s
META = f"""rosbag2_bagfile_information:
  version: 9
  storage_identifier: mcap
  duration:
    nanoseconds: {DURATION_NS}
  starting_time:
    nanoseconds_since_epoch: {START_NS}
  message_count: 472
  topics_with_message_count: []
"""


def _build(cfg=None, env=None, meta=META):
    cfg = {"service": "ros2-bag-player", "name": "bag_player",
           "source": {"run": RUN}, **(cfg or {})}
    fs = {TREE: [S1], os.path.join(TREE, S1): ["metadata.yaml", "x_0.mcap"]}
    return play_cmd.build_args(cfg, env or {}, lambda p: fs.get(p),
                               lambda p: meta if p == META_PATH else None)


def _expect_exit(needle, **kw):
    try:
        _build(**kw)
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


# --- env -> flags (§1.1 / §1.4) ------------------------------------------------------------------

def test_rig_window_maps_to_start_offset_plus_playback_duration():
    _, _, _, args, warns, extras = _build(env={"RIG_REPLAY_FROM_S": "30",
                                               "RIG_REPLAY_TO_S": "40", "RIG_SIM_TIME": "1"})
    assert args == ["--start-offset", "30", "--playback-duration", "10", "--clock"]
    assert not warns
    assert (extras["from_s"], extras["to_s"], extras["bag_start_ns"]) == (30.0, 40.0, START_NS)
    assert extras["latch"] == ["--from", "30"]                  # bare play: pre-pass selector empty


def test_from_alone_and_to_alone():
    _, _, _, args, _, extras = _build(env={"RIG_REPLAY_FROM_S": "12.5"})
    assert args == ["--start-offset", "12.5"] and extras["to_s"] is None
    _, _, _, args, _, extras = _build(env={"RIG_REPLAY_TO_S": "20"})
    assert args == ["--playback-duration", "20"]               # from 0: no offset flag
    assert (extras["from_s"], extras["to_s"]) == (0.0, 20.0)
    assert extras["latch"] == []                                # no pre-pass at from == 0


def test_flag_order_with_every_knob():
    cfg = {"play": {"rate": 2.0, "start_paused": True, "topics": ["/a"]}}
    env = {"RIG_REPLAY_FROM_S": "5", "RIG_REPLAY_TO_S": "15", "RIG_SIM_TIME": "1"}
    _, _, _, args, _, extras = _build(cfg=cfg, env=env)
    assert args == ["-r", "2", "--start-offset", "5", "--playback-duration", "10",
                    "--start-paused", "--clock", "--topics", "/a"]  # selector still LAST
    assert extras["latch"] == ["--from", "5", "--topics", "/a"]      # the SAME selector


def test_precision_survives_long_offsets():
    _, _, _, args, _, _ = _build(env={"RIG_REPLAY_FROM_S": "1234.567"},
                                 meta=META.replace(str(DURATION_NS), str(3600 * 10**9)))
    assert args == ["--start-offset", "1234.567"]              # `:g` alone would clip to 1234.57


# --- standalone config + shadowing --------------------------------------------------------------

def test_config_offsets_govern_standalone():
    cfg = {"play": {"start_offset_s": 3.5, "end_offset_s": 8}}
    _, _, _, args, warns, extras = _build(cfg=cfg)
    assert args == ["--start-offset", "3.5", "--playback-duration", "4.5"] and not warns
    assert (extras["from_s"], extras["to_s"]) == (3.5, 8.0)
    _, _, _, args, _, extras = _build(cfg={"play": {"end_offset_s": 0}})   # 0 = the bag end
    assert args == [] and extras["to_s"] is None


def test_env_shadows_nonzero_config_with_warn_only():
    cfg = {"play": {"start_offset_s": 3.5, "end_offset_s": 8}}
    _, _, _, args, warns, _ = _build(cfg=cfg, env={"RIG_REPLAY_FROM_S": "30",
                                                   "RIG_REPLAY_TO_S": "40"})
    assert args == ["--start-offset", "30", "--playback-duration", "10"]
    assert any("start_offset_s" in w and "shadowed" in w for w in warns)
    assert any("end_offset_s" in w and "shadowed" in w for w in warns)
    # a zero config value shadowed by the env is silent (nothing was overridden)
    _, _, _, _, warns, _ = _build(env={"RIG_REPLAY_FROM_S": "30"})
    assert not warns
    # per key: env FROM with config end keeps the config end
    _, _, _, args, warns, _ = _build(cfg={"play": {"end_offset_s": 45}},
                                     env={"RIG_REPLAY_FROM_S": "30"})
    assert args == ["--start-offset", "30", "--playback-duration", "15"] and not warns


def test_window_value_validation():
    _expect_exit("RIG_REPLAY_FROM_S must be a number", env={"RIG_REPLAY_FROM_S": "noon"})
    _expect_exit("RIG_REPLAY_TO_S must be >= 0", env={"RIG_REPLAY_TO_S": "-1"})
    _expect_exit("play.end_offset_s must be >= 0", cfg={"play": {"end_offset_s": -5}})
    _expect_exit("play.start_offset_s must be a number", cfg={"play": {"start_offset_s": "x"}})
    _expect_exit("unknown play key", cfg={"play": {"end_offset": 5}})        # the typo trap
    # empty env strings are ABSENT (compose interpolation defaults), not zero-with-a-warn
    _, _, _, args, warns, _ = _build(cfg={"play": {"start_offset_s": 2}},
                                     env={"RIG_REPLAY_FROM_S": "", "RIG_REPLAY_TO_S": ""})
    assert args == ["--start-offset", "2"] and not warns


# --- host-side validation against metadata.yaml (§1.1) ------------------------------------------

def test_from_at_or_past_the_bag_end_refuses():
    _expect_exit("window starts after the bag ends", env={"RIG_REPLAY_FROM_S": "49.91"})
    _expect_exit("window starts after the bag ends", env={"RIG_REPLAY_FROM_S": "500"})
    _expect_exit("window starts after the bag ends", cfg={"play": {"start_offset_s": 60}})


def test_empty_window_refuses():
    _expect_exit("empty window", env={"RIG_REPLAY_FROM_S": "30", "RIG_REPLAY_TO_S": "30"})
    _expect_exit("empty window", env={"RIG_REPLAY_FROM_S": "30", "RIG_REPLAY_TO_S": "20"})
    _expect_exit("empty window", cfg={"play": {"start_offset_s": 10, "end_offset_s": 5}})


def test_to_past_the_bag_end_warns_and_clamps_without_an_end_flag():
    _, _, _, args, warns, extras = _build(env={"RIG_REPLAY_FROM_S": "30",
                                               "RIG_REPLAY_TO_S": "120"})
    assert args == ["--start-offset", "30"]                     # clamped end = the bag's own end
    assert any("clamped" in w and "120" in w for w in warns)
    assert abs(extras["to_s"] - DURATION_NS / 1e9) < 1e-9       # the injector sees the clamp
    # a bound inside the recording is a real bound: no WARN, the flag is emitted
    _, _, _, args, warns, _ = _build(env={"RIG_REPLAY_TO_S": "49.9"})
    assert not warns and args == ["--playback-duration", "49.9"]


def test_metadata_is_required_only_for_windows_and_calls():
    # bare play: no metadata read at all (the CI fabricated tree, a metadata-less dir)
    _, _, _, args, _, extras = _build(meta=None)
    assert args == [] and extras["bag_start_ns"] is None
    _expect_exit("a window needs the session's metadata.yaml", meta=None,
                 env={"RIG_REPLAY_FROM_S": "1"})
    _expect_exit("the call injector needs the session's metadata.yaml", meta=None,
                 env={"RIG_REPLAY_CALLS": "/deploy/calls.yaml"})
    _expect_exit(META_PATH, meta=None, env={"RIG_REPLAY_TO_S": "1"})     # the path is named
    _expect_exit("not a rosbag2 metadata.yaml", meta="foo: 1\n", env={"RIG_REPLAY_FROM_S": "1"})
    _expect_exit("not valid YAML", meta="a: [\n", env={"RIG_REPLAY_FROM_S": "1"})


def test_calls_mode_exports_the_bag_start_even_without_a_window():
    _, _, _, args, _, extras = _build(env={"RIG_REPLAY_CALLS": "/deploy/calls.yaml"})
    assert args == [] and extras["bag_start_ns"] == START_NS
    assert (extras["from_s"], extras["to_s"], extras["latch"]) == (0.0, None, [])


# --- regression: the unwindowed replay is v1.10.x, byte for byte ---------------------------------

def test_unwindowed_replay_is_unchanged():
    cfg = {"play": {"rate": 1.0, "loop": False, "start_offset_s": 0, "start_paused": False,
                    "topics": ["/a", "/b"]}}
    _, _, _, args, warns, extras = _build(cfg=cfg, env={"RIG_SIM_TIME": "1", "RIG_REPLAY_FROM_S": "0"})
    assert args == ["--clock", "--topics", "/a", "/b"] and not warns
    assert extras["latch"] == [] and extras["bag_start_ns"] is None
    text = play_cmd.play_script("/replay/bags/bag_logger/" + S1, args, extras["latch"])
    assert "latch_restore" not in text                           # play.sh: no pre-pass lines
    assert text.endswith("exec ros2 bag play /replay/bags/bag_logger/" + S1
                         + " --clock --topics /a /b\n")


def test_windowed_play_script_runs_the_pre_pass_then_execs():
    _, _, _, args, _, extras = _build(env={"RIG_REPLAY_FROM_S": "30", "RIG_REPLAY_TOPICS": "/a"})
    text = play_cmd.play_script("/replay/bags/bag_logger/" + S1, args, extras["latch"])
    lines = text.splitlines()
    assert "python3 /latch_restore.py /replay/bags/bag_logger/" + S1 + " --from 30 --topics /a &" \
        in lines
    assert lines.index("LATCH_PID=$!") < lines.index(
        "exec ros2 bag play /replay/bags/bag_logger/" + S1 + " --start-offset 30 --topics /a")
    assert any(play_cmd.LATCH_READY in ln and "rm -f" in ln for ln in lines)   # stale marker gone
    assert lines[-1].startswith("exec ros2 bag play")           # exec is always the last line


def test_render_fields_for_the_launcher(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        real_read = play_cmd._read
        real_ls = play_cmd._ls
        src = pathlib.Path(d) / "run"
        sess = src / "bags" / "bag_logger" / S1
        sess.mkdir(parents=True)
        (sess / "x_0.mcap").write_bytes(b"")
        (sess / "metadata.yaml").write_text(META)
        cfg = {"name": "win_player", "source": {"run": str(src)}}
        out = play_cmd.render(cfg, {"RIG_REPLAY_FROM_S": "30", "RIG_REPLAY_TO_S": "500",
                                    "RIG_REPLAY_CALLS": "/deploy/calls.yaml"}, pathlib.Path(d))
        assert out[0] == "win_player" and out[5] == "/deploy/calls.yaml"
        assert out[6] == str(START_NS) and out[7] == "30"
        assert abs(float(out[8]) - DURATION_NS / 1e9) < 1e-6   # the CLAMPED end reaches the injector
        out = play_cmd.render(cfg, {}, pathlib.Path(d))         # bare: the trailing fields are empty
        assert out[6:] == ("", "", "")
        assert (play_cmd._read, play_cmd._ls) == (real_read, real_ls)


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
