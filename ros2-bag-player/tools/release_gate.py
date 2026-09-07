#!/usr/bin/env python3
"""ros2-bag-player release gate: resume a ``--start-paused`` player at ONE shared instant.

A replay session with per-sensor sources (rig-sensor-replay-plan §5; rig >= v0.2.52) has N
producers — camera-service instances fed from their own recordings, and this bag player — that
must start on one timeline. rig hands every launcher in the up-set the same release instant,
``RIG_REPLAY_START_AT_UNIX_S`` (unix seconds, three decimals: the up's start + ``--start-delay``,
default 20 s). Every source comes up paused holding its first frame and resumes itself there;
the player does the same: play_cmd renders ``--start-paused`` whenever the var is set (whatever
the config's ``play.start_paused`` says), and play.sh starts THIS companion in the background
before ``exec ros2 bag play`` (same container, dies with it). The companion:

  1. logs ``release gate in N.Ns (<instant>)`` — or that the instant is already past;
  2. waits for the player's services to exist (``/rosbag2_player/resume`` + ``is_paused``,
     pinned on lyrical; rosbag2 0.33 accepts no namespace for the player node) AND for the
     player to report itself paused — the hold is in place, the seek to ``--start-offset`` has
     happened (rosbag2 seeks before it honours the pause, so a windowed replay releases AT
     ``from``, verified live);
  3. holds until the instant on the WALL clock (skew between containers on one host = clock
     agreement, milliseconds), then calls ``rosbag2_interfaces/srv/Resume`` — retried until the
     player reports not-paused — and logs ``released`` with the wall time against the instant.
     An instant already past when the player is ready resumes at once with a WARN naming the
     lateness (rig warns on its side when the up outlasts the gate — pass a larger
     ``--start-delay``).

Split like the siblings: a pure core (instant parsing, the hold plan, the log lines, and the
whole wait/hold/release loop with its world — service discovery, the two calls, the clock —
injected, so every branch is unit-tested in ``../tests/`` without ROS) and a thin rclpy shell
that only builds the clients. rclpy imports lazily.
"""
from __future__ import annotations

import math
import os
import re
import sys
import time

PLAYER_NODE = "/rosbag2_player"          # lyrical's `ros2 bag play` node — no namespace accepted
RESUME_SERVICE = PLAYER_NODE + "/resume"
IS_PAUSED_SERVICE = PLAYER_NODE + "/is_paused"
NOT_PAUSED_GRACE_S = 10.0      # services up but never paused -> nothing to hold, give up gently
RESUME_ATTEMPTS = 40           # 40 x 50 ms: a resume that does not take within 2 s is a failure

# ---------------------------------------------------------------------------------------------
# Pure core — the instant, the plan, the lines. No I/O, no ROS.
# ---------------------------------------------------------------------------------------------


def parse_instant(raw) -> float:
    """``RIG_REPLAY_START_AT_UNIX_S`` text -> unix seconds. rig writes ``f"{t:.3f}"``; anything
    that is not a finite positive decimal number is refused by name (a garbled instant must not
    silently become "release now")."""
    text = str(raw if raw is not None else "").strip()
    try:
        t = float(text)
    except ValueError:
        raise SystemExit(f"release_gate: RIG_REPLAY_START_AT_UNIX_S must be unix seconds as a "
                         f"decimal number (got {text!r})")
    if not math.isfinite(t) or t <= 0:
        raise SystemExit(f"release_gate: RIG_REPLAY_START_AT_UNIX_S must be a positive unix "
                         f"time (got {text!r})")
    return t


def format_instant(instant: float) -> str:
    """The instant exactly as rig printed it (three decimals) — one spelling in every log."""
    return f"{instant:.3f}"


def plan(instant: float, now: float) -> tuple[float, bool, str]:
    """(seconds to hold, late?, the arming line). A future instant holds for its distance;
    a past one holds 0 s and says by how much it is past."""
    delay = instant - now
    if delay > 0:
        return delay, False, f"release gate in {delay:.1f}s ({format_instant(instant)})"
    return 0.0, True, (f"release gate {format_instant(instant)} is {-delay:.1f}s in the past — "
                       "releasing as soon as the player holds")


def late_line(instant: float, ready_at: float) -> str:
    """The WARN when the player is ready only after the instant: names the lateness."""
    return (f"WARN: the player was ready {ready_at - instant:.1f}s AFTER the release instant "
            f"({format_instant(instant)}) — released late; the other sources of this replay "
            "started at the instant. Pass a larger --start-delay to rig replay next time")


def released_line(instant: float, now: float) -> str:
    """The wall time of the resume against the instant — the number that proves the gate."""
    return f"released at {now:.3f} ({now - instant:+.3f}s from the instant {format_instant(instant)})"


