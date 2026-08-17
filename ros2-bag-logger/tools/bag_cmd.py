#!/usr/bin/env python3
"""ros2-bag-logger: turn one logger config (YAML) into the ``ros2 bag record`` invocation that
``ros2-bag-logger-up`` hands the container. This SELECTS + PARAMETERIZES a standard tool — it never
records anything itself, and rig stays schema-opaque (it only reads name/service).

``render(cfg, repo)`` writes ``var/run/<name>/record.sh`` (the exact command, captured by ``rig bake``
like any launcher-rendered file) and returns ``<name>\\t<script-path>``; ``build_args(cfg)`` is the pure,
testable core that maps the config to a ``ros2 bag record`` argv.

Record selection (``record.mode``) — topics only; services/actions are separate explicit keys because
on current rosbag2 ``--all`` sweeps in service/action event topics too:
  all      -> ``--all-topics``                 every topic (minus images, if ``exclude_images``)
  allow    -> ``--topics <t> <t> ...``         exactly these (``exclude_images`` is moot, ignored)
  exclude  -> ``--all-topics --exclude-regex`` everything except ``topics`` (+ images, if enabled)
(Positional topic names are GONE from current ``ros2 bag record`` — allow mode must use ``--topics``.
Flag spellings track the fleet-ros distro; a BAG_LOGGER_IMAGE from an older distro won't know them,
but rig doctor already flags distro-mismatched images.)

Runtime control: the recorder node name is pinned (``control.node_name``, default: sanitized ``name``)
so rosbag2's control services sit at stable paths — /<node>/{pause,resume,is_paused,split_bagfile,
stop,record} (+ /<node>/snapshot in snapshot mode). Trigger POLICY (arm/disarm, geofence, ...) stays
outside this service: an external node gates recording through those services.

Compression: mcap output compresses in-band by default (``storage_preset: zstd_fast`` — zstd chunks
INSIDE a standard ``.mcap``; every mcap tool reads it directly). The rosbag2-layer ``compression:``
knob is the sqlite3 path; layering it on a compressing preset double-compresses, so the preset
defaults to ``none`` whenever ``compression:`` is set.

Restart-safety: the output dir carries a runtime UTC stamp (evaluated in-container at start), so a compose
restart writes a NEW bag instead of failing on an existing dir — and ``config`` stays host-independent
(the stamp is a literal ``$(date …)`` in the rendered script, not resolved at bake time).
"""
from __future__ import annotations

import pathlib
import re
import shlex
import sys

import yaml

# Default: drop the cameras' raw image stream (huge over ROS; camera-service records compressed at source).
# Matches `/<ns>/image_raw` and its transport sub-topics (`/compressed`, `/theora`, …); widen for
# image_color / image_rect via `record.exclude_images_regex`.
DEFAULT_IMAGE_EXCLUDE = r".*/image_raw(/.*)?$"

STORAGE_PRESETS = ("none", "fastwrite", "zstd_fast", "zstd_small")

# zenoh.shared_memory: line-oriented patch of the SHIPPED session config (first `enabled: false`
# inside the `shared_memory: {` block -> true; first `pool_size:` there -> pool bytes, when given).
# ZENOH_SESSION_CONFIG_URI REPLACES the default wholesale, so a hand-rolled minimal config would
# silently drop rmw_zenoh's ROS defaults (peer mode, connect to localhost:7447) — patch a copy instead.
AWK_SHM = (r'/shared_memory: \{/ && !d {s=1} '
           r's && /enabled: false/ {sub(/enabled: false/, "enabled: true"); if (pool+0 == 0) {s=0; d=1}} '
           r's && pool+0 > 0 && /pool_size:/ {sub(/pool_size: *[0-9]+/, "pool_size: " pool); s=0; d=1} '
           r'{print}')


def _all_or_names(args: list[str], val, key: str, all_flag: str, list_flag: str) -> None:
    """record.services / record.actions: 'all' (or true) -> the --all-* flag, [names] -> the list flag."""
    if not val:
        return
    if isinstance(val, list):
        args += [list_flag] + [str(v) for v in val]
    elif val is True or str(val).lower() == "all":
        args.append(all_flag)
    else:
        raise SystemExit(f"bag_cmd: record.{key} must be 'all' or a list of names")


