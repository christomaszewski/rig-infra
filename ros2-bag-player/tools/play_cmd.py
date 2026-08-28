#!/usr/bin/env python3
"""ros2-bag-player: turn one player config (YAML) + the rig replay env (§1.1 of
rig-replay-player-handoff.md) into the ``ros2 bag play`` invocation ``ros2-bag-player-up`` hands
the container. This SELECTS + PARAMETERIZES a standard tool — the player never decides what to
play: rig's ``rig replay`` verb (or the standalone config) hands it the set, and rig stays
schema-opaque.

``build_args(cfg, env, ls)`` is the pure core (config + env -> host source dir, container session
path, argv, warnings; the injected ``ls`` directory lister is its only world access — tests pass a
dict-backed fake). ``render(cfg, env, repo)`` writes ``var/run/<name>/play.sh`` and prints
``<name>\\t<script>\\t<host-source>`` — the launcher exports the third field as
``RIG_REPLAY_SOURCE`` for the compose ``ro`` mount when rig didn't set it (standalone).

Selector doctrine (§1.1): ``RIG_REPLAY_TOPICS`` (space-separated allow-list) XOR
``RIG_REPLAY_EXCLUDE`` (one regex); both-set is REFUSED (defense in depth — rig guarantees one).
Either env selector shadows the config's ``play.topics`` (WARN). Allow mode FILTERS the topic list
with ``play.exclude`` before emitting ``--topics`` — an allow flag and an exclude flag are never
passed together, and a selection emptied by the filter is refused (a bare ``--topics`` would play
EVERYTHING). Exclude mode merges ``RIG_REPLAY_EXCLUDE`` + ``play.exclude`` into one alternation.
``--clock`` derives from ``RIG_SIM_TIME`` alone; a ``play.clock`` config key is REFUSED at parse —
clock coherence is one rig-owned token and the incoherent state stays unrepresentable.

Session resolution (§1.3) happens HOST-side at render — the source run is static (unlike
record.sh's live ``current``), so the container path ``/replay/bags/<logger>/<session>`` is baked
into play.sh. ``session: latest`` = the lexically-greatest stamped dir (stamps sort
chronologically); multiple stamped dirs WARN naming the skipped ones. A missing tree / session /
bag file refuses with the exact path it looked at. A split recording dir plays as ONE session
(rosbag2 walks the splits; verified live).

Flag spellings track the fleet-ros distro (lyrical, verified live against rosbag2 there):
``--topics t [t ...]`` (space-delimited, GREEDY — always emitted last, after the positional bag
path), ``-x/--exclude-regex REGEX`` (one regex string; pre-Iron play spelled it ``--exclude``),
``--start-offset SECONDS`` (float ok), ``--clock`` (bare = act as ROS time source; optionally
takes a rate in Hz — we emit it bare), ``-l/--loop``, ``-p/--start-paused``, ``-r RATE``.
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import sys

import yaml

STAMP_RE = re.compile(r"_\d{8}T\d{6}Z$")  # the logger's runtime UTC stamp suffix


def _ls(path: str):
    """Sorted entry names, or None when the path is missing or not a directory."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


def _strict(block: dict | None, name: str, known: set[str]) -> dict:
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise SystemExit(f"play_cmd: `{name}` must be a mapping")
    unknown = set(block) - known
    if unknown:
        raise SystemExit(f"play_cmd: unknown {name} key(s): {', '.join(sorted(unknown))} "
                         f"({', '.join(name + '.' + k for k in sorted(known))})")
    return block


def _regexes(pats, key: str) -> list[str]:
    if not isinstance(pats, list):
        raise SystemExit(f"play_cmd: {key} must be a list of regexes")
    pats = [str(p) for p in pats]
    for p in pats:
        try:
            re.compile(p)
        except re.error as exc:
            raise SystemExit(f"play_cmd: {key} regex {p!r} does not compile: {exc}")
    return pats


