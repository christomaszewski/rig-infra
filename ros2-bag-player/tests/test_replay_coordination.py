"""Real files/threads exercise the held-release protocol without ROS or Docker."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from replay_coordination import Participant, CoordinationError, CoordinationAborted, _atomic
from coordinated_player import private_clock_command, check_latch, selected_topics
from types import SimpleNamespace


class Coordination(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "statuses").mkdir()
        self.identity = dict(version=1, session_id="a" * 32, plan_id="b" * 64)
        _atomic(self.root / "session.json", dict(self.identity, participants=["test.player"],
            startup_deadline_unix_s=time.time() + 10))
        self.seq = 0
        self.command("hold")
        self.participant = Participant(self.root, session_id="a" * 32, plan_id="b" * 64,
                                       participant="test.player")
        self.addCleanup(lambda: self.participant.aborted("test cleanup"))
        self.result = []

    def command(self, op, **extra):
        self.seq += 1
        _atomic(self.root / "command.json", dict(self.identity, seq=self.seq, op=op, **extra))

    def status(self):
        return json.loads((self.root / "statuses/test.player.json").read_text())

    def wait(self, state):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if self.status()["state"] == state:
                return self.status()
            time.sleep(.01)
        self.fail(f"waiting for {state}: {self.status()}")

    def worker(self, **kwargs):
        def run():
            try:
                instant = self.participant.wait_release(**kwargs)
                self.participant.started()
                self.result.append(instant)
            except Exception as exc:
                self.result.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=.2))
        return thread

    def arm(self):
        self.wait("held")
        self.command("arm")
        self.wait("ready")

    def test_holds_then_releases_only_after_matching_commit(self):
        prepared = threading.Event()
        worker = self.worker(prepared_check=prepared.is_set)
        time.sleep(.15)
        self.assertEqual(self.status()["state"], "preparing")
        prepared.set()
        self.arm()
        held_at = self.status()["held_at_unix_s"]
        self.assertLessEqual(held_at, self.status()["ready_at_unix_s"])
        release = time.time() + .45
        self.command("prepare_release", release_at_unix_s=release)
        self.wait("release_ack")
        self.assertFalse(self.result)
        self.command("commit", release_at_unix_s=release)
        worker.join(2)
        self.assertEqual(self.result, [release])
        self.assertGreaterEqual(self.status()["actual_release_at_unix_s"], release)
        self.participant.completed()
        self.assertEqual(self.status()["state"], "completed")
        self.assertEqual(self.status()["held_at_unix_s"], held_at)
        self.assertEqual((self.root / "statuses/test.player.json").stat().st_mode & 0o777, 0o644)

    def test_missing_commit_fails_without_output(self):
        worker = self.worker()
        self.arm()
        self.command("prepare_release", release_at_unix_s=time.time() + .3)
        worker.join(2)
        self.assertIsInstance(self.result[0], CoordinationError)
        self.assertEqual(self.status()["state"], "failed")
        self.assertNotIn("actual_release_at_unix_s", self.status())

    def test_slow_arm_keeps_heartbeats_and_can_be_aborted(self):
        entered, leave = threading.Event(), threading.Event()
        def arm():
            entered.set()
            leave.wait(2)
        worker = self.worker(on_arm=arm)
        self.wait("held")
        self.command("arm")
        self.assertTrue(entered.wait(1))
        old = self.status()["updated_at_unix_s"]
        time.sleep(.25)
        self.assertGreater(self.status()["updated_at_unix_s"], old)
        self.command("abort")
        self.wait("aborted")
        leave.set()
        worker.join(1)
        self.assertIsInstance(self.result[0], CoordinationAborted)

    def test_readiness_loss_fails_before_commit(self):
        ready = threading.Event(); ready.set()
        worker = self.worker(ready_check=ready.is_set)
        self.arm()
        ready.clear()
        worker.join(1)
        self.assertIsInstance(self.result[0], CoordinationError)
        self.assertIn("lost readiness", str(self.result[0]))

    def test_changed_release_and_foreign_commands_fail_closed(self):
        self.worker(); self.arm()
        release = time.time() + 1
        self.command("prepare_release", release_at_unix_s=release)
        self.wait("release_ack")
        self.command("commit", release_at_unix_s=release + 1)
        self.wait("failed")
        self.assertNotIn("actual_release_at_unix_s", self.status())

    def test_duplicate_sequence_is_idempotent_but_mutation_is_rejected(self):
        self.participant.poll(); self.participant.poll()
        _atomic(self.root / "command.json", dict(self.identity, seq=self.seq, op="arm"))
        with self.assertRaisesRegex(CoordinationError, "sequence"):
            self.participant.poll()

    def test_foreign_identity_and_unregistered_participant_rejected(self):
        with self.assertRaisesRegex(CoordinationError, "registered"):
            Participant(self.root, session_id="a"*32, plan_id="b"*64, participant="other.player")
        self.command("hold", plan_id="c"*64)
        with self.assertRaisesRegex(CoordinationError, "foreign"):
            self.participant.poll()

    def test_private_clock_extends_existing_remaps_without_replacing_them(self):
        command = ["ros2", "bag", "play", "bag", "--remap", "/raw:=/live", "--topics", "/raw"]
        self.assertEqual(private_clock_command(command, "/private"),
            ["ros2", "bag", "play", "bag", "--remap", "/raw:=/live", "/clock:=/private", "--topics", "/raw"])
        with self.assertRaisesRegex(CoordinationError, "owns"):
            private_clock_command(["--remap", "/clock:=/other"], "/private")

    def test_transient_missing_read_is_retried_but_bad_data_is_not(self):
        payload = json.dumps(dict(self.identity, seq=1, op="hold"))
        with self.participant._lock:
            with patch.object(Path, "read_text", side_effect=[FileNotFoundError(), payload]) as read:
                self.assertEqual(self.participant._read("command.json")["op"], "hold")
                self.assertEqual(read.call_count, 2)
            with patch.object(Path, "read_text", return_value="{") as read:
                with self.assertRaises(CoordinationError):
                    self.participant._read("command.json")
                self.assertEqual(read.call_count, 1)
            start = time.monotonic()
            with patch.object(Path, "read_text", side_effect=FileNotFoundError()):
                with self.assertRaises(CoordinationError):
                    self.participant._read("command.json")
            self.assertLess(time.monotonic() - start, .4)

    def test_only_empty_latch_may_exit_successfully(self):
        marker = self.root / "latch.ready"
        child = SimpleNamespace(poll=lambda: 0, returncode=0)
        for contents in (None, "restored:1\n", ""):
            if contents is not None:
                marker.write_text(contents)
            with self.assertRaises(CoordinationError):
                check_latch(child, marker)
        marker.write_text("empty\n")
        check_latch(child, marker)
        with self.assertRaises(CoordinationError):
            check_latch(SimpleNamespace(poll=lambda: 1, returncode=1), marker)

    def test_type_guard_selection_uses_original_names_and_service_union(self):
        topics = [SimpleNamespace(name=name) for name in ("/raw", "/unknown", "/s/_service_event", "/clock")]
        options = SimpleNamespace(topics=["/raw"], services=["/s"], regex=None, exclude_regex=None, clock=40)
        self.assertEqual([t.name for t in selected_topics(topics, options)], ["/raw", "/s/_service_event"])
        options.topics = []; options.services = []; options.exclude_regex = "unknown"
        self.assertIn("/unknown", [t.name for t in selected_topics(topics, options)])
        options.clock = 0
        self.assertIn("/clock", [t.name for t in selected_topics(topics, options)])
        options.clock = 40
        options.exclude_regex = "/unknown"
        self.assertEqual([t.name for t in selected_topics(topics, options)], ["/raw", "/s/_service_event"])

    def test_partial_environment_never_silently_disables_coordination(self):
        self.assertIsNone(Participant.from_env("player", {}))
        with self.assertRaises(CoordinationError):
            Participant.from_env("player", {"RIG_REPLAY_CONTROL_DIR": ""})
        with self.assertRaises(CoordinationError):
            self.participant.started()


if __name__ == "__main__":
    unittest.main()