def build_args(cfg: dict) -> tuple[str, str, str, list[str], list[str]]:
    """(name, output-subdir, node-name, ros2-bag-record argv WITHOUT -o, warnings). Pure — no I/O."""
    name = str(cfg.get("name") or "bag_logger")
    rec = cfg.get("record") or {}
    out = cfg.get("output") or {}
    ctl = cfg.get("control") or {}
    warns: list[str] = []

    mode = str(rec.get("mode") or "exclude").lower()
    topics = rec.get("topics") or []
    if not isinstance(topics, list):
        raise SystemExit("bag_cmd: record.topics must be a list")
    topics = [str(t) for t in topics]
    exclude_images = bool(rec.get("exclude_images", True))
    img_re = str(rec.get("exclude_images_regex") or DEFAULT_IMAGE_EXCLUDE)

    storage = str(out.get("storage") or "mcap")
    args: list[str] = ["-s", storage]

    comp = str(out.get("compression") or "none").lower()
    preset = out.get("storage_preset")
    if storage == "mcap":
        if preset is None:  # compress by default, but never UNDER a rosbag2-layer compressor
            preset = "none" if comp not in ("none", "") else "zstd_fast"
        preset = str(preset).lower()
        if preset not in STORAGE_PRESETS:
            raise SystemExit(f"bag_cmd: unknown output.storage_preset '{preset}' ({'|'.join(STORAGE_PRESETS)})")
        if preset != "none":
            args += ["--storage-preset-profile", preset]
            if comp not in ("none", ""):
                warns.append(f"output.compression={comp} on top of storage_preset={preset} double-compresses"
                             " (and file mode yields .mcap.zstd, unreadable without decompressing)")
    elif preset:
        warns.append(f"output.storage_preset is mcap-only; ignored for storage={storage}")
    if comp not in ("none", ""):
        cmode, cfmt = comp.split(":", 1) if ":" in comp else ("file", comp)  # "zstd" -> file:zstd
        args += ["--compression-mode", cmode, "--compression-format", cfmt]

    split_s = int(out.get("split_duration_s") or 0)
    split_mb = int(out.get("max_size_mb") or 0)
    if split_s > 0:
        args += ["--max-bag-duration", str(split_s)]
    if split_mb > 0:
        args += ["--max-bag-size", str(split_mb * 1024 * 1024)]
    if int(out.get("max_files") or 0) > 0:  # circular logging: rosbag2 deletes the oldest split
        if split_s <= 0 and split_mb <= 0:
            raise SystemExit("bag_cmd: output.max_files needs a split (split_duration_s or max_size_mb)")
        args += ["--max-bag-files", str(int(out["max_files"]))]
    if int(out.get("max_cache_mb") or 0) > 0:
        args += ["--max-cache-size", str(int(out["max_cache_mb"]) * 1024 * 1024)]

    if mode == "all":
        args.append("--all-topics")
        if topics:
            warns.append("record.mode=all ignores record.topics")
        if exclude_images:
            args += ["--exclude-regex", img_re]
    elif mode == "allow":
        if not topics and not (rec.get("services") or rec.get("actions")):
            raise SystemExit("bag_cmd: record.mode=allow needs record.topics (and/or services/actions)")
        if topics:
            args += ["--topics"] + topics
        if exclude_images:
            warns.append("record.mode=allow records exactly record.topics; exclude_images ignored")
    elif mode == "exclude":
        args.append("--all-topics")
        pats = list(topics) + ([img_re] if exclude_images else [])
        if pats:
            args += ["--exclude-regex", "|".join(f"(?:{p})" for p in pats)]
    else:
        raise SystemExit(f"bag_cmd: unknown record.mode '{mode}' (all|allow|exclude)")

    _all_or_names(args, rec.get("services"), "services", "--all-services", "--services")
    _all_or_names(args, rec.get("actions"), "actions", "--all-actions", "--actions")
    if rec.get("include_hidden_topics"):
        args.append("--include-hidden-topics")
    if rec.get("include_unpublished_topics"):
        args.append("--include-unpublished-topics")
    rtl = rec.get("repeat_transient_local")  # re-write latched topics (/tf_static, …) on split/snapshot
    if rtl:
        if isinstance(rtl, list):
            args += ["--repeat-transient-local"] + [str(v) for v in rtl]
        elif rtl is True:
            args.append("--repeat-all-transient-local")
        elif isinstance(rtl, int):
            args += ["--repeat-all-transient-local", str(rtl)]
        else:
            raise SystemExit("bag_cmd: record.repeat_transient_local must be true, a depth, or a list")

    # Control surface: ALWAYS pin the node name so the rosbag2 services sit at stable, documented paths
    # (/<node>/pause etc.) — that is the whole contract an external trigger node programs against.
    node = str(ctl.get("node_name") or re.sub(r"[^A-Za-z0-9_]", "_", name))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", node):
        raise SystemExit(f"bag_cmd: '{node}' is not a valid ROS node name; set control.node_name")
    args += ["--node-name", node]
    if ctl.get("start_paused"):
        args.append("--start-paused")
    if ctl.get("snapshot"):
        args.append("--snapshot-mode")
    if ctl.get("use_sim_time"):
        args.append("--use-sim-time")

    if cfg.get("services"):
        warns.append("top-level `services:` moved to record.services (service recording is now implemented)")
    return name, str(out.get("subdir") or "bags"), node, args, warns


