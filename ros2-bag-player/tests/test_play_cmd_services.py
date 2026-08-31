"""The v1.10.0 half of the player's pure core (tools/play_cmd.py) against the frozen
service-call contract (rig-replay-calls-handoff §1.1–§1.2): RIG_REPLAY_SERVICES -> rosbag2
service playback (flags emitted just before the topic selector, requests-source knob strict,
default emits nothing), the refusal matrix (SERVICES+CALLS both set; SERVICES with no topic
selector mode; config `calls` + play.services both set), the shadowing WARNs (env wins, the
topics pattern), and the calls-path plumbing the launcher's `calls` profile gate consumes.
rig v0.2.37's `rig replay` is built against exactly this behavior — a change that breaks these
tests is a contract renegotiation, not a refactor.
Run: `python3 tests/test_play_cmd_services.py` (no ROS, no filesystem — the dir lister is
injected)."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import play_cmd

RUN = "/data/runs/run-042"
TREE = os.path.join(RUN, "bags", "bag_logger")
S1 = "bag_logger_20260827T100000Z"
CALLS = "/deploy/scripts/calls.yaml"


def _fs():
    return {TREE: [S1], os.path.join(TREE, S1): ["metadata.yaml", "x_0.mcap"]}


def _build(cfg=None, env=None):
    cfg = {"service": "ros2-bag-player", "name": "bag_player",
           "source": {"run": RUN}, **(cfg or {})}
    fs = _fs()
    return play_cmd.build_args(cfg, env or {}, lambda p: fs.get(p))


def _expect_exit(needle, **kw):
    try:
        _build(**kw)
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


# --- verbatim service playback (§1.1) ------------------------------------------------------------

def test_rig_services_ride_alongside_the_topic_allow_list():
    env = {"RIG_REPLAY_TOPICS": "/gnss/fix /imu/data",
           "RIG_REPLAY_SERVICES": "/planner/set_mode /planner/arm"}
    _, _, _, args, warns, extras = _build(env=env)
    assert args == ["--publish-service-requests",
                    "--services", "/planner/set_mode", "/planner/arm",
                    "--topics", "/gnss/fix", "/imu/data"]   # selector stays LAST (greedy)
    assert extras == {"calls": "", "services_source": "service"}
    assert not warns


def test_rig_services_ride_alongside_exclude_mode_too():
    env = {"RIG_REPLAY_EXCLUDE": r"/cam/.*", "RIG_REPLAY_SERVICES": "/planner/set_mode"}
    _, _, _, args, _, _ = _build(env=env)
    assert args == ["--publish-service-requests", "--services", "/planner/set_mode",
                    "--exclude-regex", "(?:/cam/.*)"]


def test_services_without_a_topic_selector_mode_is_refused():
    _expect_exit("topic selector mode", env={"RIG_REPLAY_SERVICES": "/planner/set_mode"})
    # config topics are NOT a topic selector MODE — rig always pairs the env with an env selector
    _expect_exit("topic selector mode", cfg={"play": {"topics": ["/a"]}},
                 env={"RIG_REPLAY_SERVICES": "/planner/set_mode"})


def test_env_services_shadow_config_services_with_warn():
    cfg = {"play": {"services": ["/from/config"]}}
    env = {"RIG_REPLAY_TOPICS": "/a", "RIG_REPLAY_SERVICES": "/from/rig"}
    _, _, _, args, warns, _ = _build(cfg=cfg, env=env)
    assert "/from/rig" in args and "/from/config" not in args
    assert any("play.services is shadowed" in w for w in warns)


def test_standalone_config_services_and_source_knob():
    cfg = {"play": {"topics": ["/a"], "services": ["/planner/set_mode"],
                    "services_source": "client"}}
    _, _, _, args, warns, extras = _build(cfg=cfg)
    assert args == ["--publish-service-requests",
                    "--service-requests-source", "client_introspection",
                    "--services", "/planner/set_mode", "--topics", "/a"]
    assert extras["services_source"] == "client" and not warns
    # the default source emits NO flag (rosbag2's own default is service_introspection)
    _, _, _, args, _, _ = _build(cfg={"play": {"topics": ["/a"],
                                               "services": ["/planner/set_mode"]}})
    assert "--service-requests-source" not in args


def test_services_source_is_strict():
    _expect_exit("services_source", cfg={"play": {"services_source": "server"}})
    _expect_exit("unknown play key", cfg={"play": {"servcies": ["/a"]}})


def test_no_services_emit_no_service_flags():
    _, _, _, args, _, _ = _build(env={"RIG_REPLAY_TOPICS": "/a"})
    assert "--publish-service-requests" not in args and "--services" not in args
    # the source knob alone (it also governs export-calls) emits nothing either
    _, _, _, args, _, extras = _build(cfg={"play": {"services_source": "client"}})
    assert args == [] and extras["services_source"] == "client"


# --- the call injector's path (§1.2) -------------------------------------------------------------

def test_rig_calls_resolves_and_emits_no_service_flags():
    _, _, _, args, warns, extras = _build(env={"RIG_REPLAY_CALLS": CALLS})
    assert extras["calls"] == CALLS
    assert "--publish-service-requests" not in args and not warns
    # works with or without a topic selector (a calls-only session is legal standalone)
    _, _, _, args, _, extras = _build(env={"RIG_REPLAY_TOPICS": "/a",
                                           "RIG_REPLAY_CALLS": CALLS})
    assert extras["calls"] == CALLS and args == ["--topics", "/a"]


def test_services_and_calls_both_set_is_refused():
    _expect_exit("both set", env={"RIG_REPLAY_TOPICS": "/a",
                                  "RIG_REPLAY_SERVICES": "/s", "RIG_REPLAY_CALLS": CALLS})
    _expect_exit("both set", cfg={"play": {"services": ["/s"], "topics": ["/a"]},
                                  "calls": CALLS})


def test_env_calls_shadows_config_calls_and_suppresses_config_services():
    cfg = {"calls": "/elsewhere/other.yaml", "play": {"services": ["/s"]}}
    _, _, _, args, warns, extras = _build(cfg=cfg, env={"RIG_REPLAY_CALLS": CALLS})
    assert extras["calls"] == CALLS
    assert "--services" not in args
    assert any("shadowed by RIG_REPLAY_CALLS" in w for w in warns)
    assert any("suppressed under RIG_REPLAY_CALLS" in w for w in warns)


def test_env_services_ignore_config_calls_with_warn():
    cfg = {"calls": CALLS}
    env = {"RIG_REPLAY_TOPICS": "/a", "RIG_REPLAY_SERVICES": "/s"}
    _, _, _, args, warns, extras = _build(cfg=cfg, env=env)
    assert extras["calls"] == "" and "--services" in args
    assert any("ignored under RIG_REPLAY_SERVICES" in w for w in warns)


def test_calls_path_validation():
    _expect_exit("absolute host path", env={"RIG_REPLAY_CALLS": "relative/calls.yaml"})
    _expect_exit("absolute host path", cfg={"calls": "relative.yaml"})
    _expect_exit("`calls` must be a string", cfg={"calls": {"path": CALLS}})
    # empty string = unset (the example config's commented default)
    _, _, _, _, _, extras = _build(cfg={"calls": ""})
    assert extras["calls"] == ""


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
