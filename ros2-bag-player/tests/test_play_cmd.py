"""The bag player's pure core (tools/play_cmd.py) against the frozen replay contract
(rig-replay-player-handoff §1): the env channel (RIG_REPLAY_TOPICS xor RIG_REPLAY_EXCLUDE with
both-set refused; RIG_SIM_TIME -> --clock and a play.clock key refused at parse), allow-mode
filtering (config excludes prune the list BEFORE --topics — never an allow flag and an exclude
flag together, and an emptied selection refuses rather than playing everything), exclude-mode
alternation merge, host-side session resolution (latest/explicit/multiple/missing, refusals
naming the path looked at), and the standalone fallbacks rig-less use depends on. rig v0.2.33's
`rig replay` is built against exactly this behavior — a change that breaks these tests is a
contract renegotiation, not a refactor.
Run: `python3 tests/test_play_cmd.py` (no ROS, no filesystem — the dir lister is injected)."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import play_cmd

RUN = "/data/runs/run-042"
TREE = os.path.join(RUN, "bags", "bag_logger")
S1, S2 = "bag_logger_20260827T100000Z", "bag_logger_20260828T090000Z"
# v1.12.0: an offset/window (or a call script) reads the session's metadata.yaml host-side — the
# injected reader serves this 120 s recording so the frozen v1.8.0 knob assertions stay verbatim.
META = """rosbag2_bagfile_information:
  version: 9
  storage_identifier: mcap
  duration: {nanoseconds: 120000000000}
  starting_time: {nanoseconds_since_epoch: 1788361659604888347}
  topics_with_message_count: []
