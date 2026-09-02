#!/usr/bin/env python3
"""ros2-bag-player: turn one player config (YAML) + the rig replay env (§1.1 of
rig-replay-player-handoff.md) into the ``ros2 bag play`` invocation ``ros2-bag-player-up`` hands
the container. This SELECTS + PARAMETERIZES a standard tool — the player never decides what to
play: rig's ``rig replay`` verb (or the standalone config) hands it the set, and rig stays
schema-opaque.

``build_args(cfg, env, ls, read)`` is the pure core (config + env -> host source dir, container
session path, argv, warnings, extras; the injected ``ls`` directory lister and ``read`` file
reader are its only world access — tests pass dict-backed fakes). ``render(cfg, env, repo)``
writes ``var/run/<name>/play.sh`` and prints ``<name>\\t<script>\\t<host-source>\\t…`` — the
launcher exports the third field as ``RIG_REPLAY_SOURCE`` for the compose ``ro`` mount when rig
didn't set it (standalone), and the trailing fields (bag start, resolved window) for the injector.

Selector doctrine (§1.1): ``RIG_REPLAY_TOPICS`` (space-separated allow-list) XOR
``RIG_REPLAY_EXCLUDE`` (one regex); both-set is REFUSED (defense in depth — rig guarantees one).
Either env selector shadows the config's ``play.topics`` (WARN). Allow mode FILTERS the topic list
with ``play.exclude`` before emitting ``--topics`` — an allow flag and an exclude flag are never
passed together, and a selection emptied by the filter is refused (a bare ``--topics`` would play
EVERYTHING). Exclude mode merges ``RIG_REPLAY_EXCLUDE`` + ``play.exclude`` into one alternation.
``--clock`` derives from ``RIG_SIM_TIME`` alone; a ``play.clock`` config key is REFUSED at parse —
clock coherence is one rig-owned token and the incoherent state stays unrepresentable.

Service playback + the call injector (rig-replay-calls-handoff §1, frozen; rig >= v0.2.37):
``RIG_REPLAY_SERVICES`` (space-separated service names, same grammar as TOPICS — rig computes the
with-set's observed ``provides`` minus its observed ``requires``) maps to rosbag2's service
playback; it is REFUSED without a topic selector mode (services replay only within a topic
session) and REFUSED alongside ``RIG_REPLAY_CALLS`` (script mode subsumes verbatim — a scripted
call plus its recorded twin would double-call). Standalone: ``play.services`` allow-list, shadowed
by the env var with a WARN — exactly the topics pattern. ``play.services_source``
(``service`` | ``client``, default ``service``) picks which side's recorded events requests are
reconstructed from — servers under test adopted introspection; their callers may not have.
``RIG_REPLAY_CALLS`` (or a standalone top-level ``calls:`` path — env wins, WARN) names the
call-script YAML; the launcher activates the injector's compose profile on it, and play.sh itself
emits NO service flags in calls mode. Services absent from the bag are tolerated (rig's selector
may name services no caller exercised).

Replay WINDOWS (rig-replay-window-handoff §1, frozen; rig >= v0.2.46): ``RIG_REPLAY_FROM_S`` /
``RIG_REPLAY_TO_S`` — float seconds from BAG START (the session's ``metadata.yaml``
``starting_time``), ``to`` exclusive — map to ``--start-offset <from>`` + ``--playback-duration
<to − from>``. Standalone: ``play.start_offset_s`` / ``play.end_offset_s`` (0/absent = bag end);
each env key shadows a NON-ZERO config value with a WARN, the topics pattern. Validation is
HOST-side against the session's ``metadata.yaml``: ``from >= duration`` REFUSES (the window starts
after the bag ends), ``from >= to`` REFUSES (empty window), ``to > duration`` WARNs and CLAMPS
(fixed-step sweeps hit the bag end naturally; the clamped end is the bag's own, so no end flag is
emitted). The bag start (ns) travels to the launcher for the call injector — the ONE zero every
``t`` counts from — so ``metadata.yaml`` is REQUIRED whenever a window or a call script is in
play (a rosbag2 recording always has one; a bag-less fabricated tree can only play bare). With
``from > 0`` play.sh starts the LATCH PRE-PASS (``tools/latch_restore.py``) in the background
before ``exec ros2 bag play``: rosbag2's ``--start-offset`` skips every transient-local message
recorded before the offset, so the pre-pass republishes each SELECTED latched topic's last
pre-window value with its recorded QoS and stays alive for the session.

Session resolution (§1.3) happens HOST-side at render — the source run is static (unlike
record.sh's live ``current``), so the container path ``/replay/bags/<logger>/<session>`` is baked
into play.sh. ``session: latest`` = the lexically-greatest stamped dir (stamps sort
chronologically); multiple stamped dirs WARN naming the skipped ones. A missing tree / session /
bag file refuses with the exact path it looked at. A split recording dir plays as ONE session
(rosbag2 walks the splits; verified live).

Flag spellings track the fleet-ros distro (lyrical, verified live against rosbag2 there):
``--topics t [t ...]`` (space-delimited, GREEDY — always emitted last, after the positional bag
path), ``-x/--exclude-regex REGEX`` (one regex string, a FULL match against each name — ``tick``
plays ``/toy/tick``, ``.*tick`` excludes it; verified live v1.12.0. Allow-mode ``play.exclude``
filtering searches, the v1.8.0 behavior — anchor patterns to mean the same in both modes;
pre-Iron play spelled the flag ``--exclude``),
``--start-offset SECONDS`` (float ok), ``--clock`` (bare = act as ROS time source; optionally
takes a rate in Hz — we emit it bare), ``-l/--loop``, ``-p/--start-paused``, ``-r RATE``.
Service playback (pinned live on lyrical, v1.10.0): ``--publish-service-requests`` (reconstruct
recorded REQUESTS from the bag's ``…/_service_event`` topics and CALL live servers, instead of
republishing the event topics as plain messages), ``--services s [s ...]`` (space-delimited,
GREEDY like ``--topics`` — emitted just BEFORE the topic selector so the selector stays last and
each greedy list is terminated by the next flag), ``--service-requests-source
{service_introspection,client_introspection}`` (rosbag2's own default is service_introspection —
we emit the flag only for ``client``, the defaults-emit-nothing pattern).
The window's end bound (pinned live on lyrical, v1.12.0): ``--playback-duration SECONDS`` counts
RECORDED time from the (offset) start — independent of ``-r`` and of ``--clock``, composing with
``--start-offset`` as ``[from, from + duration)``; the player exits 0 when it is reached. Its
sibling ``--playback-until-sec`` (an absolute epoch stamp; ``--playback-until-nsec`` for exact
ns) is the same bound spelled absolutely — rosbag2 takes the LATER of the two when both are
given — and stays unused here: the duration form self-documents the window length in play.sh
and needs no epoch arithmetic. Pre-Jazzy rosbag2 lacked the offset+duration composition.
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import sys

import yaml

STAMP_RE = re.compile(r"_\d{8}T\d{6}Z$")  # the logger's runtime UTC stamp suffix
LATCH_READY = "/tmp/latch_restore.ready"   # in-container marker: the pre-pass has published
LATCH_WAIT_S = 60                          # bound on how long play.sh waits for that marker


def _ls(path: str):
    """Sorted entry names, or None when the path is missing or not a directory."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


