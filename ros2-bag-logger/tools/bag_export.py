#!/usr/bin/env python3
"""ros2-bag-logger `export`: re-write the sessions this instance recorded into a run SMALLER, for
the trip off the vehicle (rig ≥ v0.2.54 `rig run export` / `rig fleet sync --profile`). SELECTS +
PARAMETERIZES ``ros2 bag convert`` (rosbag2's own re-writer: one output spec per session) — it
never converts anything itself; the launcher runs the rendered script in the logger's image.

The channel (rig's, per the rigging's ``export: {data: bags/{name}}``):
  RIG_EXPORT_SOURCE   the run dir (sessions under <source>/<output.subdir>/<name>/<session>/)
  RIG_EXPORT_DEST     the export dir — the slim sessions go to the SAME relative path under it
  RIG_EXPORT_OPTIONS  a YAML file: the profile's block for this instance (schema below; rig
                      never reads it — absent/empty = the defaults)
  RIG_EXPORT_PROFILE  its name (log lines only)
  RIG_EXPORT_FORCE    "1": redo a session already exported (replace only after verification)
  RIG_EXPORT_NAME     the DATA name to work on when it is not this config's `name` — a run from
                      another deployment, an instance since renamed (rig >= v0.2.59)
  RIG_EXPORT_INPLACE  "1": REWRITE the sessions inside the run (`rig run export --in-place`):
                      convert into <tree>/.rig-rewrite/<session>, VERIFY the per-topic message
                      counts against the original's metadata.yaml, then swap the new session in
                      and remove the old one. A session that does not verify stays untouched.
                      Needs free space for one converted session at a time (checked).
  RIG_EXPORT_LOSSY    "1": allow in-place options that DROP data for good (exclude, topics,
                      from_s/to_s) — refused without it; a copy-export needs no such gate

The options (every key optional; unknown keys REFUSE — a typo must not silently keep the data):
  preset: zstd_small        mcap chunk compression for the output (none|fastwrite|zstd_fast|
                            zstd_small; default zstd_small — smallest, the point of an export)
  exclude: [regex, ...]     full topic names to DROP (e.g. '.*/points$'); resolved to exact
                            retained names so hidden, service and action interfaces agree
  topics: [/exact, ...]     an ALLOW list instead of everything (composes with exclude)
  from_s / to_s: seconds    a time window, counted from EACH session's bag start (its
                            metadata.yaml starting_time — the same zero rig replay --from uses)
  split_duration_s: N       roll the OUTPUT every N seconds (default: one file per session — a
  max_size_mb: N            multi-file recording converts to ONE file unless split here)

A session is a ``<name>_<UTCstamp>/`` dir holding ``metadata.yaml`` + its .mcap files — exactly
what the recorder writes. A dir with bag files but no metadata.yaml (the recorder died mid-run)
is SKIPPED with a warning naming the fix (``ros2 bag reindex`` on the vehicle) — never a guess.
Idempotent: a session whose output already carries a metadata.yaml is skipped unless FORCE.

``plan(...)`` is the pure core (sessions -> specs + the script text); ``main`` writes the specs
and the script under <dest>/.rig/export/<name>/<invocation>/ (visible in-container at the same path, since
the launcher mounts both dirs at their host paths) and prints ``<script>\\t<count>\\t<name>``.
"""
from __future__ import annotations

import os
import math
import pathlib
import re
import shlex
import shutil
import sys
import uuid

import yaml

PRESETS = ("none", "fastwrite", "zstd_fast", "zstd_small")
KNOWN = {"preset", "exclude", "topics", "from_s", "to_s", "split_duration_s", "max_size_mb"}
REWRITE_DIR = ".rig-rewrite"
EXPORT_WORK_DIR = ".rig-export"

# Runs INSIDE the image (python3 + yaml ship with ros2cli): compare the converted session's
# per-topic message counts with the original's. all = every topic with messages must match
# (lossless); kept = every topic still present must match (topics dropped on purpose);
# any = a window was cut, only require a non-empty result.
VERIFY_PY = """\
import sys, yaml
def counts(path):
    info = yaml.safe_load(open(path))["rosbag2_bagfile_information"]
    return {t["topic_metadata"]["name"]: int(t["message_count"])
            for t in info.get("topics_with_message_count") or [] if int(t["message_count"]) > 0}
orig, new, mode = counts(sys.argv[1]), counts(sys.argv[2]), sys.argv[3]
if mode == "all":
    bad = {k: (orig.get(k), new.get(k)) for k in set(orig) | set(new) if orig.get(k) != new.get(k)}
elif mode == "kept":
    bad = {k: (orig.get(k), v) for k, v in new.items() if orig.get(k) != v}
    if not new:
        bad = {"<all>": ("messages", "none")}
else:
    bad = {} if new else {"<all>": ("messages", "none")}
if bad:
    print("bag_export: VERIFY FAILED (topic: original -> converted):", file=sys.stderr)
    for k, (a, b) in sorted(bad.items())[:12]:
        print(f"  {k}: {a} -> {b}", file=sys.stderr)
    sys.exit(1)
print(f"bag_export: verified {sum(new.values())} message(s) on {len(new)} topic(s) [{mode}]", file=sys.stderr)
"""
DEFAULT_PRESET = "zstd_small"
ROS_SOURCE = ('[ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ] '
              '&& . "/opt/ros/$ROS_DISTRO/setup.bash"')


