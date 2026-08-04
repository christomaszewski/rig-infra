#!/usr/bin/env python3
"""ros2-bag-logger: turn one logger config (YAML) into the ``ros2 bag record`` invocation that
``ros2-bag-logger-up`` hands the container. This SELECTS + PARAMETERIZES a standard tool — it never
records anything itself, and rig stays schema-opaque (it only reads name/service).

``render(cfg, repo)`` writes ``var/run/<name>/record.sh`` (the exact command, captured by ``rig bake``
like any launcher-rendered file) and returns ``<name>\\t<script-path>``; ``build_args(cfg)`` is the pure,
testable core that maps the config to a ``ros2 bag record`` argv.

Record selection (``record.mode``):
  all      -> ``--all``                        every topic (minus images, if ``exclude_images``)
  allow    -> ``<topic> <topic> ...``          exactly these (``exclude_images`` is moot, ignored)
  exclude  -> ``--all --exclude-regex '...'``  everything except ``topics`` (+ images, if enabled)

Restart-safety: the output dir carries a runtime UTC stamp (evaluated in-container at start), so a compose
restart writes a NEW bag instead of failing on an existing dir — and ``config`` stays host-independent
(the stamp is a literal ``$(date …)`` in the rendered script, not resolved at bake time).
"""
from __future__ import annotations

import pathlib
import shlex
import sys

import yaml

# Default: drop the cameras' raw image stream (huge over ROS; camera-service records compressed at source).
# Matches `/<ns>/image_raw` and its transport sub-topics (`/compressed`, `/theora`, …); widen for
# image_color / image_rect via `record.exclude_images_regex`.
DEFAULT_IMAGE_EXCLUDE = r".*/image_raw(/.*)?$"


def build_args(cfg: dict) -> tuple[str, str, list[str], list[str]]:
    """(name, output-subdir, ros2-bag-record argv WITHOUT -o, warnings). Pure — no I/O."""
    name = str(cfg.get("name") or "bag_logger")
    rec = cfg.get("record") or {}
    out = cfg.get("output") or {}
    warns: list[str] = []

    mode = str(rec.get("mode") or "exclude").lower()
    topics = rec.get("topics") or []
    if not isinstance(topics, list):
        raise SystemExit("bag_cmd: record.topics must be a list")
    topics = [str(t) for t in topics]
    exclude_images = bool(rec.get("exclude_images", True))
    img_re = str(rec.get("exclude_images_regex") or DEFAULT_IMAGE_EXCLUDE)

    args: list[str] = ["-s", str(out.get("storage") or "mcap")]
    comp = str(out.get("compression") or "none").lower()
    if comp not in ("none", ""):
        cmode, cfmt = comp.split(":", 1) if ":" in comp else ("file", comp)  # "zstd" -> file:zstd
        args += ["--compression-mode", cmode, "--compression-format", cfmt]
    if int(out.get("split_duration_s") or 0) > 0:
        args += ["--max-bag-duration", str(int(out["split_duration_s"]))]
    if int(out.get("max_size_mb") or 0) > 0:
        args += ["--max-bag-size", str(int(out["max_size_mb"]) * 1024 * 1024)]

    if mode == "all":
        args.append("--all")
        if topics:
            warns.append("record.mode=all ignores record.topics")
        if exclude_images:
            args += ["--exclude-regex", img_re]   # NOTE: pre-Iron rosbag2 spells this `--exclude`
    elif mode == "allow":
        if not topics:
            raise SystemExit("bag_cmd: record.mode=allow needs record.topics (the topics to record)")
        args += topics
        if exclude_images:
            warns.append("record.mode=allow records exactly record.topics; exclude_images ignored")
    elif mode == "exclude":
        args.append("--all")
        pats = list(topics) + ([img_re] if exclude_images else [])
        if pats:
            args += ["--exclude-regex", "|".join(f"(?:{p})" for p in pats)]
    else:
        raise SystemExit(f"bag_cmd: unknown record.mode '{mode}' (all|allow|exclude)")

    if cfg.get("services"):  # forward-looking knob; rosbag2 service recording is a later addition
        warns.append("record of service calls is not implemented yet (the `services:` key is ignored)")
    return name, str(out.get("subdir") or "bags"), args, warns


def render(cfg: dict, repo: pathlib.Path) -> tuple[str, str]:
    """Write var/run/<name>/record.sh and return (name, script-path)."""
    name, subdir, args, warns = build_args(cfg)
    for w in warns:
        sys.stderr.write("ros2-bag-logger: " + w + "\n")
    argv = " ".join(shlex.quote(a) for a in args)
    run_dir = pathlib.Path(repo) / "var" / "run" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    script = run_dir / "record.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"   # bash: ROS `setup.bash` is bash-only (sourcing it under sh/dash errors)
        "set -e\n"
        "# Source ROS if the image's entrypoint didn't (we run as `command:`, so usually it did).\n"
        '[ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ] && '
        '. "/opt/ros/$ROS_DISTRO/setup.bash"\n'
        # Run-aware output (ROADMAP §3c): pin the OPEN run at process start — resolve `current` ONCE
        # (a live symlink flip would ENOENT the recorder's next split). No run registry -> flat layout.
        'root="${RIG_BAG_ROOT:-/data}"\n'
        'if [ -e "$root/current" ]; then bags="$(readlink -f "$root/current")/bags"; else bags="$root/bags"; fi\n'
        f'base="$bags/{shlex.quote(name)}"\n'
        f'out="$base/{shlex.quote(name)}_$(date -u +%Y%m%dT%H%M%SZ)"\n'
        'mkdir -p "$base"\n'
        f'echo "ros2-bag-logger: recording ({shlex.quote(subdir)}) -> $out" >&2\n'
        f'exec ros2 bag record {argv} -o "$out"\n'
    )
    script.chmod(0o755)
    return name, str(script)


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: bag_cmd.py <config.yaml> <repo-dir>\n")
        return 2
    cfg = yaml.safe_load(open(sys.argv[1])) or {}
    name, script = render(cfg, pathlib.Path(sys.argv[2]))
    print(name + "\t" + script)
    return 0


if __name__ == "__main__":
    sys.exit(main())