def _read(path: str):
    """File text, or None when the path is missing or unreadable."""
    try:
        with open(path) as f:
            return f.read()
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


def _seconds(val, where: str, allow_none: bool = False):
    """A non-negative float number of seconds; '' / None -> None when allowed (env absent, config
    key omitted). Refuses text and negatives by name."""
    if val is None or (isinstance(val, str) and not val.strip()):
        if allow_none:
            return None
        return 0.0
    try:
        s = float(val)
    except (TypeError, ValueError):
        raise SystemExit(f"play_cmd: {where} must be a number of seconds (got {val!r})")
    if s < 0:
        raise SystemExit(f"play_cmd: {where} must be >= 0 (got {s:g})")
    return s


def _num(x: float) -> str:
    """Seconds as argv text: shortest form without trailing zeros, 9 significant digits (ns-ish
    precision below 1000 s; `:g` alone would clip a 20-minute offset to 10 ms)."""
    return f"{x:.9g}"


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


def read_bag_bounds(text: str, where: str) -> tuple[int, int]:
    """(starting_time ns since epoch, duration ns) from a session's metadata.yaml text — the two
    numbers the window contract stands on (rig-replay-window-handoff §1.1). Refuses, naming the
    file, when either is missing: a window or an injector without a bag start has no zero."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"play_cmd: {where} is not valid YAML: {exc}")
    info = doc.get("rosbag2_bagfile_information") if isinstance(doc, dict) else None
    try:
        start = int(info["starting_time"]["nanoseconds_since_epoch"])
        duration = int(info["duration"]["nanoseconds"])
    except (TypeError, KeyError, ValueError):
        raise SystemExit(f"play_cmd: {where} has no rosbag2_bagfile_information.starting_time"
                         ".nanoseconds_since_epoch / duration.nanoseconds — not a rosbag2 "
                         "metadata.yaml?")
    if start < 0 or duration < 0:
        raise SystemExit(f"play_cmd: {where} carries a negative starting_time/duration")
    return start, duration


def resolve_window(play: dict, env: dict, warns: list[str]) -> tuple[float, float | None]:
    """(from_s, to_s|None) BEFORE bag validation: each rig env key shadows a non-zero config value
    with a WARN (the topics pattern); `to` of 0/absent means the bag end (None). Pure."""
    cfg_from = _seconds(play.get("start_offset_s"), "play.start_offset_s")
    cfg_to = _seconds(play.get("end_offset_s"), "play.end_offset_s")
    env_from = _seconds(env.get("RIG_REPLAY_FROM_S"), "RIG_REPLAY_FROM_S", allow_none=True)
    env_to = _seconds(env.get("RIG_REPLAY_TO_S"), "RIG_REPLAY_TO_S", allow_none=True)
    if env_from is not None:
        if cfg_from:
            warns.append(f"play.start_offset_s ({cfg_from:g}) is shadowed by RIG_REPLAY_FROM_S "
                         f"({env_from:g}) — rig's window governs")
        from_s = env_from
    else:
        from_s = cfg_from
    if env_to is not None:
        if cfg_to:
            warns.append(f"play.end_offset_s ({cfg_to:g}) is shadowed by RIG_REPLAY_TO_S "
                         f"({env_to:g}) — rig's window governs")
        to_s = env_to or None
    else:
        to_s = cfg_to or None
    return from_s, to_s


def validate_window(from_s: float, to_s: float | None, duration_ns: int,
                    warns: list[str]) -> tuple[float, float | None, bool]:
    """The §1.1 refuse/clamp rules against the bag's real duration: (from_s, to_s|None,
    clamped). `from >= duration` refuses; `to > duration` WARNs and clamps to the bag end (the end
    flag is then dropped — the bag's own end IS the bound); `from >= to` refuses. Pure."""
    duration_s = duration_ns / 1e9
    if from_s > 0 and from_s >= duration_s:
        raise SystemExit(f"play_cmd: window starts after the bag ends — from={from_s:g}s but the "
                         f"recording is {duration_s:.3f}s long")
    clamped = False
    if to_s is not None and to_s > duration_s:
        warns.append(f"window end {to_s:g}s exceeds the recording ({duration_s:.3f}s) — clamped "
                     "to the bag end (fixed-step sweeps hit the end naturally)")
        to_s, clamped = duration_s, True
    if to_s is not None and from_s >= to_s:
        raise SystemExit(f"play_cmd: empty window — from={from_s:g}s must be < to={to_s:g}s")
    return from_s, to_s, clamped


def build_args(cfg: dict, env: dict, ls=_ls, read=_read) -> tuple[str, str, str, list[str],
                                                                   list[str], dict]:
    """(name, host-source-dir, container-session-path, play argv AFTER the path, warnings,
    extras). `extras` carries what the LAUNCHER / play.sh (not the play argv) consume: `calls`
    (the resolved call-script host path, empty when calls mode is off — gates the injector's
    compose profile), `services_source` (`service`|`client` — also wired to export-calls),
    `bag_start_ns` (the session's starting_time, None when metadata.yaml was not needed and not
    read), `from_s` / `to_s` (the resolved, validated window; `to_s` None = the bag end) and
    `latch` (the pre-pass argv after the session path, empty when `from_s == 0`). Pure modulo the
    injected `ls` / `read`."""
    name = str(cfg.get("name") or "bag_player")
    src = _strict(cfg.get("source"), "source", {"run", "logger", "session"})
    play = _strict(cfg.get("play"), "play",
                   {"topics", "exclude", "rate", "loop", "start_offset_s", "end_offset_s",
                    "start_paused", "services", "services_source",
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
    host_session = os.path.join(source, "bags", logger, session)

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

    # --- service playback XOR the call injector (rig-replay-calls-handoff §1.1–§1.2) ----------
    svc_env = str(env.get("RIG_REPLAY_SERVICES") or "").split()
    calls_env = str(env.get("RIG_REPLAY_CALLS") or "").strip()
    cfg_services = play.get("services") or []
    if not isinstance(cfg_services, list):
        raise SystemExit("play_cmd: play.services must be a list of service names")
    cfg_services = [str(s) for s in cfg_services]
    src_mode = str(play.get("services_source") or "service")
    if src_mode not in ("service", "client"):
        raise SystemExit(f"play_cmd: play.services_source must be `service` or `client` (got "
                         f"{src_mode!r}) — which side's recorded events requests are "
                         "reconstructed from")
    calls_cfg = cfg.get("calls")
    if calls_cfg is not None and not isinstance(calls_cfg, str):
        raise SystemExit("play_cmd: `calls` must be a string — the call-script YAML's absolute "
                         "host path")
    calls_cfg = str(calls_cfg or "").strip()

    if svc_env and calls_env:
        raise SystemExit("play_cmd: RIG_REPLAY_SERVICES and RIG_REPLAY_CALLS are both set — "
                         "script mode subsumes verbatim playback (a scripted call plus its "
                         "recorded twin would double-call); rig never sets both, refusing "
                         "rather than guessing")

    calls, services = "", []
    if calls_env:
        calls = calls_env
        if calls_cfg:
            warns.append("`calls` config path is shadowed by RIG_REPLAY_CALLS (rig's script "
                         "governs)")
        if cfg_services:
            warns.append("play.services is suppressed under RIG_REPLAY_CALLS (script mode "
                         "subsumes verbatim playback — no service flags are emitted)")
    elif svc_env:
        services = svc_env
        if not t_env and not x_env:
            raise SystemExit("play_cmd: RIG_REPLAY_SERVICES without a topic selector mode — "
                             "services replay only within a topic session; rig always pairs it "
                             "with RIG_REPLAY_TOPICS or RIG_REPLAY_EXCLUDE")
        if cfg_services:
            warns.append("play.services is shadowed by RIG_REPLAY_SERVICES (rig's selection "
                         "governs)")
        if calls_cfg:
            warns.append("`calls` config path is ignored under RIG_REPLAY_SERVICES (rig chose "
                         "verbatim mode — the injector is not activated)")
    else:
        if calls_cfg and cfg_services:
            raise SystemExit("play_cmd: `calls` and play.services are both set — script mode "
                             "subsumes verbatim playback (double-call discipline); drop one")
        calls, services = calls_cfg, cfg_services
    if calls and not os.path.isabs(calls):
        raise SystemExit(f"play_cmd: the call script must be an absolute host path (got "
                         f"{calls!r}) — it is bind-mounted read-only into the injector")

    # --- the window (rig-replay-window-handoff §1.1) + the bag start for the injector ----------
    from_s, to_s = resolve_window(play, env, warns)
    bag_start_ns, clamped = None, False
    if from_s > 0 or to_s is not None or calls:
        meta_path = os.path.join(host_session, "metadata.yaml")
        text = read(meta_path)
        if text is None:
            what = "a window" if (from_s > 0 or to_s is not None) else "the call injector"
            raise SystemExit(f"play_cmd: {what} needs the session's metadata.yaml (bag start + "
                             f"duration — the ONE zero every `t` counts from) but none is "
                             f"readable at {meta_path}")
        bag_start_ns, duration_ns = read_bag_bounds(text, meta_path)
        from_s, to_s, clamped = validate_window(from_s, to_s, duration_ns, warns)

    args: list[str] = []
    try:  # `or`-defaulting would turn an explicit rate: 0 into 1.0 instead of a refusal
        rate = 1.0 if play.get("rate") is None else float(play.get("rate"))
    except (TypeError, ValueError):
        raise SystemExit("play_cmd: play.rate must be a number")
    if rate <= 0:
        raise SystemExit("play_cmd: play.rate must be > 0")
    if rate != 1.0:
        args += ["-r", f"{rate:g}"]
    if play.get("loop"):
        args.append("--loop")
        warns.append('play.loop: true — with restart: "no" the bag wraps forever and `down` is '
                     "the only exit (soak testing, not fidelity: /clock jumps backwards at the "
                     "wrap" + (", and the window bounds every lap" if to_s is not None else "")
                     + ")")
    if from_s > 0:
        args += ["--start-offset", _num(from_s)]
    if to_s is not None and not clamped:
        # RECORDED seconds from the (offset) start — pinned live on lyrical, see the docstring.
        # A clamped `to` IS the bag end: no flag, the bag ends by itself.
        args += ["--playback-duration", _num(to_s - from_s)]
    if play.get("start_paused"):
        args.append("--start-paused")
    if str(env.get("RIG_SIM_TIME") or "") == "1":
        args.append("--clock")

    # Verbatim service playback, just BEFORE the topic selector: --services is greedy like
    # --topics, so the next flag terminates it and the selector stays last. Requests come from
    # server-side events unless the config says client (rosbag2's own default is
    # service_introspection — the flag is emitted only for the non-default).
    if services:
        args.append("--publish-service-requests")
        if src_mode == "client":
            args += ["--service-requests-source", "client_introspection"]
        args += ["--services"] + services

    # Selector LAST: --topics is greedy (nargs+), nothing may follow it. The SAME selector
    # argv drives the latch pre-pass, so it can never publish a topic the main play would not.
    selector: list[str] = []
    if allow:
        kept = [t for t in allow if not any(re.search(p, t) for p in cfg_excl)]
        dropped = [t for t in allow if t not in kept]
        if dropped:
            warns.append("play.exclude filtered from the allow-list: " + " ".join(dropped))
        if not kept:
            raise SystemExit("play_cmd: topic selection is empty after play.exclude filtering — "
                             "refusing (a bare `ros2 bag play` would play EVERYTHING)")
        selector = ["--topics"] + kept
    else:
        pats = ([x_env] if x_env else []) + cfg_excl
        if pats:
            selector = ["--exclude-regex", "|".join(f"(?:{p})" for p in pats)]
    args += selector

    latch = (["--from", _num(from_s)] + selector) if from_s > 0 else []
    return name, source, container_path, args, warns, {
        "calls": calls, "services_source": src_mode, "bag_start_ns": bag_start_ns,
        "from_s": from_s, "to_s": to_s, "latch": latch}


def play_script(path: str, args: list[str], latch: list[str]) -> str:
    """play.sh text: source ROS, (windowed) start the latch pre-pass and wait — bounded — for it
    to have published, then exec `ros2 bag play`. Everything is static: no runtime stamps, playback
    writes nothing. The pre-pass wait exists so the main play can never overtake it: an in-window
    latched sample published by the player, then clobbered by the pre-pass's OLDER pre-window
    value, would leave subscribers holding stale state."""
    argv = " ".join(shlex.quote(a) for a in args)
    lines = [
        "#!/usr/bin/env bash",   # bash: ROS `setup.bash` is bash-only
        "set -e",
        "# Source ROS if the image's entrypoint didn't (we run as `command:`, so usually it did).",
        '[ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ] && '
        '. "/opt/ros/$ROS_DISTRO/setup.bash"',
        f'echo "ros2-bag-player: playing {shlex.quote(path)} (exit 0 = the bag ended — a finished'
        ' replay, not a failure)" >&2',
    ]
    if latch:
        latch_argv = " ".join(shlex.quote(a) for a in latch)
        lines += [
            "# Latch pre-pass (rig-replay-window-handoff §1.2): --start-offset skips every",
            "# transient-local message recorded before the offset, so restore each SELECTED latched",
            "# topic's last pre-window value first — from a node that stays alive for the session",
            "# (it dies with this container). The msgs overlay's setup.bash makes source-built",
            "# overlay types importable in Python (the export_calls precedent); no-op without it.",
            "[ -f /opt/fleet-msgs/setup.bash ] && . /opt/fleet-msgs/setup.bash",
            f"rm -f {LATCH_READY}",
            f"python3 /latch_restore.py {shlex.quote(path)} {latch_argv} &",
            "LATCH_PID=$!",
            f"for _ in $(seq 1 {LATCH_WAIT_S * 10}); do",
            f"  [ -f {LATCH_READY} ] && break",
            '  kill -0 "$LATCH_PID" 2>/dev/null || break',
            "  sleep 0.1",
            "done",
            f'[ -f {LATCH_READY} ] || echo "ros2-bag-player: WARN: the latch pre-pass did not '
            f'report within {LATCH_WAIT_S}s (crashed, or a very large bag) — starting play anyway;'
            ' latched topics recorded before the window may be missing" >&2',
        ]
    lines.append(f"exec ros2 bag play {shlex.quote(path)}{' ' + argv if argv else ''}")
    return "\n".join(lines) + "\n"


def render(cfg: dict, env: dict, repo: pathlib.Path) -> tuple[str, ...]:
    """Write var/run/<name>/play.sh and return (name, script-path, host-source-dir,
    container-session-path, services-source, calls-path-or-empty, bag-start-ns-or-empty,
    from-s-or-empty, to-s-or-empty). Everything is resolved here at render time — the script is
    static (no runtime stamps: playback writes nothing), captured by `rig bake` like any
    launcher-rendered file."""
    name, source, path, args, warns, extras = build_args(cfg, env)
    for w in warns:
        sys.stderr.write("ros2-bag-player: " + w + "\n")
    run_dir = pathlib.Path(repo) / "var" / "run" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    script = run_dir / "play.sh"
    script.write_text(play_script(path, args, extras["latch"]))
    script.chmod(0o755)
    return (name, str(script), source, path, extras["services_source"], extras["calls"],
            "" if extras["bag_start_ns"] is None else str(extras["bag_start_ns"]),
            _num(extras["from_s"]) if extras["from_s"] > 0 else "",
            "" if extras["to_s"] is None else _num(extras["to_s"]))


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: play_cmd.py <config.yaml> <repo-dir>\n")
        return 2
    cfg = yaml.safe_load(open(sys.argv[1])) or {}
    # Tab-separated for the launcher's hand-split; trailing fields may be empty.
    print("\t".join(render(cfg, dict(os.environ), pathlib.Path(sys.argv[2]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