"""


def _fs(sessions=(S1,), files=("meta.yaml",), bag="x_0.mcap"):
    fs = {TREE: list(sessions)}
    for s in sessions:
        fs[os.path.join(TREE, s)] = [bag, *files]
    return fs


def _ls(fs):
    return lambda path: fs.get(path)


def _read(path):
    return META if path.endswith("metadata.yaml") else None


def _build(cfg=None, env=None, fs=None):
    cfg = {"service": "ros2-bag-player", "name": "bag_player",
           "source": {"run": RUN}, **(cfg or {})}
    # v1.10.0 grew a 6th element (extras: calls path + services_source) — sliced off here so the
    # frozen v1.8.0 assertions stay verbatim; test_play_cmd_services.py covers the extras.
    return play_cmd.build_args(cfg, env or {}, _ls(_fs() if fs is None else fs), _read)[:5]


def _expect_exit(needle, **kw):
    try:
        _build(**kw)
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


# --- selector modes ------------------------------------------------------------------------------

def test_rig_allow_mode_filters_with_config_excludes_before_topics():
    cfg = {"play": {"exclude": [r".*/image_raw(/.*)?$"]}}
    env = {"RIG_REPLAY_TOPICS": "/gnss/fix /cam/image_raw /imu/data"}
    _, _, _, args, warns = _build(cfg=cfg, env=env)
    assert args == ["--topics", "/gnss/fix", "/imu/data"]      # filtered, allow flag only
    assert "--exclude-regex" not in args                        # NEVER both flags together
    assert any("/cam/image_raw" in w for w in warns)            # the drop is named


def test_rig_exclude_mode_merges_env_and_config_into_one_alternation():
    cfg = {"play": {"exclude": [r"/diag/.*"]}}
    env = {"RIG_REPLAY_EXCLUDE": r"/cam/.*"}
    _, _, _, args, _ = _build(cfg=cfg, env=env)
    assert args == ["--exclude-regex", "(?:/cam/.*)|(?:/diag/.*)"]
    assert "--topics" not in args


def test_both_selectors_set_is_refused():
    _expect_exit("both set", env={"RIG_REPLAY_TOPICS": "/a", "RIG_REPLAY_EXCLUDE": "/b"})


def test_empty_selection_after_filter_is_refused_not_play_everything():
    _expect_exit("empty after play.exclude",
                 cfg={"play": {"exclude": [".*"]}}, env={"RIG_REPLAY_TOPICS": "/a /b"})


def test_standalone_config_topics_and_excludes_govern_without_rig_env():
    _, _, _, args, warns = _build(cfg={"play": {"topics": ["/a", "/b"]}})
    assert args == ["--topics", "/a", "/b"] and not warns
    _, _, _, args, _ = _build(cfg={"play": {"exclude": [r"/x.*", r"/y.*"]}})
    assert args == ["--exclude-regex", "(?:/x.*)|(?:/y.*)"]
    _, _, _, args, _ = _build()                                 # nothing at all: bare play
    assert args == []


def test_env_selector_shadows_config_topics_with_warn():
    cfg = {"play": {"topics": ["/from/config"]}}
    _, _, _, args, warns = _build(cfg=cfg, env={"RIG_REPLAY_TOPICS": "/from/rig"})
    assert args == ["--topics", "/from/rig"] and any("shadowed" in w for w in warns)
    _, _, _, args, warns = _build(cfg=cfg, env={"RIG_REPLAY_EXCLUDE": "/cam/.*"})
    assert args[0] == "--exclude-regex" and any("ignored" in w for w in warns)


def test_bad_exclude_regex_is_refused_at_parse():
    _expect_exit("does not compile", cfg={"play": {"exclude": ["(["]}})


# --- clock ---------------------------------------------------------------------------------------

def test_clock_derives_from_rig_sim_time_alone():
    assert "--clock" in _build(env={"RIG_SIM_TIME": "1"})[3]
    assert "--clock" not in _build()[3]
    assert "--clock" not in _build(env={"RIG_SIM_TIME": ""})[3]


def test_play_clock_key_is_refused():
    _expect_exit("play.clock", cfg={"play": {"clock": True}})
    _expect_exit("play.clock", cfg={"play": {"clock": False}})  # even asking for coherence: one owner


# --- session resolution (§1.3) -------------------------------------------------------------------

def test_source_env_wins_over_config_run():
    fs = {os.path.join("/elsewhere", "bags", "bag_logger"): [S1],
          os.path.join("/elsewhere", "bags", "bag_logger", S1): ["a.mcap"]}
    name, source, path, _, _ = _build(env={"RIG_REPLAY_SOURCE": "/elsewhere"}, fs=fs)
    assert (name, source) == ("bag_player", "/elsewhere")
    assert path == f"/replay/bags/bag_logger/{S1}"


def test_no_source_anywhere_is_refused_with_usage():
    _expect_exit("RIG_REPLAY_SOURCE", cfg={"source": {"run": ""}})
    _expect_exit("absolute", cfg={"source": {"run": "relative/run"}})


def test_session_latest_picks_greatest_stamp_and_warns_on_multiple():
    fs = _fs(sessions=(S1, S2))
    _, _, path, _, warns = _build(fs=fs)
    assert path.endswith(S2)                                    # stamps sort chronologically
    assert any("skipping " + S1 in w for w in warns)            # skipped sessions are NAMED
    _, _, _, _, warns = _build(fs=_fs())                        # one session: silent
    assert not warns


def test_session_explicit_stamp_or_full_dirname():
    fs = _fs(sessions=(S1, S2))
    for sess in ("20260827T100000Z", S1):                       # bare stamp or the full dir name
        _, _, path, _, warns = _build(cfg={"source": {"run": RUN, "session": sess}}, fs=fs)
        assert path.endswith(S1) and not warns                  # explicit: no multiple-WARN


def test_missing_tree_session_and_bags_refuse_naming_the_path():
    _expect_exit(os.path.join(RUN, "bags", "other"),
                 cfg={"source": {"run": RUN, "logger": "other"}})
    _expect_exit(os.path.join(TREE, "bag_logger_20990101T000000Z"),
                 cfg={"source": {"run": RUN, "session": "20990101T000000Z"}})
    _expect_exit("no stamped recording dirs", fs={TREE: ["not_a_session"]})
    fs = {TREE: [S1], os.path.join(TREE, S1): ["metadata.yaml"]}   # metadata alone ≠ a recording
    _expect_exit("no bag files", fs=fs)


def test_split_recording_is_one_session():
    fs = {TREE: [S1], os.path.join(TREE, S1): ["metadata.yaml", "x_0.mcap", "x_1.mcap", "x_2.mcap"]}
    _, _, path, args, warns = _build(fs=fs)
    assert path.endswith(S1) and not warns and args == []       # the DIR plays, splits included


# --- knobs -> argv -------------------------------------------------------------------------------

def test_knob_mapping_and_flag_order():
    cfg = {"play": {"rate": 2.0, "loop": True, "start_offset_s": 3.5, "start_paused": True,
                    "topics": ["/a"]}}
    _, _, _, args, warns = _build(cfg=cfg, env={"RIG_SIM_TIME": "1"})
    assert args == ["-r", "2", "--loop", "--start-offset", "3.5", "--start-paused", "--clock",
                    "--topics", "/a"]                           # --topics greedy -> always LAST
    assert any("loop" in w and "down" in w for w in warns)      # loop WARNs: down is the only exit


def test_default_knobs_emit_nothing():
    cfg = {"play": {"rate": 1.0, "loop": False, "start_offset_s": 0, "start_paused": False}}
    assert _build(cfg=cfg)[3] == []


def test_knob_validation():
    _expect_exit("rate", cfg={"play": {"rate": 0}})
    _expect_exit("start_offset_s", cfg={"play": {"start_offset_s": -1}})
    _expect_exit("unknown play key", cfg={"play": {"topcs": ["/a"]}})       # the silent-typo trap
    _expect_exit("unknown source key", cfg={"source": {"run": RUN, "sesion": "latest"}})


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
