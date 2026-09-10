"""The `export` verb (v1.14.0): bag_export's pure core — sessions -> `ros2 bag convert` specs
(preset, exclude, topics, a window off the session's metadata), the options schema (unknown
keys refuse), idempotency/force — and the launcher against a docker stub: the one-off
`compose run` with both dirs mounted at their host paths, the refusals without rig's channel.
Run: python3 tests/test_bag_export.py
"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import bag_export  # noqa: E402

LAUNCHER = REPO / "ros2-bag-logger-up"
T0 = 1_767_225_600_000_000_000
META = ("rosbag2_bagfile_information:\n  version: 9\n  storage_identifier: mcap\n"
        "  duration: {nanoseconds: 120000000000}\n"
        f"  starting_time: {{nanoseconds_since_epoch: {T0}}}\n  topics_with_message_count: []\n")

STUB = """#!/usr/bin/env bash
{
  printf 'ARGV:%s\\n' "$*"
  printf 'ENV:COMPOSE_PROJECT_NAME=%s\\n' "${COMPOSE_PROJECT_NAME-}"
  printf 'ENV:BAG_RECORD_SCRIPT=%s\\n' "${BAG_RECORD_SCRIPT-}"
} >> "$DOCKER_STUB_LOG"
"""


def _run_tree(tmp: pathlib.Path, *, sessions=("bag_logger_20260101T000000Z", "bag_logger_20260101T001000Z"),
              unindexed=(), name="bag_logger") -> tuple[pathlib.Path, pathlib.Path]:
    run = tmp / "run"
    for sess in sessions:
        d = run / "bags" / name / sess
        d.mkdir(parents=True)
        (d / f"{sess}_0.mcap").write_bytes(b"x" * 100)
        (d / "metadata.yaml").write_text(META)
    for sess in unindexed:
        d = run / "bags" / name / sess
        d.mkdir(parents=True)
        (d / f"{sess}_0.mcap").write_bytes(b"x" * 100)
    (run / "bags" / name).mkdir(parents=True, exist_ok=True)
    (run / "bags" / name / "notes.txt").write_text("not a session")
    dest = tmp / "run" / "exports" / "review"
    (dest / "bags" / name).mkdir(parents=True)
    return run, dest


def _plan(run, dest, opts, *, force=False, name="bag_logger"):
    return bag_export.plan(str(run), str(dest), "bags", name, bag_export.parse_options(opts),
                           force=force, spec_dir=dest / ".rig" / "export" / name)


# --- the options schema ----------------------------------------------------------------------------

def test_options_defaults_and_refusals():
    d = bag_export.parse_options(None)
    assert d == {"preset": "zstd_small", "exclude": [], "topics": [], "from_s": None, "to_s": None}
    assert bag_export.parse_options({})["preset"] == "zstd_small"
    o = bag_export.parse_options({"preset": "ZSTD_FAST", "exclude": ".*/points$", "topics": ["/a"],
                                  "from_s": 3, "to_s": "10.5"})
    assert o == {"preset": "zstd_fast", "exclude": [".*/points$"], "topics": ["/a"],
                 "from_s": 3.0, "to_s": 10.5}
    for bad, needle in (({"presets": "zstd_small"}, "unknown export option"),
                        ({"preset": "lzma"}, "unknown preset"),
                        ({"exclude": [".*/points(" ]}, "not a regex"),
                        ({"topics": [1]}, "topic names"),
                        ({"from_s": "soon"}, "seconds"),
                        ({"from_s": -1}, ">= 0"),
                        ({"from_s": 10, "to_s": 5}, "greater than"),
                        ("zstd_small", "mapping")):
        try:
            bag_export.parse_options(bad)
            assert False, f"must refuse {bad!r}"
        except SystemExit as exc:
            assert needle in str(exc), str(exc)


# --- the plan ----------------------------------------------------------------------------------------

def test_specs_per_session_with_the_default_preset():
    with tempfile.TemporaryDirectory() as tmp:
        run, dest = _run_tree(pathlib.Path(tmp))
        jobs, script, warns = _plan(run, dest, {})
        assert warns == []
        assert [j[0].name for j in jobs] == ["bag_logger_20260101T000000Z", "bag_logger_20260101T001000Z"]
        sess, spec, spec_path = jobs[0]
        assert spec == {"output_bags": [{"uri": str(dest / "bags" / "bag_logger" / sess.name),
                                         "storage_id": "mcap", "storage_preset_profile": "zstd_small",
                                         "all_topics": True}]}
        assert spec_path == dest / ".rig" / "export" / "bag_logger" / f"{sess.name}.yaml"
        assert f"ros2 bag convert -i {sess} -o {spec_path}" in script
        assert script.count("ros2 bag convert -i") == 2
        assert "setup.bash" in script and script.startswith("#!/usr/bin/env bash")
        assert "exit $rc" in script


def test_exclude_topics_and_window_from_the_session_start():
    with tempfile.TemporaryDirectory() as tmp:
        run, dest = _run_tree(pathlib.Path(tmp), sessions=("bag_logger_20260101T000000Z",))
        jobs, _, _ = _plan(run, dest, {"preset": "none", "exclude": [".*/points$", "^/cam/.*"],
                                       "topics": ["/imu/data", "/gnss/fix"], "from_s": 5, "to_s": 15})
        spec = jobs[0][1]["output_bags"][0]
        assert "storage_preset_profile" not in spec               # none = uncompressed
        assert spec["exclude_regex"] == "(?:.*/points$)|(?:^/cam/.*)"
        assert spec["topics"] == ["/imu/data", "/gnss/fix"] and spec["all_topics"] is False
        assert spec["start_time_ns"] == T0 + 5_000_000_000
        assert spec["end_time_ns"] == T0 + 15_000_000_000
        jobs, _, _ = _plan(run, dest, {"exclude": ["^/chatter$"]})
        assert jobs[0][1]["output_bags"][0]["exclude_regex"] == "^/chatter$"   # one = as written


def test_unindexed_session_skipped_with_the_fix_named_and_window_needs_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        run, dest = _run_tree(pathlib.Path(tmp), sessions=("bag_logger_20260101T000000Z",),
                              unindexed=("bag_logger_20260101T002000Z",))
        jobs, _, warns = _plan(run, dest, {})
        assert [j[0].name for j in jobs] == ["bag_logger_20260101T000000Z"]
        assert len(warns) == 1 and "no metadata.yaml" in warns[0] and "ros2 bag reindex" in warns[0]
        (run / "bags" / "bag_logger" / "bag_logger_20260101T000000Z" / "metadata.yaml").write_text("x: 1\n")
        try:
            _plan(run, dest, {"from_s": 1})
            assert False
        except SystemExit as exc:
            assert "starting_time" in str(exc)


def test_idempotent_unless_force_and_nothing_to_export():
    with tempfile.TemporaryDirectory() as tmp:
        run, dest = _run_tree(pathlib.Path(tmp))
        done = dest / "bags" / "bag_logger" / "bag_logger_20260101T000000Z"
        done.mkdir(parents=True)
        (done / "metadata.yaml").write_text(META)
        jobs, script, warns = _plan(run, dest, {})
        assert [j[0].name for j in jobs] == ["bag_logger_20260101T001000Z"]
        assert any("already exported" in w for w in warns)
        jobs, script, _ = _plan(run, dest, {}, force=True)
        assert len(jobs) == 2 and f"rm -rf {done}" in script      # convert refuses an existing dir
        jobs, _, warns = _plan(run, dest, {}, name="other_logger")
        assert jobs == [] and any("no sessions" in w for w in warns)


# --- main + the launcher -------------------------------------------------------------------------------

def test_main_writes_specs_and_script_and_prints_the_handoff():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        run, dest = _run_tree(tmp)
        cfg = tmp / "bag_logger.yaml"
        cfg.write_text("service: ros2-bag-logger\nname: bag_logger\n")
        opts = tmp / "opts.yaml"
        opts.write_text("preset: zstd_small\nexclude: ['.*/points$']\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bag_export.main([str(cfg), str(REPO)],
                                 {"RIG_EXPORT_SOURCE": str(run), "RIG_EXPORT_DEST": str(dest),
                                  "RIG_EXPORT_OPTIONS": str(opts), "RIG_EXPORT_PROFILE": "review"})
        assert rc == 0
        script, count, name = out.getvalue().strip().split("\t")
        assert count == "2" and name == "bag_logger"
        assert script == str(dest / ".rig" / "export" / "bag_logger" / "convert.sh")
        assert os.access(script, os.X_OK)
        spec = yaml.safe_load((dest / ".rig" / "export" / "bag_logger" /
                               "bag_logger_20260101T000000Z.yaml").read_text())
        assert spec["output_bags"][0]["exclude_regex"] == ".*/points$"
        assert "2 session(s) to convert, preset=zstd_small" in err.getvalue()
        for env, needle in (({"RIG_EXPORT_DEST": str(dest)}, "RIG_EXPORT_SOURCE"),
                            ({"RIG_EXPORT_SOURCE": str(run)}, "RIG_EXPORT_DEST")):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    bag_export.main([str(cfg), str(REPO)], env)
                assert False
            except SystemExit as exc:
                assert needle in str(exc)


def _launch(tmp: pathlib.Path, verb: str, env_extra: dict) -> tuple[subprocess.CompletedProcess, str]:
    (tmp / "bin").mkdir(exist_ok=True)
    stub = tmp / "bin" / "docker"
    stub.write_text(STUB)
    stub.chmod(0o755)
    cfg = tmp / "bag_logger.yaml"
    cfg.write_text("service: ros2-bag-logger\nname: bag_logger\n")
    log = tmp / "docker.log"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("RIG_EXPORT") and k != "COMPOSE_PROJECT_NAME"}
    env["PATH"] = f"{tmp / 'bin'}:{env.get('PATH', '')}"
    env["DOCKER_STUB_LOG"] = str(log)
    env.update(env_extra)
    proc = subprocess.run([str(LAUNCHER), str(cfg), verb], env=env, capture_output=True, text=True)
    return proc, log.read_text() if log.exists() else ""


def test_launcher_export_runs_a_one_off_convert_with_both_dirs_mounted():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        run, dest = _run_tree(tmp)
        proc, log = _launch(tmp, "export", {"RIG_EXPORT_SOURCE": str(run), "RIG_EXPORT_DEST": str(dest),
                                            "COMPOSE_PROJECT_NAME": "bag_logger-vehicle-7"})
        assert proc.returncode == 0, proc.stderr
        script = dest / ".rig" / "export" / "bag_logger" / "convert.sh"
        assert script.is_file()
        argv = [line for line in log.splitlines() if line.startswith("ARGV:")][0]
        assert argv.startswith(f"ARGV:compose -f {REPO}/docker/compose.deploy.yaml run --rm --no-deps -T ")
        assert f"-v {run}:{run}:ro" in argv and f"-v {dest}:{dest}" in argv
        assert argv.endswith(f"bag-logger /bin/bash {script}")
        assert "ENV:COMPOSE_PROJECT_NAME=bag_logger-vehicle-7" in log     # rig's project honored
        assert f"ENV:BAG_RECORD_SCRIPT={script}" in log                   # the :? mount satisfied
        assert "sessions=2" in proc.stderr


def test_launcher_export_refuses_without_the_channel_and_skips_docker_when_empty():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        proc, log = _launch(tmp, "export", {})
        assert proc.returncode != 0 and "RIG_EXPORT_SOURCE" in proc.stderr and "ARGV" not in log
        run, dest = _run_tree(tmp, sessions=())
        proc, log = _launch(tmp, "export", {"RIG_EXPORT_SOURCE": str(run), "RIG_EXPORT_DEST": str(dest)})
        assert proc.returncode == 0 and "nothing to convert" in proc.stderr and "ARGV" not in log
        assert "no sessions under" in proc.stderr


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                print("FAIL", name, "->", exc)
                traceback.print_exc()
    sys.exit(1 if failures else 0)