def parse_options(raw) -> dict:
    """The profile block for this instance, validated. None/{} = defaults."""
    opts = {} if raw is None else raw
    if not isinstance(opts, dict):
        raise SystemExit("bag_export: the export options must be a mapping "
                         "(preset, exclude, topics, from_s, to_s)")
    unknown = set(opts) - KNOWN
    if unknown:
        raise SystemExit(f"bag_export: unknown export option(s): {', '.join(sorted(unknown))} "
                         f"(known: {', '.join(sorted(KNOWN))})")
    preset = str(opts.get("preset") or DEFAULT_PRESET).lower()
    if preset not in PRESETS:
        raise SystemExit(f"bag_export: unknown preset '{preset}' ({'|'.join(PRESETS)})")
    out = {"preset": preset}
    for key in ("exclude", "topics"):
        val = opts.get(key) or []
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list) or not all(isinstance(v, str) and v.strip() for v in val):
            raise SystemExit(f"bag_export: {key} must be a list of "
                             f"{'regex patterns' if key == 'exclude' else 'topic names'}")
        out[key] = [v.strip() for v in val]
    for pat in out["exclude"]:
        try:
            re.compile(pat)
        except re.error as exc:
            raise SystemExit(f"bag_export: exclude pattern {pat!r} is not a regex ({exc})")
    for key in ("from_s", "to_s"):
        val = opts.get(key)
        if val is None:
            out[key] = None
            continue
        try:
            sec = float(val)
        except (TypeError, ValueError):
            raise SystemExit(f"bag_export: {key} must be seconds (got {val!r})")
        if not math.isfinite(sec) or sec < 0:
            raise SystemExit(f"bag_export: {key} must be finite and >= 0")
        out[key] = sec
    if out["from_s"] is not None and out["to_s"] is not None and out["to_s"] <= out["from_s"]:
        raise SystemExit("bag_export: to_s must be greater than from_s")
    for key in ("split_duration_s", "max_size_mb"):
        val = opts.get(key)
        try:
            out[key] = int(val) if val is not None else 0
        except (TypeError, ValueError):
            raise SystemExit(f"bag_export: {key} must be a whole number (got {val!r})")
        if out[key] < 0:
            raise SystemExit(f"bag_export: {key} must be >= 0")
    return out


def lossy_reasons(opts: dict) -> list[str]:
    """The options that DROP data — harmless in a copy, permanent in place."""
    out = []
    if opts["exclude"]:
        out.append(f"exclude {opts['exclude']}")
    if opts["topics"]:
        out.append(f"topics (allow-list) {opts['topics']}")
    if opts["from_s"] is not None or opts["to_s"] is not None:
        out.append(f"window {opts['from_s']}..{opts['to_s']}s")
    return out


def verify_mode(opts: dict) -> str:
    if opts["from_s"] is not None or opts["to_s"] is not None:
        return "any"
    return "kept" if (opts["exclude"] or opts["topics"]) else "all"


def read_bag_start(text: str, where: str) -> int:
    """starting_time (ns since epoch) from a session's metadata.yaml text."""
    try:
        doc = yaml.safe_load(text) or {}
        info = doc["rosbag2_bagfile_information"]
        return int(info["starting_time"]["nanoseconds_since_epoch"])
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise SystemExit(f"bag_export: {where}: no rosbag2_bagfile_information.starting_time — "
                         f"is this a rosbag2 metadata.yaml?")