def zenoh_shm_lines(cfg: dict, warns: list[str]) -> str:
    """record.sh preamble for `zenoh:` (shared_memory / shm_pool_mb) — empty string when off.
    Runtime-guarded on RMW_IMPLEMENTATION (the knob is inert under other RMWs) and skipped when the
    deployment already owns ZENOH_SESSION_CONFIG_URI. An unpatchable config (format drift in a future
    distro) warns rather than half-enabling: cmp catching an unchanged copy means SHM stays OFF."""
    zn = cfg.get("zenoh") or {}
    if not isinstance(zn, dict):
        raise SystemExit("bag_cmd: `zenoh` must be a mapping")
    unknown = set(zn) - {"shared_memory", "shm_pool_mb"}
    if unknown:
        raise SystemExit(f"bag_cmd: unknown zenoh key(s): {', '.join(sorted(unknown))} "
                         "(zenoh.shared_memory, zenoh.shm_pool_mb)")
    pool_mb = int(zn.get("shm_pool_mb") or 0)
    if pool_mb < 0:
        raise SystemExit("bag_cmd: zenoh.shm_pool_mb must be a size in MB")
    if not zn.get("shared_memory"):
        if pool_mb:
            warns.append("zenoh.shm_pool_mb without zenoh.shared_memory: true — ignored")
        return ""
    return (
        "# zenoh.shared_memory: patch a copy of the shipped session config (SHM ships disabled) and\n"
        "# point this session at it. SHM engages per link only where the publisher opted in too AND\n"
        "# the containers share the host IPC namespace (compose sets ipc:host) — otherwise zenoh\n"
        "# falls back to TCP loopback silently.\n"
        'if [ "${RMW_IMPLEMENTATION:-}" = "rmw_zenoh_cpp" ]; then\n'
        '  if [ -n "${ZENOH_SESSION_CONFIG_URI:-}" ]; then\n'
        '    echo "ros2-bag-logger: zenoh.shared_memory skipped — ZENOH_SESSION_CONFIG_URI already set" >&2\n'
        '  else\n'
        '    def="/opt/ros/${ROS_DISTRO:-}/share/rmw_zenoh_cpp/config/DEFAULT_RMW_ZENOH_SESSION_CONFIG.json5"\n'
        '    if [ -f "$def" ] && awk -v pool=' + str(pool_mb * 1024 * 1024) + " '" + AWK_SHM + "'"
        ' "$def" > /tmp/zenoh_session_shm.json5 && ! cmp -s "$def" /tmp/zenoh_session_shm.json5; then\n'
        '      export ZENOH_SESSION_CONFIG_URI=/tmp/zenoh_session_shm.json5\n'
        '      echo "ros2-bag-logger: zenoh shared memory ON (ZENOH_SESSION_CONFIG_URI=$ZENOH_SESSION_CONFIG_URI)" >&2\n'
        '    else\n'
        '      echo "ros2-bag-logger: WARNING could not patch shared_memory in $def — SHM NOT enabled" >&2\n'
        '    fi\n'
        '  fi\n'
        'fi\n'
    )


def render(cfg: dict, repo: pathlib.Path) -> tuple[str, str]:
    """Write var/run/<name>/record.sh and return (name, script-path)."""
    name, subdir, node, args, warns = build_args(cfg)
    shm = zenoh_shm_lines(cfg, warns)
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
        + shm +
        # Run-aware output (ROADMAP §3c): pin the OPEN run at process start — resolve `current` ONCE
        # (a live symlink flip would ENOENT the recorder's next split). No run registry -> flat layout.
        'root="${RIG_BAG_ROOT:-/data}"\n'
        'if [ -e "$root/current" ]; then bags="$(readlink -f "$root/current")/bags"; else bags="$root/bags"; fi\n'
        f'base="$bags/{shlex.quote(name)}"\n'
        f'out="$base/{shlex.quote(name)}_$(date -u +%Y%m%dT%H%M%SZ)"\n'
        'mkdir -p "$base"\n'
        f'echo "ros2-bag-logger: recording ({shlex.quote(subdir)}) -> $out'
        f' (control services: /{node}/pause|resume|split_bagfile|stop)" >&2\n'
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
