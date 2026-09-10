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
  RIG_EXPORT_FORCE    "1": redo a session already exported (its output dir is removed first)

The options (every key optional; unknown keys REFUSE — a typo must not silently keep the data):
  preset: zstd_small        mcap chunk compression for the output (none|fastwrite|zstd_fast|
                            zstd_small; default zstd_small — smallest, the point of an export)
  exclude: [regex, ...]     topics to DROP (joined into one --exclude-regex; e.g. '.*/points$')
  topics: [/exact, ...]     an ALLOW list instead of everything (composes with exclude)
  from_s / to_s: seconds    a time window, counted from EACH session's bag start (its
                            metadata.yaml starting_time — the same zero rig replay --from uses)

A session is a ``<name>_<UTCstamp>/`` dir holding ``metadata.yaml`` + its .mcap files — exactly
what the recorder writes. A dir with bag files but no metadata.yaml (the recorder died mid-run)
is SKIPPED with a warning naming the fix (``ros2 bag reindex`` on the vehicle) — never a guess.
Idempotent: a session whose output already carries a metadata.yaml is skipped unless FORCE.

``plan(...)`` is the pure core (sessions -> specs + the script text); ``main`` writes the specs
and the script under <dest>/.rig/export/<name>/ (visible in-container at the same path, since
the launcher mounts both dirs at their host paths) and prints ``<script>\\t<count>\\t<name>``.
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import sys

import yaml

PRESETS = ("none", "fastwrite", "zstd_fast", "zstd_small")
KNOWN = {"preset", "exclude", "topics", "from_s", "to_s"}
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
        if sec < 0:
            raise SystemExit(f"bag_export: {key} must be >= 0")
        out[key] = sec
    if out["from_s"] is not None and out["to_s"] is not None and out["to_s"] <= out["from_s"]:
        raise SystemExit("bag_export: to_s must be greater than from_s")
    return out


def read_bag_start(text: str, where: str) -> int:
    """starting_time (ns since epoch) from a session's metadata.yaml text."""
    try:
        doc = yaml.safe_load(text) or {}
        info = doc["rosbag2_bagfile_information"]
        return int(info["starting_time"]["nanoseconds_since_epoch"])
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise SystemExit(f"bag_export: {where}: no rosbag2_bagfile_information.starting_time — "
                         f"is this a rosbag2 metadata.yaml?")


def session_spec(session_src: str, session_dest: str, opts: dict, bag_start_ns: int | None) -> dict:
    """One `ros2 bag convert` output spec (the YAML rosbag2 takes with -o), pure."""
    spec: dict = {"uri": session_dest, "storage_id": "mcap"}
    if opts["preset"] != "none":
        spec["storage_preset_profile"] = opts["preset"]
    if opts["topics"]:
        spec["topics"] = list(opts["topics"])
        spec["all_topics"] = False
    else:
        spec["all_topics"] = True
    if opts["exclude"]:
        pats = opts["exclude"]
        spec["exclude_regex"] = pats[0] if len(pats) == 1 else "|".join(f"(?:{p})" for p in pats)
    if opts["from_s"] is not None or opts["to_s"] is not None:
        if bag_start_ns is None:
            raise SystemExit(f"bag_export: {session_src}: from_s/to_s need the session's "
                             f"metadata.yaml (its starting_time is the window's zero)")
        if opts["from_s"] is not None:
            spec["start_time_ns"] = bag_start_ns + int(round(opts["from_s"] * 1e9))
        if opts["to_s"] is not None:
            spec["end_time_ns"] = bag_start_ns + int(round(opts["to_s"] * 1e9))
    return {"output_bags": [spec]}


