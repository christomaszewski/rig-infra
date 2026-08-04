#!/usr/bin/env python3
"""ros1-bag-logger: turn one logger config (YAML) into the ``rosbag record`` invocation that
``ros1-bag-logger-up`` hands the container. SELECTS + PARAMETERIZES a standard tool — same config schema
as the ROS 2 logger, mapped onto ROS 1's ``rosbag record`` flags.

``render(cfg, repo)`` writes ``var/run/<name>/record.sh`` and returns ``<name>\\t<script-path>``;
``build_args(cfg)`` is the pure, testable core.

Record selection (``record.mode``):
  all      -> ``-a``                  every topic (minus images, if ``exclude_images``)
  allow    -> ``<topic> ...``         exactly these (``exclude_images`` is moot, ignored)
  exclude  -> ``-a -x '...'``         everything except ``topics`` (+ images, if enabled)

Restart-safety is native: ``rosbag record -o <prefix>`` appends ``_<date>.bag``, so a restart writes a new
file. ROS 1 needs a roscore (``ROS_MASTER_URI``) — supply one as an infra service; the compose defaults to
localhost. (rig's fleet env is ROS 2-shaped — ROS_DOMAIN_ID/RMW — so the master is handled in-compose.)
"""
from __future__ import annotations

import pathlib
import shlex
import sys

import yaml

DEFAULT_IMAGE_EXCLUDE = r".*/image_raw(/.*)?$"


def build_args(cfg: dict) -> tuple[str, str, list[str], list[str]]:
    """(name, output-subdir, rosbag-record argv WITHOUT -o, warnings). Pure — no I/O."""
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

    args: list[str] = []
    storage = str(out.get("storage") or "bag").lower()
    if storage not in ("bag", ""):
        warns.append(f"ROS 1 bags are .bag only; ignoring output.storage={storage}")
    comp = str(out.get("compression") or "none").lower()
    if comp in ("lz4", "bz2"):
        args.append(f"--{comp}")
    elif comp not in ("none", ""):
        warns.append(f"ROS 1 supports lz4|bz2; ignoring output.compression={comp}")
    dur = int(out.get("split_duration_s") or 0)
    size_mb = int(out.get("max_size_mb") or 0)
    if dur > 0 or size_mb > 0:
        args.append("--split")
        if dur > 0:
            args.append(f"--duration={dur}")       # bare number = seconds
        if size_mb > 0:
            args.append(f"--size={size_mb}")        # rosbag --size is in MB

    if mode == "all":
        args.append("-a")
        if topics:
            warns.append("record.mode=all ignores record.topics")
        if exclude_images:
            args += ["-x", img_re]
    elif mode == "allow":
        if not topics:
            raise SystemExit("bag_cmd: record.mode=allow needs record.topics (the topics to record)")
        args += topics
        if exclude_images:
            warns.append("record.mode=allow records exactly record.topics; exclude_images ignored")
    elif mode == "exclude":
        args.append("-a")
        pats = list(topics) + ([img_re] if exclude_images else [])
        if pats:
            args += ["-x", "|".join(f"(?:{p})" for p in pats)]
    else:
        raise SystemExit(f"bag_cmd: unknown record.mode '{mode}' (all|allow|exclude)")

    if cfg.get("services"):
        warns.append("record of service calls is not implemented yet (the `services:` key is ignored)")
    return name, str(out.get("subdir") or "bags"), args, warns


def render(cfg: dict, repo: pathlib.Path) -> tuple[str, str]:
    """Write var/run/<name>/record.sh and return (name, script-path)."""
    name, subdir, args, warns = build_args(cfg)
    for w in warns:
        sys.stderr.write("ros1-bag-logger: " + w + "\n")
    argv = " ".join(shlex.quote(a) for a in args)
    run_dir = pathlib.Path(repo) / "var" / "run" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    script = run_dir / "record.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"   # bash: ROS `setup.bash` is bash-only (sourcing it under sh/dash errors)
        "set -e\n"
        '[ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ] && '
        '. "/opt/ros/$ROS_DISTRO/setup.bash"\n'
        # Run-aware output (ROADMAP §3c): pin the OPEN run at process start; flat layout when no registry.
        'root="${RIG_BAG_ROOT:-/data}"\n'
        'if [ -e "$root/current" ]; then bags="$(readlink -f "$root/current")/bags"; else bags="$root/bags"; fi\n'
        f'base="$bags/{shlex.quote(name)}"\n'
        'mkdir -p "$base"\n'
        # rosbag -o <prefix> appends _<date>.bag -> a restart writes a new file (no clobber).
        f'echo "ros1-bag-logger: recording ({shlex.quote(subdir)}) -> $base/{shlex.quote(name)}_*.bag" >&2\n'
        f'exec rosbag record {argv} -o "$base/{shlex.quote(name)}"\n'
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
