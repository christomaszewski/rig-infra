"""The release gate's pure core (tools/release_gate.py) against rig-sensor-replay-plan §5 /
rig-replay-player-handoff §1.1 (amended 2026-09-07; rig >= v0.2.52): the instant as rig spells
it (`RIG_REPLAY_START_AT_UNIX_S`, unix seconds with three decimals) parsed strictly — a garbled
instant refuses by name rather than becoming "play now"; a future instant plans a hold of its
distance and logs `release gate in N.Ns (instant)`; a past instant holds 0 s and the lateness is
NAMED (the WARN the operator reads to pick a larger --start-delay); the `released` line reports
the wall time of the resume against the instant; the companion's node groups under the player
instance like its siblings. The whole wait/hold/release loop (`hold_and_release`) runs here
against a scripted world — a fake wall clock whose `sleep` advances it, scripted service
discovery and call answers — so the branches the bench never hit are pinned: services that
appear late are waited for and named; services that never appear refuse; a player that never
reports paused is given up on gently (WARN, exit 0, no resume); a refused resume is retried; an
unanswered probe keeps waiting; a resume that never takes refuses naming the hand fix; the
happy path resumes exactly at the instant. Only the rclpy client construction stays
bench-verified. Run: `python3 tests/test_release_gate.py` (no ROS)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import release_gate

INSTANT = 1788788622.342          # the handoff's example: rig's f"{t:.3f}"


def _expect_exit(needle, raw):
    try:
        release_gate.parse_instant(raw)
        raise AssertionError(f"expected SystemExit mentioning {needle!r} for {raw!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


# --- the instant ---------------------------------------------------------------------------------

def test_parse_instant_accepts_rigs_spelling():
    assert release_gate.parse_instant("1788788622.342") == INSTANT
    assert release_gate.parse_instant(" 1788788622.342\n") == INSTANT     # env whitespace
    assert release_gate.parse_instant("1788788622") == 1788788622.0       # a bare integer is fine
    assert release_gate.parse_instant(INSTANT) == INSTANT                  # already a number


def test_parse_instant_refuses_junk_by_name():
    for raw in ("", "   ", "soon", "-1788788622.342", "0", "nan", "inf", None):
        _expect_exit("RIG_REPLAY_START_AT_UNIX_S", raw)


def test_format_instant_is_rigs_three_decimals():
    assert release_gate.format_instant(INSTANT) == "1788788622.342"
    assert release_gate.format_instant(1788788622.0) == "1788788622.000"
    assert release_gate.format_instant(1788788622.3) == "1788788622.300"


# --- the plan ------------------------------------------------------------------------------------

def test_future_instant_plans_the_hold_and_names_it():
    delay, late, line = release_gate.plan(INSTANT, INSTANT - 12.342)
    assert abs(delay - 12.342) < 1e-6 and late is False
    assert line == "release gate in 12.3s (1788788622.342)"


def test_past_instant_releases_at_once_and_names_the_lateness():
    delay, late, line = release_gate.plan(INSTANT, INSTANT + 3.3)
    assert delay == 0.0 and late is True
    assert "1788788622.342" in line and "3.3s in the past" in line
    warn = release_gate.late_line(INSTANT, INSTANT + 3.3)
    assert warn.startswith("WARN") and "3.3s AFTER" in warn and "1788788622.342" in warn
    assert "--start-delay" in warn                 # the operator's fix is named


def test_the_instant_itself_is_not_late():
    delay, late, _ = release_gate.plan(INSTANT, INSTANT)
    assert delay == 0.0 and late is True           # exactly now: nothing to hold, release


def test_released_line_reports_wall_time_against_the_instant():
    line = release_gate.released_line(INSTANT, INSTANT + 0.004)
    assert line == "released at 1788788622.346 (+0.004s from the instant 1788788622.342)"
    assert "(-0.002s" in release_gate.released_line(INSTANT, INSTANT - 0.002)


# --- identity -------------------------------------------------------------------------------------

def test_gate_node_groups_under_the_player_instance():
    assert release_gate.node_identity("bag_player") == ("release_gate", "/bag_player")
    assert release_gate.node_identity("big-player.2") == ("release_gate", "/big_player_2")
    assert release_gate.RESUME_SERVICE == "/rosbag2_player/resume"       # pinned on lyrical
    assert release_gate.IS_PAUSED_SERVICE == "/rosbag2_player/is_paused"


# --- the loop, world injected ----------------------------------------------------------------------

class World:
    """A scripted player + wall clock. `services_at`: the clock at which the services appear;
    `paused`: the answers is_paused gives, in order (the last repeats; None = no answer);
    `resumes`: the answers resume gives, in order ((code, error) or None; the last repeats)."""

    def __init__(self, t0=1000.0, services_at=None, paused=(True,), resumes=((0, ""),),
                 paused_after_resume=False):
        self.t = t0
        self.services_at = t0 if services_at is None else services_at
        self._paused = list(paused)
        self._resumes = list(resumes)
        self.paused_after_resume = paused_after_resume
        self.resumed_at: list[float] = []
        self.log: list[str] = []

    def now(self):
        return self.t

    def sleep(self, s):
        assert s > 0
        self.t += s

    def services_ready(self):
        return self.t >= self.services_at

    def is_paused(self):
        if self.resumed_at and not self.paused_after_resume:
            return False
        ans = self._paused.pop(0) if len(self._paused) > 1 else self._paused[0]
        return ans

    def resume(self):
        self.resumed_at.append(self.t)
        return self._resumes.pop(0) if len(self._resumes) > 1 else self._resumes[0]

    def run(self, instant, **kw):
        return release_gate.hold_and_release(
            instant, services_ready=self.services_ready, is_paused=self.is_paused,
            resume=self.resume, now=self.now, sleep=self.sleep, log=self.log.append,
            ready_timeout_s=kw.pop("ready_timeout_s", 600), **kw)


def _expect_loop_exit(needle, world, instant, **kw):
    try:
        world.run(instant, **kw)
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


def test_happy_path_holds_until_the_instant_and_resumes_exactly_there():
    w = World(t0=1000.0)
    assert w.run(1005.0) == 0
    assert w.resumed_at == [1005.0]                   # one resume, at the instant, not before
    assert w.log[0] == "player services up after 0.0s — waiting for the hold"
    assert w.log[1].startswith("holding 5.0s more (player paused at 1000.000, instant 1005.000)")
    assert w.log[-1] == "released at 1005.000 (+0.000s from the instant 1005.000)"
    assert not any("WARN" in ln for ln in w.log)


def test_services_appearing_late_are_waited_for_and_named():
    w = World(t0=1000.0, services_at=1003.0)
    assert w.run(1010.0) == 0
    assert w.log[0] == "player services up after 3.0s — waiting for the hold"
    assert w.resumed_at == [1010.0]                   # the hold still lands at the instant


def test_services_that_never_appear_refuse_naming_them():
    w = World(t0=1000.0, services_at=10_000.0)
    _expect_loop_exit("/rosbag2_player/resume", w, 1010.0, ready_timeout_s=2.0)
    _expect_loop_exit("never appeared within 2s", World(t0=1000.0, services_at=10_000.0),
                      1010.0, ready_timeout_s=2.0)
    assert w.resumed_at == []


def test_a_player_that_never_reports_paused_is_given_up_on_gently():
    w = World(t0=1000.0, paused=(False,))
    assert w.run(1010.0) == 0                          # exit 0: nothing to release
    assert w.resumed_at == []                          # ... and nothing was resumed
    assert any(ln.startswith("WARN: the player never reported paused within 10s") for ln in w.log)
    assert 1010.0 <= w.t <= 1010.2                     # gave up after the grace, not at the instant


def test_an_unanswered_probe_keeps_waiting():
    w = World(t0=1000.0, paused=(None, None, True))
    assert w.run(1001.0) == 0
    assert w.resumed_at == [1001.0]


def test_a_refused_resume_is_retried():
    w = World(t0=1000.0, resumes=((3, "resume failed"), (0, "")))
    assert w.run(1001.0) == 0
    assert len(w.resumed_at) == 2 and w.resumed_at[0] == 1001.0
    assert any(ln == "resume refused: 'resume failed' (code 3) — retrying" for ln in w.log)
    assert w.log[-1].startswith("released at 1001.05")   # one 50 ms retry later


def test_an_unanswered_resume_is_retried_too():
    w = World(t0=1000.0, resumes=(None, (0, "")))
    assert w.run(1001.0) == 0 and len(w.resumed_at) == 2


def test_a_resume_that_never_takes_refuses_naming_the_hand_fix():
    w = World(t0=1000.0, paused_after_resume=True)     # is_paused stays true whatever we call
    _expect_loop_exit("still paused", w, 1001.0, attempts=3)
    assert len(w.resumed_at) == 3
    _expect_loop_exit("ros2 service call /rosbag2_player/resume",
                      World(t0=1000.0, paused_after_resume=True), 1001.0, attempts=1)


def test_a_past_instant_releases_at_once_naming_the_lateness():
    w = World(t0=1000.0)
    assert w.run(991.5) == 0
    assert w.resumed_at == [1000.0]                    # no hold
    assert not any(ln.startswith("holding") for ln in w.log)
    assert any("8.5s AFTER the release instant (991.500)" in ln for ln in w.log)
    assert w.log[-1] == "released at 1000.000 (+8.500s from the instant 991.500)"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