def find_sessions(tree: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    """(sessions with metadata.yaml, warnings): every dir under <tree> holding .mcap files."""
    sessions: list[pathlib.Path] = []
    warns: list[str] = []
    if not tree.is_dir():
        return sessions, warns
    for d in sorted(p for p in tree.iterdir() if p.is_dir()):
        bags = [f for f in d.iterdir() if f.suffix == ".mcap" or f.suffix == ".db3"]
        if not bags:
            continue
        if not (d / "metadata.yaml").is_file():
            warns.append(f"{d.name}: bag files but no metadata.yaml (the recorder did not close "
                         f"it) — skipped; `ros2 bag reindex {d} mcap` on the vehicle, then redo")
            continue
        sessions.append(d)
    return sessions, warns


def plan(source: str, dest: str, subdir: str, name: str, opts: dict, *, force: bool,
         spec_dir: pathlib.Path) -> tuple[list[tuple[pathlib.Path, dict, pathlib.Path]], str, list[str]]:
    """(jobs, script text, warnings). A job = (session src dir, its spec, the spec file path);
    the script converts each job in turn inside the image, reporting sizes. Pure but for reads."""
    tree = pathlib.Path(source) / subdir / name
    sessions, warns = find_sessions(tree)
    if not sessions:
        warns.append(f"no sessions under {tree} — nothing to export")
    jobs: list[tuple[pathlib.Path, dict, pathlib.Path]] = []
    for sess in sessions:
        out_dir = pathlib.Path(dest) / subdir / name / sess.name
        if (out_dir / "metadata.yaml").is_file() and not force:
            warns.append(f"{sess.name}: already exported ({out_dir}) — skipped (RIG_EXPORT_FORCE=1 "
                         f"redoes it)")
            continue
        bag_start = None
        if opts["from_s"] is not None or opts["to_s"] is not None:
            bag_start = read_bag_start((sess / "metadata.yaml").read_text(), str(sess / "metadata.yaml"))
        spec = session_spec(str(sess), str(out_dir), opts, bag_start)
        jobs.append((sess, spec, spec_dir / f"{sess.name}.yaml"))
    lines = ["#!/usr/bin/env bash",
             f"# rendered by bag_export.py — `ros2 bag convert` per session, {name}: "
             f"preset={opts['preset']}"
             + (f" exclude={opts['exclude']}" if opts["exclude"] else "")
             + (f" topics={opts['topics']}" if opts["topics"] else "")
             + (f" window={opts['from_s']}..{opts['to_s']}s" if opts["from_s"] is not None
                or opts["to_s"] is not None else ""),
             "set -o pipefail   # never -u: ROS setup.bash reads unset vars (AMENT_TRACE_SETUP_FILES)",
             ROS_SOURCE,
             "rc=0"]
    for sess, spec, spec_path in jobs:
        out_dir = spec["output_bags"][0]["uri"]
        q_sess, q_spec, q_out = shlex.quote(str(sess)), shlex.quote(str(spec_path)), shlex.quote(out_dir)
        lines += [f"rm -rf {q_out}  # convert refuses an existing output dir",
                  f"if ros2 bag convert -i {q_sess} -o {q_spec}; then",
                  f"  echo \"bag_export: {sess.name}: $(du -sk {q_sess} | cut -f1) KB -> "
                  f"$(du -sk {q_out} | cut -f1) KB\" >&2",
                  f"else echo \"bag_export: {sess.name}: ros2 bag convert FAILED\" >&2; "
                  f"rm -rf {q_out}; rc=1; fi"]
    lines.append("exit $rc")
    return jobs, "\n".join(lines) + "\n", warns


def main(argv: list[str], env: dict | None = None) -> int:
    env = os.environ if env is None else env
    if len(argv) != 2:
        print("usage: bag_export.py <config.yaml> <repo>", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(pathlib.Path(argv[0]).read_text()) or {}
    name = str(cfg.get("name") or "bag_logger")
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
    spec_dir = pathlib.Path(dest) / ".rig" / "export" / name
    jobs, script, warns = plan(source, dest, subdir, name, opts, force=force, spec_dir=spec_dir)
    for w in warns:
        print(f"bag_export: {name}: {w}", file=sys.stderr)
    spec_dir.mkdir(parents=True, exist_ok=True)
    for _, spec, spec_path in jobs:
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
    script_path = spec_dir / "convert.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)
    profile = env.get("RIG_EXPORT_PROFILE", "")
    print(f"bag_export: {name}{' [' + profile + ']' if profile else ''}: {len(jobs)} session(s) "
          f"to convert, preset={opts['preset']}", file=sys.stderr)
    print(f"{script_path}\t{len(jobs)}\t{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