def resolve_session(source: str, logger: str, session: str, ls, warns: list[str]) -> str:
    """The stamped recording dir name under <source>/bags/<logger>/ (§1.3) — or a refusal naming
    the exact path that came up empty. `latest` = lexically-greatest stamped dir; an explicit
    session may be the full dir name or just the UTC stamp (the logger names dirs
    <logger>_<stamp>). The chosen dir must hold at least one bag file (*.mcap / *.db3) —
    metadata.yaml alone is not a recording."""
    tree = os.path.join(source, "bags", logger)
    entries = ls(tree)
    if entries is None:
        raise SystemExit(f"play_cmd: no logger tree at {tree} "
                         "(source.logger names the bags/<logger>/ dir to play)")
    if session == "latest":
        stamped = [e for e in entries if STAMP_RE.search(e) and ls(os.path.join(tree, e)) is not None]
        if not stamped:
            raise SystemExit(f"play_cmd: no stamped recording dirs under {tree}")
        chosen = max(stamped)
        if len(stamped) > 1:
            skipped = ", ".join(e for e in stamped if e != chosen)
            warns.append(f"multiple sessions under {tree}: playing {chosen}, skipping {skipped} "
                         "(each is one recording — set source.session to play another)")
    else:
        cands = [str(session), f"{logger}_{session}"]
        chosen = next((c for c in cands if ls(os.path.join(tree, c)) is not None), None)
        if chosen is None:
            looked = " or ".join(os.path.join(tree, c) for c in cands)
            raise SystemExit(f"play_cmd: session {session!r} not found — looked at {looked}")
    files = ls(os.path.join(tree, chosen)) or []
    if not any(f.endswith((".mcap", ".db3")) for f in files):
        raise SystemExit(f"play_cmd: no bag files (*.mcap/*.db3) in {os.path.join(tree, chosen)}")
    return chosen


