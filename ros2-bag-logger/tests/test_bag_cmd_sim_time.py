"""bag_cmd sim-time adoption: RIG_SIM_TIME fills `control.use_sim_time`'s DEFAULT, explicit
config wins both ways (the rigging's `replay: {sim_time: true}` promise, rig >= v0.2.34).
Run: python3 tests/test_bag_cmd_sim_time.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import bag_cmd  # noqa: E402


def _args(cfg, env=None):
    _, _, _, args, _ = bag_cmd.build_args(cfg, env)
    return args


def test_env_fills_the_default():
    assert "--use-sim-time" in _args({}, "1")
    assert "--use-sim-time" not in _args({}, None)
    assert "--use-sim-time" not in _args({}, "")  # popped/empty env = not set


def test_explicit_config_wins_both_ways():
    on = {"control": {"use_sim_time": True}}
    off = {"control": {"use_sim_time": False}}
    assert "--use-sim-time" in _args(on, None)     # config true, no env
    assert "--use-sim-time" in _args(on, "1")      # agree
    assert "--use-sim-time" not in _args(off, "1")  # explicit false BEATS the env default
    assert "--use-sim-time" not in _args(off, None)


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