def session_spec(session_src: str, session_dest: str, opts: dict, bag_start_ns: int | None,
                 topic_names: list[str] | None = None) -> dict:
    """One `ros2 bag convert` output spec (the YAML rosbag2 takes with -o), pure."""
    spec: dict = {"uri": session_dest, "storage_id": "mcap"}
    if opts["preset"] != "none":
        spec["storage_preset_profile"] = opts["preset"]
    spec["include_hidden_topics"] = True
    if opts["topics"] or opts["exclude"]:
        # TopicFilter's regex changes meaning for service/action interfaces
        # (it matches their base name). Resolve the user's full-topic filter
        # once instead, then use exact regular/action topic + service-event lists.
        names = opts["topics"] or topic_names or []
        selected = sorted({name for name in names
                           if not any(re.search(pattern, name) for pattern in opts["exclude"])})
        spec.update(topics=selected, services=selected, all_topics=False,
                    all_services=False, all_actions=False)
    else:
        spec.update(all_topics=True, all_services=True, all_actions=True)
    if opts.get("split_duration_s"):
        spec["max_bagfile_duration"] = int(opts["split_duration_s"])
    if opts.get("max_size_mb"):
        spec["max_bagfile_size"] = int(opts["max_size_mb"]) * 1024 * 1024
    result = {"output_bags": [spec]}
    if opts["from_s"] is not None or opts["to_s"] is not None:
        if bag_start_ns is None:
            raise SystemExit(f"bag_export: {session_src}: from_s/to_s need the session's "
                             f"metadata.yaml (its starting_time is the window's zero)")
        # rosbag2 applies time bounds to the INPUT StorageOptions. Putting them
        # on the output silently converts the entire session.
        input_spec = {"uri": session_src}
        if opts["from_s"] is not None:
            input_spec["start_time_ns"] = bag_start_ns + int(round(opts["from_s"] * 1e9))
        if opts["to_s"] is not None:
            input_spec["end_time_ns"] = bag_start_ns + int(round(opts["to_s"] * 1e9))
        result["input_bags"] = [input_spec]
    return result