def build_args(cfg: dict, env: dict, ls=_ls) -> tuple[str, str, str, list[str], list[str]]:
    """(name, host-source-dir, container-session-path, play argv AFTER the path, warnings).
    Pure modulo the injected `ls`."""
    name = str(cfg.get("name") or "bag_player")
    src = _strict(cfg.get("source"), "source", {"run", "logger", "session"})
    play = _strict(cfg.get("play"), "play",
                   {"topics", "exclude", "rate", "loop", "start_offset_s", "start_paused",
                    "clock"})  # `clock` recognized only to refuse it by name below
    warns: list[str] = []

    if "clock" in play:
        raise SystemExit("play_cmd: play.clock is refused — the sim clock is rig-owned, one token "
                         "(RIG_SIM_TIME) with two consumers (--clock here, use_sim_time in the "
                         "services under test); a config knob could split them")

    # --- the source run + session (§1.1 env wins; §1.3 host-side resolution) -------------------
    source = str(env.get("RIG_REPLAY_SOURCE") or "").strip() or str(src.get("run") or "").strip()
    if not source:
        raise SystemExit("play_cmd: no source run — rig exports RIG_REPLAY_SOURCE; standalone use "
                         "sets source.run to the run dir's absolute host path")
    if not os.path.isabs(source):
        raise SystemExit(f"play_cmd: source run must be an absolute host path (got {source!r})")
    logger = str(src.get("logger") or "bag_logger")
    session = resolve_session(source, logger, str(src.get("session") or "latest"), ls, warns)
    container_path = f"/replay/bags/{logger}/{session}"

    # --- the topic selection (§1.1: env XOR env; config governs standalone) --------------------
    t_env = str(env.get("RIG_REPLAY_TOPICS") or "").split()
    x_env = str(env.get("RIG_REPLAY_EXCLUDE") or "").strip()
    if t_env and x_env:
        raise SystemExit("play_cmd: RIG_REPLAY_TOPICS and RIG_REPLAY_EXCLUDE are both set — rig "
                         "guarantees exactly one; refusing rather than guessing which to honor")
    cfg_topics = play.get("topics") or []
    if not isinstance(cfg_topics, list):
        raise SystemExit("play_cmd: play.topics must be a list of topic names")
    cfg_topics = [str(t) for t in cfg_topics]
    cfg_excl = _regexes(play.get("exclude") or [], "play.exclude")
    if x_env:
        _regexes([x_env], "RIG_REPLAY_EXCLUDE")

    if t_env and cfg_topics:
        warns.append("play.topics is shadowed by RIG_REPLAY_TOPICS (rig's selection governs)")
    if x_env and cfg_topics:
        warns.append("play.topics is ignored under RIG_REPLAY_EXCLUDE (rig chose exclude mode)")
    allow = t_env or ([] if x_env else cfg_topics)

    args: list[str] = []
    try:  # `or`-defaulting would turn an explicit rate: 0 into 1.0 instead of a refusal
        rate = 1.0 if play.get("rate") is None else float(play.get("rate"))
        offset = float(play.get("start_offset_s") or 0)
    except (TypeError, ValueError):
        raise SystemExit("play_cmd: play.rate / play.start_offset_s must be numbers")
    if rate <= 0:
        raise SystemExit("play_cmd: play.rate must be > 0")
    if rate != 1.0:
        args += ["-r", f"{rate:g}"]
    if play.get("loop"):
        args.append("--loop")
        warns.append('play.loop: true — with restart: "no" the bag wraps forever and `down` is '
                     "the only exit (soak testing, not fidelity: /clock jumps backwards at the "
                     "wrap)")
    if offset < 0:
        raise SystemExit("play_cmd: play.start_offset_s must be >= 0")
    if offset > 0:
        args += ["--start-offset", f"{offset:g}"]
    if play.get("start_paused"):
        args.append("--start-paused")
    if str(env.get("RIG_SIM_TIME") or "") == "1":
        args.append("--clock")

    # Selector LAST: --topics is greedy (nargs+), nothing may follow it.
    if allow:
        kept = [t for t in allow if not any(re.search(p, t) for p in cfg_excl)]
        dropped = [t for t in allow if t not in kept]
        if dropped:
            warns.append("play.exclude filtered from the allow-list: " + " ".join(dropped))
        if not kept:
            raise SystemExit("play_cmd: topic selection is empty after play.exclude filtering — "
                             "refusing (a bare `ros2 bag play` would play EVERYTHING)")
        args += ["--topics"] + kept
    else:
        pats = ([x_env] if x_env else []) + cfg_excl
        if pats:
            args += ["--exclude-regex", "|".join(f"(?:{p})" for p in pats)]

    return name, source, container_path, args, warns


def render(cfg: dict, env: dict, repo: pathlib.Path) -> tuple[str, str, str]:
    """Write var/run/<name>/play.sh and return (name, script-path, host-source-dir). Everything is
    resolved here at render time — the script is static (no runtime stamps: playback writes
    nothing), captured by `rig bake` like any launcher-rendered file."""
    name, source, path, args, warns = build_args(cfg, env)
    for w in warns:
        sys.stderr.write("ros2-bag-player: " + w + "\n")
    argv = " ".join(shlex.quote(a) for a in args)
    run_dir = pathlib.Path(repo) / "var" / "run" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    script = run_dir / "play.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"   # bash: ROS `setup.bash` is bash-only
        "set -e\n"
        "# Source ROS if the image's entrypoint didn't (we run as `command:`, so usually it did).\n"
        '[ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ] && '
        '. "/opt/ros/$ROS_DISTRO/setup.bash"\n'
        f'echo "ros2-bag-player: playing {shlex.quote(path)} (exit 0 = the bag ended — a finished'
        ' replay, not a failure)" >&2\n'
        f"exec ros2 bag play {shlex.quote(path)}{' ' + argv if argv else ''}\n"
    )
    script.chmod(0o755)
    return name, str(script), source


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: play_cmd.py <config.yaml> <repo-dir>\n")
        return 2
    cfg = yaml.safe_load(open(sys.argv[1])) or {}
    name, script, source = render(cfg, dict(os.environ), pathlib.Path(sys.argv[2]))
    print(name + "\t" + script + "\t" + source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
