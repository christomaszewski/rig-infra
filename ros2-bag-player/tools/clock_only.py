"""Drive an empty data selection across the selected bag's exact timeline."""
import argparse
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
    import rclpy
    from rosgraph_msgs.msg import Clock
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    rclpy.init(args=[])
    node = rclpy.create_node("replay_clock")
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE)
    publisher = node.create_publisher(Clock, "/clock", qos) if not a.wall_clock else None
    try:
        while a.at and time.time() < a.at:
            rclpy.spin_once(node, timeout_sec=min(.02, max(0, a.at - time.time())))
        while rclpy.ok():
            zero = time.monotonic()
            while rclpy.ok():
                elapsed = time.monotonic() - zero
                if publisher:
                    msg = Clock()
                    ns = stamp(a.start_ns, a.from_s, elapsed, a.rate, a.to_s)
                    msg.clock.sec, msg.clock.nanosec = divmod(ns, 1_000_000_000)
                    publisher.publish(msg)
                if elapsed * a.rate >= a.to_s - a.from_s:
                    break
                rclpy.spin_once(node, timeout_sec=.01)
            if not a.loop:
                break
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