def find_sessions(tree: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    """(sessions with metadata.yaml, warnings): every dir under <tree> holding .mcap files."""
    sessions: list[pathlib.Path] = []
    warns: list[str] = []
    if not tree.is_dir():
        return sessions, warns
    for d in sorted(p for p in tree.iterdir() if p.is_dir() and not p.name.startswith(".")):
        bags = [f for f in d.iterdir() if f.name.endswith((".mcap", ".db3", ".mcap.zstd", ".db3.zstd"))]
        if not bags:
            continue
        if not (d / "metadata.yaml").is_file():
            warns.append(f"{d.name}: bag files but no metadata.yaml (the recorder did not close "
                         f"it) — skipped; `ros2 bag reindex {d} mcap` on the vehicle, then redo")
            continue
        sessions.append(d)
    return sessions, warns


def plan(source: str, dest: str, subdir: str, name: str, opts: dict, *, force: bool,
         spec_dir: pathlib.Path, in_place: bool = False
         ) -> tuple[list[tuple[pathlib.Path, dict, pathlib.Path]], str, list[str]]:
    """(jobs, script text, warnings). A job = (session src dir, its spec, the spec file path);
    the script converts each job in turn inside the image, reporting sizes. Pure but for reads.
    in_place: each session converts into <tree>/.rig-rewrite/<session> (the SAME basename, so
    rosbag2 names the files after the session, and the same filesystem, so the swap is a
    rename), is verified by verify.py, then replaces the original."""
    tree = pathlib.Path(source) / subdir / name
    destination_tree = tree if in_place else pathlib.Path(dest) / subdir / name
    work = destination_tree / (REWRITE_DIR if in_place else EXPORT_WORK_DIR)
    sessions, warns = find_sessions(tree)
    metadata_paths = {sess: sess / "metadata.yaml" for sess in sessions}
    if in_place:
        # A crash can leave the source at .orig between the two publication
        # renames. Plan from that metadata, then restore it under the runtime
        # lock before reading/converting. Do not strand it behind count=0.
        for backup in sorted(work.glob("*.orig")):
            original = tree / backup.name.removesuffix(".orig")
            if not original.exists() and (backup / "metadata.yaml").is_file():
                sessions.append(original)
                metadata_paths[original] = backup / "metadata.yaml"
        sessions.sort()
    if not sessions:
        warns.append(f"no sessions under {tree} — nothing to export")
    jobs: list[tuple[pathlib.Path, dict, pathlib.Path]] = []
    for sess in sessions:
        final_dir = destination_tree / sess.name
        out_dir = work / sess.name
        if not in_place and (final_dir / "metadata.yaml").is_file() and not force:
            warns.append(f"{sess.name}: already exported ({final_dir}) — skipped (RIG_EXPORT_FORCE=1 "
                         f"redoes it)")
            continue
        bag_start = None
        if opts["from_s"] is not None or opts["to_s"] is not None:
            bag_start = read_bag_start(metadata_paths[sess].read_text(), str(metadata_paths[sess]))
        info = (yaml.safe_load(metadata_paths[sess].read_text()) or {}).get("rosbag2_bagfile_information", {})
        topic_names = [entry.get("topic_metadata", {}).get("name")
                       for entry in info.get("topics_with_message_count", [])]
        topic_names = [topic for topic in topic_names if isinstance(topic, str)]
        spec = session_spec(str(sess), str(out_dir), opts, bag_start, topic_names)
        jobs.append((sess, spec, spec_dir / f"{sess.name}.yaml"))
    lines = ["#!/usr/bin/env bash",
             f"# rendered by bag_export.py — `ros2 bag convert` per session, {name}: "
             f"preset={opts['preset']}" + (" IN PLACE" if in_place else "")
             + (f" exclude={opts['exclude']}" if opts["exclude"] else "")
             + (f" topics={opts['topics']}" if opts["topics"] else "")
             + (f" window={opts['from_s']}..{opts['to_s']}s" if opts["from_s"] is not None
                or opts["to_s"] is not None else ""),
             "set -o pipefail   # never -u: ROS setup.bash reads unset vars (AMENT_TRACE_SETUP_FILES)",
             ROS_SOURCE,
             "rc=0"]
    q_work, q_tree = shlex.quote(str(work)), shlex.quote(str(destination_tree))
    q_verify = shlex.quote(str(spec_dir / "verify.py"))
    q_provenance = shlex.quote(str(spec_dir / "export_provenance.py"))
    mode = verify_mode(opts)
    q_lock = shlex.quote(str(destination_tree / ".rig-export.lock"))
    lines += ["command -v flock >/dev/null 2>&1 || { echo 'bag_export: runtime needs flock for safe concurrent exports; use fleet-ros' >&2; exit 1; }",
              f"mkdir -p {q_tree} || exit 1",
              f"exec 9>{q_lock} || exit 1",
              "flock -n 9 || { echo 'bag_export: another export is active for this destination/logger; retry after it finishes' >&2; exit 1; }",
              f"mkdir -p {q_work} || exit 1",
              "# Recover a crash between verified publication's two renames.",
              f"for o in {q_work}/*.orig; do [ -e \"$o\" ] || continue; "
              f"s={q_tree}/$(basename \"${{o%.orig}}\"); "
              f"if [ -e \"$s\" ]; then rm -rf \"$o\"; else mv \"$o\" \"$s\"; fi; done"]
    for sess, spec, spec_path in jobs:
        out_dir = spec["output_bags"][0]["uri"]
        q_sess, q_spec, q_out = shlex.quote(str(sess)), shlex.quote(str(spec_path)), shlex.quote(out_dir)
        q_final = shlex.quote(str(destination_tree / sess.name))
        q_orig = shlex.quote(str(work / f"{sess.name}.orig"))
        if not in_place and not force:
            lines += [f"if [ -f {q_final}/metadata.yaml ]; then",
                      f"  echo 'bag_export: {sess.name}: already exported after locking — skipped' >&2",
                      "else"]
        lines += [f"need=$(du -sk {q_sess} | cut -f1)"]
        if in_place:
            lines += [f"have=$(df -Pk {q_tree} | awk 'NR==2{{print $4}}')",
                      "if [ \"${have:-0}\" -lt \"$need\" ]; then",
                      f"  echo \"bag_export: {sess.name}: needs ${{need}} KB free for the rewrite, "
                      "${have:-0} KB available — skipped, original untouched\" >&2; rc=1",
                      "else"]
        q_state = shlex.quote(str(spec_dir / f"{sess.name}.provenance.sqlite"))
        input_args = f"--input-options {q_spec}" if "input_bags" in spec else f"-i {q_sess}"
        info = (yaml.safe_load(metadata_paths[sess].read_text()) or {}).get("rosbag2_bagfile_information", {})
        file_compressed = str(info.get("compression_mode", "")).lower() == "file"
        prepare = f"python3 {q_provenance} prepare {q_sess} {q_spec} {q_state}"
        if file_compressed:
            q_input = shlex.quote(str(work / f"{sess.name}.input"))
            q_input_spec = shlex.quote(str(spec_dir / f"{sess.name}.input.yaml"))
            lines += [f"rm -rf {q_input}"]
            prepare += f" && python3 {q_provenance} stage-input {q_sess} {q_input} {q_spec} {q_input_spec}"
            input_args = f"--input-options {q_input_spec}"
        convert = (f"{prepare} && ros2 bag convert {input_args} -o {q_spec}"
                   f" && python3 {q_provenance} verify {q_out} {q_state}")
        if in_place:
            convert += f" && python3 {q_verify} {q_sess}/metadata.yaml {q_out}/metadata.yaml {mode}"
        lines += [f"rm -rf {q_out}  # only remove staging; preserve a prior exported session",
                  f"if {convert}; then",
                  f"  if {{ [ ! -e {q_final} ] || mv {q_final} {q_orig}; }} && mv {q_out} {q_final}; then",
                  f"    rm -rf {q_orig}",
                  f"    echo \"bag_export: {sess.name}: ${{need}} KB -> $(du -sk {q_final} | cut -f1) KB"
                  + (" (in place)" if in_place else "") + "\" >&2",
                  f"  else [ -e {q_final} ] || {{ [ ! -e {q_orig} ] || mv {q_orig} {q_final}; }}; "
                  f"echo \"bag_export: {sess.name}: publish FAILED — original restored\" >&2; rc=1; fi",
                  f"else echo \"bag_export: {sess.name}: convert/verify FAILED — original untouched\" "
                  f">&2; rm -rf {q_out}; rc=1; fi",
                  f"rm -f {q_state} {q_state}-journal"]
        if file_compressed:
            lines += [f"rm -rf {q_input}", f"rm -f {q_input_spec}"]
        if in_place:
            lines.append("fi")
        if not in_place and not force:
            lines.append("fi")
    lines.append(f"rmdir {q_work} 2>/dev/null || true")
    lines.append("exit $rc")
    return jobs, "\n".join(lines) + "\n", warns


def main(argv: list[str], env: dict | None = None) -> int:
    env = os.environ if env is None else env
    if len(argv) != 2:
        print("usage: bag_export.py <config.yaml> <repo>", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(pathlib.Path(argv[0]).read_text()) or {}
    name = str(env.get("RIG_EXPORT_NAME") or cfg.get("name") or "bag_logger")
    if "/" in name or name.startswith("."):
        raise SystemExit(f"bag_export: RIG_EXPORT_NAME {name!r} is not a plain directory name")
    subdir = str((cfg.get("output") or {}).get("subdir") or "bags")
    source, dest = env.get("RIG_EXPORT_SOURCE", ""), env.get("RIG_EXPORT_DEST", "")
    if not source or not os.path.isdir(source):
        raise SystemExit(f"bag_export: RIG_EXPORT_SOURCE must be the run dir (got {source!r}) — "
                         f"`rig run export` sets it; this verb is not for standalone use")
    if not dest or not os.path.isdir(dest):
        raise SystemExit(f"bag_export: RIG_EXPORT_DEST must be the export dir (got {dest!r})")
    opt_file = env.get("RIG_EXPORT_OPTIONS", "")
    raw = yaml.safe_load(pathlib.Path(opt_file).read_text()) if opt_file else None
    opts = parse_options(raw)
    force = env.get("RIG_EXPORT_FORCE", "") == "1"
    in_place = env.get("RIG_EXPORT_INPLACE", "") == "1"
    if in_place:
        if os.path.realpath(source) != os.path.realpath(dest):
            raise SystemExit("bag_export: in place means RIG_EXPORT_DEST is the run itself")
        drops = lossy_reasons(opts)
        if drops and env.get("RIG_EXPORT_LOSSY", "") != "1":
            raise SystemExit("bag_export: these options DROP data and in place that is permanent: "
                             + "; ".join(drops) + " — `rig run export --in-place --lossy` to "
                             "mean it, or drop them for a lossless recompress")
    # Each launch owns immutable scripts/specs while another container may still
    # be using an earlier render. The runtime lock protects staging/publication.
    spec_dir = pathlib.Path(dest) / ".rig" / "export" / name / uuid.uuid4().hex
    jobs, script, warns = plan(source, dest, subdir, name, opts, force=force, spec_dir=spec_dir,
                               in_place=in_place)
    for w in warns:
        print(f"bag_export: {name}: {w}", file=sys.stderr)
    spec_dir.mkdir(parents=True, exist_ok=True)
    for _, spec, spec_path in jobs:
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
    for helper in ("export_provenance.py", "publication_identity.py"):
        shutil.copyfile(pathlib.Path(__file__).resolve().parent / helper, spec_dir / helper)
    if in_place:
        (spec_dir / "verify.py").write_text(VERIFY_PY)
    script_path = spec_dir / "convert.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)
    profile = env.get("RIG_EXPORT_PROFILE", "")
    print(f"bag_export: {name}{' [' + profile + ']' if profile else ''}: {len(jobs)} session(s) "
          f"to {'rewrite IN PLACE' if in_place else 'convert'}, preset={opts['preset']}",
          file=sys.stderr)
    print(f"{script_path}\t{len(jobs)}\t{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