def node_identity(name: str) -> tuple[str, str]:
    """`release_gate` under `/<name>` — grouped under the player instance in rig's epoch reader,
    like `/<name>/latch_restore` and `/<name>/call_injector`."""
    return "release_gate", "/" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def hold_and_release(instant: float, *, services_ready, is_paused, resume, now, sleep, log,
                     ready_timeout_s: float, not_paused_grace_s: float = NOT_PAUSED_GRACE_S,
                     attempts: int = RESUME_ATTEMPTS) -> int:
    """The gate's life after arming, world injected: `services_ready() -> bool` (both player
    services discovered), `is_paused() -> bool | None` (None = the probe got no answer in time),
    `resume() -> (return_code, error_string) | None`, `now()` (wall seconds — the instant's
    clock), `sleep(s)`, `log(text)`.

      1. wait for the player to HOLD: services up, then `is_paused` true — a player whose
         services are up but that never reports paused within `not_paused_grace_s` has nothing
         to release (already playing?): WARN, return 0; services that never appear within
         `ready_timeout_s` are a refusal;
      2. hold until the instant (a past instant: WARN naming the lateness, no hold);
      3. resume, confirmed by `is_paused` false — a refused or unanswered call is retried
         `attempts` times at 50 ms; a player still paused after that is a refusal naming the
         hand fix.

    Returns 0 released (or nothing to release); raises SystemExit otherwise. Pure modulo the
    injected world — the bench-verified log lines are produced here, not in the shell."""
    t0 = now()
    seen = None
    while True:
        if not services_ready():
            if now() - t0 > ready_timeout_s:
                raise SystemExit(f"release_gate: the player's services ({RESUME_SERVICE}, "
                                 f"{IS_PAUSED_SERVICE}) never appeared within {ready_timeout_s:g}s")
            sleep(0.05)
            continue
        if seen is None:
            seen = now()
            log(f"player services up after {seen - t0:.1f}s — waiting for the hold")
        if is_paused() is True:
            break
        if now() - seen > not_paused_grace_s:
            log(f"WARN: the player never reported paused within {not_paused_grace_s:g}s of its "
                "services appearing — nothing to release (already playing?)")
            return 0
        sleep(0.05)
    ready_at = now()

    if ready_at > instant:
        log(late_line(instant, ready_at))
    else:
        log(f"holding {instant - ready_at:.1f}s more (player paused at {ready_at:.3f}, instant "
            f"{format_instant(instant)})")
        while True:
            remaining = instant - now()
            if remaining <= 0:
                break
            sleep(min(remaining, 0.02))

    for _attempt in range(attempts):
        resp = resume()
        fired = now()
        if resp is not None and resp[0] != 0:
            log(f"resume refused: {resp[1]!r} (code {resp[0]}) — retrying")
        elif resp is not None and is_paused() is False:
            log(released_line(instant, fired))
            return 0
        sleep(0.05)
    raise SystemExit(f"release_gate: the player did not resume after {attempts} attempts — it is "
                     f"still paused; `ros2 service call {RESUME_SERVICE} "
                     "rosbag2_interfaces/srv/Resume` releases it by hand")


# ---------------------------------------------------------------------------------------------
# Shell — the clients, the bounded call, the injected world.
# ---------------------------------------------------------------------------------------------

def _log(text: str) -> None:
    sys.stderr.write("release-gate: " + text + "\n")
    sys.stderr.flush()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ros2-bag-player release gate")
    ap.add_argument("--at", required=True, help="the release instant, unix seconds (rig's "
                                                 "RIG_REPLAY_START_AT_UNIX_S)")
    ap.add_argument("--player", default=PLAYER_NODE, help="the player node (its services hang "
                                                          "under it)")
    ap.add_argument("--ready-timeout", type=float,
                    default=float(os.environ.get("RELEASE_GATE_READY_TIMEOUT_S") or 600),
                    help="give up when the player's services never appear (s)")
    a = ap.parse_args()
    instant = parse_instant(a.at)
    _delay, _late, line = plan(instant, time.time())
    _log(line)

    import rclpy  # lazy: the pure core above stays importable (testable) without ROS
    from rosbag2_interfaces.srv import IsPaused, Resume

    rclpy.init()
    node_name, namespace = node_identity(os.environ.get("BAG_PLAYER_NAME") or "bag_player")
    node = rclpy.create_node(node_name, namespace=namespace)
    resume_client = node.create_client(Resume, a.player + "/resume")
    paused_client = node.create_client(IsPaused, a.player + "/is_paused")

    def call(client, request, timeout_s: float = 1.0):
        """One call, bounded; None when it did not come back in time."""
        future = client.call_async(request)
        deadline = time.monotonic() + timeout_s
        while not future.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
        if not future.done():
            future.cancel()
            return None
        return future.result()

    def services_ready() -> bool:
        return resume_client.service_is_ready() and paused_client.service_is_ready()

    def is_paused():
        resp = call(paused_client, IsPaused.Request())
        return None if resp is None else bool(resp.paused)

    def resume():
        resp = call(resume_client, Resume.Request())
        return None if resp is None else (int(resp.return_code), str(resp.error_string))

    try:
        return hold_and_release(instant, services_ready=services_ready, is_paused=is_paused,
                                resume=resume, now=time.time, sleep=time.sleep, log=_log,
                                ready_timeout_s=a.ready_timeout)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    sys.exit(main())
