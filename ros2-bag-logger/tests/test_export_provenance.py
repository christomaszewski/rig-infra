"""Raw MCAP export checks, including failed conversion and safe replacement.

Requires mcap (the fleet image and CI install it); no ROS or Docker needed.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import yaml
from mcap.writer import Writer, CompressionType

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import bag_export
import export_provenance as provenance
from publication_identity import PREFIX


def sample(**kwargs):
    result = {"topic": "/imu", "data": b"payload", "sequence": 7, "log_time": 20,
              "publish_time": 15, "type_hash": "RIHS01_" + "a" * 64,
              "evidence": {"schema": "1", "kind": "rmw_publication", "scope": "boot:test",
                           "rmw": "rmw_zenoh_cpp", "domain_id": "42",
                           "writer_gid": "0123456789abcdef" * 2, "sequence_upper32": "5"}}
    result.update(kwargs)
    return result


def bag(path, parts, *, marker=True, chunked=True):
    path.mkdir(parents=True)
    paths, counts = [], {}
    for index, records in enumerate(parts):
        filename = f"part_{index}.mcap"
        paths.append(filename)
        with (path / filename).open("wb") as stream:
            writer = Writer(stream, compression=CompressionType.NONE, use_chunking=chunked,
                            enable_crcs=True, enable_data_crcs=True)
            writer.start(profile="ros2")
            schema = writer.register_schema(name="std_msgs/msg/String", encoding="ros2msg", data=b"string data\n")
            if marker and any(record["evidence"] for record in records):
                writer.add_metadata(provenance.MARKER, {"schema": "1", "contract": "rmw_publication"})
            for record in records:
                metadata = {PREFIX + key: value for key, value in record["evidence"].items()}
                metadata["topic_type_hash"] = record["type_hash"]
                channel = writer.register_channel(topic=record["topic"], message_encoding="cdr",
                                                  schema_id=schema, metadata=metadata)
                writer.add_message(channel_id=channel, data=record["data"],
                                   log_time=record["log_time"], publish_time=record["publish_time"],
                                   sequence=record["sequence"])
                counts[record["topic"]] = counts.get(record["topic"], 0) + 1
            writer.finish()
    info = {"storage_identifier": "mcap", "relative_file_paths": paths,
            "starting_time": {"nanoseconds_since_epoch": 0},
            "topics_with_message_count": [{"topic_metadata": {"name": key}, "message_count": count}
                                          for key, count in counts.items()]}
    (path / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": info}))
    return path


def capability(path):
    path.write_text(json.dumps({"schema_version": 1, "contract": "rmw_publication",
                               "preserve_mcap_convert": True, "rmw_implementations": ["rmw_zenoh_cpp"]}))
    return path


def spec(**kwargs):
    return {"output_bags": [{"all_topics": True, **kwargs}]}


def refused(fn, needle):
    try:
        fn()
    except (ValueError, ImportError) as exc:
        assert needle in str(exc), str(exc)
    else:
        raise AssertionError(f"expected refusal: {needle}")


def test_lossless_resplit_and_reordered_duplicates_preserve_exact_records():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        a, b = sample(), sample(data=b"other", sequence=8)
        old = sample(data=b"ordinary", evidence={}, sequence=19)
        source = bag(root / "source", [[a, b], [a, old]])
        output = bag(root / "output", [[old, b, a, a]], chunked=False)
        state = root / "state.sqlite"
        provenance.prepare(source, spec(), state, capability(root / "capability.json"))
        provenance.verify(output, state)


def test_lossless_default_keeps_hidden_service_and_action_samples():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        records = [sample(topic=topic, data=topic.encode()) for topic in
                   ("/imu", "/_hidden", "/set/_service_event", "/move/_action/status")]
        source = bag(root / "source", [records])
        output = bag(root / "output", [records[::-1]])
        options = bag_export.parse_options({})
        selected = bag_export.session_spec(str(source), str(output), options, None)
        assert all(selected["output_bags"][0][key] for key in
                   ("all_topics", "all_services", "all_actions", "include_hidden_topics"))
        state = root / "state.sqlite"
        provenance.prepare(source, selected, state, capability(root / "cap.json"))
        provenance.verify(output, state)
        filtered = bag_export.session_spec(str(source), str(output),
            bag_export.parse_options({"exclude": ["/_service_event$", "/status$"]}), None,
            [record["topic"] for record in records])
        assert filtered["output_bags"][0]["topics"] == ["/_hidden", "/imu"]
        assert filtered["output_bags"][0]["services"] == ["/_hidden", "/imu"]
        assert "exclude_regex" not in filtered["output_bags"][0]


def test_selected_topic_regex_and_inclusive_input_time_window():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        records = [sample(log_time=t, data=str(t).encode()) for t in (5, 10, 20, 21)]
        records += [sample(topic="/drop"), sample(topic="/_hidden")]
        source = bag(root / "source", [records])
        output = bag(root / "output", [[records[2], records[1]]])
        selected = spec(all_topics=False, topics=["/imu", "/drop"], exclude_regex="drop")
        selected["input_bags"] = [{"start_time_ns": 10, "end_time_ns": 20}]
        state = root / "state.sqlite"
        provenance.prepare(source, selected, state, capability(root / "capability.json"))
        provenance.verify(output, state)


def test_mutations_and_multiplicity_changes_fail_closed():
    a, b = sample(), sample(data=b"different", sequence=8)
    cases = [[a], [a, b, b]]
    for field, value in (("evidence", {}), ("sequence", 100), ("log_time", 21),
                         ("publish_time", 16), ("data", b"changed"), ("type_hash", "")):
        changed = copy.deepcopy(a)
        changed[field] = value
        cases.append([changed, b])
    # Matching sets of identities and payloads are insufficient: associations matter.
    cases.append([sample(data=b"different"), sample(data=b"payload", sequence=8)])
    for records in cases:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = bag(root / "source", [[a, b]])
            output = bag(root / "output", [records])
            state = root / "state.sqlite"
            provenance.prepare(source, spec(), state, capability(root / "capability.json"))
            refused(lambda: provenance.verify(output, state), "converted")


def test_capability_missing_mcap_unknown_schema_and_marker_loss_refuse():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = bag(root / "source", [[sample()]])
        state = root / "state.sqlite"
        refused(lambda: provenance.prepare(source, spec(), state, root / "missing"), "lacks verified")
        assert not state.exists()
        cap = capability(root / "cap.json")
        with patch.dict(sys.modules, {"mcap.reader": None}):
            refused(lambda: provenance.prepare(source, spec(), state, cap), "requires Python mcap")
        invalid = sample()
        invalid["evidence"]["schema"] = "2"
        source_bad = bag(root / "bad", [[invalid]])
        refused(lambda: provenance.prepare(source_bad, spec(), state, cap), "unsupported or malformed")
        output = bag(root / "output", [[sample()]], marker=False)
        provenance.prepare(source, spec(), state, cap)
        refused(lambda: provenance.verify(output, state), "uncompressed file marker")


def test_old_bags_need_no_capability_or_mcap_and_payload_cannot_spoof_marker():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = bag(root / "source", [[sample(evidence={}, data=provenance.MARKER.encode())]])
        state = root / "state.sqlite"
        assert not provenance.outer_evidence(source / "part_0.mcap")
        with patch.object(provenance, "require_capability", side_effect=AssertionError("not needed")):
            with patch.dict(sys.modules, {"mcap.reader": None}):
                provenance.prepare(source, spec(), state, root / "missing")
                provenance.verify(root / "not-opened-for-old-bags", state)


def test_source_change_after_preflight_prevents_publication():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = bag(root / "source", [[sample()]])
        output = bag(root / "output", [[sample()]])
        state = root / "state.sqlite"
        provenance.prepare(source, spec(), state, capability(root / "cap.json"))
        with (source / "part_0.mcap").open("ab") as stream:
            stream.write(b"another recording write")
        refused(lambda: provenance.verify(output, state), "source changed")


def test_identity_mirror_is_vendored_and_byte_identical():
    assert (REPO / "tools/publication_identity.py").read_bytes() == (
        REPO.parent / "ros2-bag-player/tools/publication_identity.py").read_bytes()
    descriptor = yaml.safe_load((REPO / "rigging.yaml").read_text())
    for name in ("export_provenance.py", "publication_identity.py"):
        assert f"tools/{name}" in descriptor["launch_surface"]


def test_file_compression_input_stage_is_private_and_refuses_escaping_paths():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        compressed = source / "part.db3.zstd"
        compressed.write_bytes(b"compressed fixture")
        metadata = source / "metadata.yaml"
        metadata.write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
            "storage_identifier": "sqlite3", "relative_file_paths": [compressed.name],
            "compression_mode": "FILE", "compression_format": "zstd"}}))
        selected = {**spec(), "input_bags": [{"uri": str(source), "start_time_ns": 10, "end_time_ns": 20}]}
        stage, input_spec = root / "stage", root / "input.yaml"
        provenance.stage_input(source, stage, selected, input_spec)
        assert (stage / compressed.name).read_bytes() == compressed.read_bytes()
        assert (stage / compressed.name).stat().st_ino != compressed.stat().st_ino
        (stage / "part.db3").write_bytes(b"decompressed")
        assert not (source / "part.db3").exists()
        assert yaml.safe_load(input_spec.read_text()) == {"input_bags": [
            {"uri": str(stage), "start_time_ns": 10, "end_time_ns": 20}]}
        (source / "escape.zstd").symlink_to(root / "input.yaml")
        metadata.write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
            "relative_file_paths": ["escape.zstd"]}}))
        refused(lambda: provenance.stage_input(source, root / "bad", selected, input_spec), "unsafe")
        assert not (root / "bad").exists()


def snapshot(path):
    return {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}


def host_flock(bin_path):
    """The flock command is Linux-only; exercise the same POSIX lock on macOS."""
    executable = bin_path / "flock"
    executable.write_text(f"#!{sys.executable}\nimport fcntl,sys\n"
                          "try: fcntl.flock(int(sys.argv[-1]),fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                          "except BlockingIOError: sys.exit(1)\n")
    executable.chmod(0o755)


def test_rendered_script_preserves_original_and_prior_export_when_provenance_is_lost():
    # Run the real rendered shell and verifier. The fake converter succeeds but
    # silently strips evidence, exactly the unsafe custom-runtime failure.
    for inplace in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "run"
            session = bag(source / "bags/logger/session", [[sample()]])
            destination = source if inplace else root / "export"
            destination.mkdir(exist_ok=True)
            final = destination / "bags/logger/session"
            if not inplace:
                bag(final, [[sample(data=b"prior export")]])
            before_source, before_final = snapshot(session), snapshot(final)
            cfg = root / "config.yaml"
            cfg.write_text("name: logger\n")
            cap = capability(root / "cap.json")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                bag_export.main([str(cfg), str(REPO)], {"RIG_EXPORT_SOURCE": str(source),
                    "RIG_EXPORT_DEST": str(destination), "RIG_EXPORT_FORCE": "1",
                    "RIG_EXPORT_INPLACE": "1" if inplace else "0"})
            bin_path = root / "bin"
            bin_path.mkdir()
            host_flock(bin_path)
            wrapper = bin_path / "python3"
            wrapper.write_text(f"#!{sys.executable}\nimport sys,runpy\n"
                               f"sys.path.insert(0,{str(REPO / 'tools')!r})\n"
                               "if sys.argv[1].endswith('export_provenance.py'):\n"
                               " import export_provenance as p\n"
                               f" p.capability_path=lambda:p.Path({str(cap)!r})\n"
                               " sys.exit(p.main(sys.argv[2:]))\n"
                               "sys.argv=sys.argv[1:]\nrunpy.run_path(sys.argv[0],run_name='__main__')\n")
            converter = bin_path / "ros2"
            converter.write_text(f"#!{sys.executable}\nimport sys,yaml\nfrom pathlib import Path\n"
                                 f"sys.path.insert(0,{str(Path(__file__).parent)!r})\n"
                                 "from test_export_provenance import bag,sample\n"
                                 "spec=yaml.safe_load(Path(sys.argv[sys.argv.index('-o')+1]).read_text())\n"
                                 "bag(Path(spec['output_bags'][0]['uri']),[[sample(evidence={})]])\n")
            wrapper.chmod(0o755)
            converter.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_path}:{os.environ['PATH']}"}
            script = next((destination / ".rig/export/logger").glob("*/convert.sh"))
            result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
            assert result.returncode == 1, result.stderr
            assert "publication verification FAILED" in result.stderr
            assert snapshot(session) == before_source
            assert snapshot(final) == before_final
            assert not list(final.parent.glob(".rig-*/*"))


def test_unique_renders_and_runtime_lock_prevent_competing_exports():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run, destination = root / "run", root / "export"
        bag(run / "bags/logger/session", [[sample()]])
        destination.mkdir()
        cfg = root / "cfg.yaml"
        cfg.write_text("name: logger\n")
        rendered = []
        for _ in range(2):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                bag_export.main([str(cfg), str(REPO)], {"RIG_EXPORT_SOURCE": str(run),
                    "RIG_EXPORT_DEST": str(destination)})
            rendered.append(Path(output.getvalue().split("\t")[0]))
        assert rendered[0] != rendered[1]
        assert rendered[0].read_text() != rendered[1].read_text()
        # Hold the first shell immediately after its actual lock acquisition;
        # the second must exit before it can touch staging or recovery files.
        _, first_text, _ = bag_export.plan(str(run), str(destination), "bags", "absent",
                                           bag_export.parse_options({}), force=False, spec_dir=root)
        marker = root / "locked"
        first_text = first_text.replace("# Recover", f"touch '{marker}'\nread -r release\n# Recover")
        first, second = root / "first.sh", root / "second.sh"
        first.write_text(first_text)
        second.write_text(first_text.replace(f"touch '{marker}'\nread -r release\n", ""))
        bin_path = root / "bin"
        bin_path.mkdir()
        host_flock(bin_path)
        env = {**os.environ, "PATH": f"{bin_path}:{os.environ['PATH']}"}
        process = subprocess.Popen(["bash", str(first)], env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert marker.exists(), process.communicate(timeout=1)
            result = subprocess.run(["bash", str(second)], env=env, capture_output=True, text=True)
            assert result.returncode == 1 and "another export is active" in result.stderr
            process.communicate("release\n", timeout=5)
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        # The lock is released by exit, not by removing/recreating its inode.
        result = subprocess.run(["bash", str(second)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_crash_original_is_planned_restored_before_conversion_and_not_stranded():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run = root / "run"
        original = bag(run / "bags/logger/session", [[sample()]])
        before = snapshot(original)
        backup = original.parent / ".rig-rewrite/session.orig"
        backup.parent.mkdir()
        original.rename(backup)
        jobs, script, warnings = bag_export.plan(str(run), str(run), "bags", "logger",
            bag_export.parse_options({"from_s": 0}), force=False, spec_dir=root, in_place=True)
        assert len(jobs) == 1 and jobs[0][0] == original and not warnings
        # The absent original must restore under the actual lock even when the
        # following preflight fails (no helper intentionally installed here).
        bin_path = root / "bin"
        bin_path.mkdir()
        host_flock(bin_path)
        script_path = root / "convert.sh"
        script_path.write_text(script)
        result = subprocess.run(["bash", str(script_path)], text=True, capture_output=True,
            env={**os.environ, "PATH": f"{bin_path}:{os.environ['PATH']}"})
        assert result.returncode == 1
        assert snapshot(original) == before and not backup.exists()


def test_nonforce_rechecks_completed_export_after_locking():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, destination = root / "run", root / "export"
        bag(source / "bags/logger/session", [[sample()]])
        jobs, script, _ = bag_export.plan(str(source), str(destination), "bags", "logger",
            bag_export.parse_options({}), force=False, spec_dir=root)
        assert len(jobs) == 1
        final = bag(destination / "bags/logger/session", [[sample(data=b"other completed export")]])
        before = snapshot(final)
        bin_path = root / "bin"
        bin_path.mkdir()
        host_flock(bin_path)
        script_path = root / "convert.sh"
        script_path.write_text(script)
        result = subprocess.run(["bash", str(script_path)], text=True, capture_output=True,
            env={**os.environ, "PATH": f"{bin_path}:{os.environ['PATH']}"})
        assert result.returncode == 0 and "after locking" in result.stderr
        assert snapshot(final) == before


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:
                failures += 1
                import traceback
                print("FAIL", name, "->", exc)
                traceback.print_exc()
    sys.exit(bool(failures))
