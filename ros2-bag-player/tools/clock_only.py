"""Drive an empty data selection across the selected bag's exact timeline."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time


def stamp(start_ns, offset_s, elapsed_s, rate, end_s):
    return start_ns + int(min(end_s, offset_s + elapsed_s * rate) * 1e9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-ns", type=int, required=True)
    ap.add_argument("--from", dest="from_s", type=float, required=True)
    ap.add_argument("--to", dest="to_s", type=float, required=True)
    ap.add_argument("--rate", type=float, default=1)
    ap.add_argument("--at", type=float)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--wall-clock", action="store_true")
    a = ap.parse_args()
    import math
    if not all(math.isfinite(x) for x in (a.from_s, a.to_s, a.rate, a.at or 0)) or (
            a.from_s < 0 or a.to_s <= a.from_s or a.rate <= 0):
        ap.error("invalid clock timeline")
    participant = None
    readiness = None
    if "RIG_REPLAY_CONTROL_DIR" in os.environ:
        from replay_coordination import Participant
        participant = Participant.from_env("player")
    import rclpy
    from rosgraph_msgs.msg import Clock
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    rclpy.init(args=[])
    node = rclpy.create_node("replay_clock")
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE)
    publisher = node.create_publisher(Clock, "/clock", qos) if not a.wall_clock else None
    def publish(elapsed):
        if publisher:
            msg = Clock()
            ns = stamp(a.start_ns, a.from_s, elapsed, a.rate, a.to_s)
            msg.clock.sec, msg.clock.nanosec = divmod(ns, 1_000_000_000)
            publisher.publish(msg)
    try:
        armed = False
        if participant:
            readiness = subprocess.Popen([sys.executable, str(Path(__file__).with_name("replay_readiness.py")),
                str(participant.directory / "requirements.json"), "--timeout",
                str(max(.1, min(3600, participant._deadline - time.time())))])
            def arm():
                nonlocal armed
                armed = True
            def tick():
                if readiness.poll() not in (None, 0):
                    raise RuntimeError("consumer readiness probe failed")
                if armed:
                    publish(0)
                rclpy.spin_once(node, timeout_sec=.005)
            a.at = participant.wait_release(on_arm=arm, on_tick=tick)
            participant.started()
        while a.at and time.time() < a.at:
            rclpy.spin_once(node, timeout_sec=min(.02, max(0, a.at - time.time())))
        while rclpy.ok():
            zero = time.monotonic() + (a.at - time.time() if a.at else 0)
            a.at = None
            while rclpy.ok():
                elapsed = time.monotonic() - zero
                if participant:
                    participant.poll()
                publish(elapsed)
                if elapsed * a.rate >= a.to_s - a.from_s:
                    break
                rclpy.spin_once(node, timeout_sec=.01)
            if not a.loop:
                break
        if participant:
            participant.completed()
    except BaseException as exc:
        if participant:
            participant.failed(exc)
            raise
        if not isinstance(exc, (KeyboardInterrupt, rclpy.executors.ExternalShutdownException)):
            raise
    finally:
        if readiness is not None:
            from coordinated_player import stop
            stop(readiness)
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
