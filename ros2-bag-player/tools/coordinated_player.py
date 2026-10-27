#!/usr/bin/env python3
"""Hold rosbag2, expose its paused clock only on arm, then resume on commit."""
from __future__ import annotations

import argparse
import json
import hashlib
import re
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from replay_coordination import Participant, CoordinationError


def selected_topics(topics, options):
    """Apply the emitted rosbag2 selectors to source names, before any remapping."""
    allow = options.topics or []
    services = options.services or []
    include = options.regex
    exclude = options.exclude_regex
    selected = []
    for topic in topics:
        service = topic.name[:-len("/_service_event")] if topic.name.endswith("/_service_event") else None
        accepted = (not allow and not services and not include) or topic.name in allow or service in services
        if include and re.fullmatch(include, topic.name):
            accepted = True
        if exclude and re.fullmatch(exclude, topic.name):
            accepted = False
        if topic.name == "/clock" and options.clock:
            accepted = False  # rosbag2 owns /clock when its periodic clock is enabled.
        if accepted:
            selected.append(topic)
    return selected


def validate_selected_types(command):
    """rosbag2 otherwise warns and silently drops topics with unavailable serializers."""
    if command[:3] != ["ros2", "bag", "play"]:
        raise CoordinationError("coordinated player requires the rosbag2 CLI command")
    from ros2bag.verb.play import PlayVerb
    import rosbag2_py
    from rosidl_runtime_py.utilities import get_message, get_service
    from rclpy.type_support import check_for_type_support
    parser = argparse.ArgumentParser()
    PlayVerb().add_arguments(parser, "ros2 bag play")
    options = parser.parse_args(command[3:])
    info = rosbag2_py.Info().read_metadata(options.bag_path, "")
    for topic in selected_topics([entry.topic_metadata for entry in info.topics_with_message_count], options):
        try:
            if topic.serialization_format != "cdr":
                raise ValueError(f"unsupported serialization {topic.serialization_format!r}")
            message = get_message(topic.type)
            check_for_type_support(message)
            if topic.name.endswith("/_service_event") and options.publish_service_requests:
                if not topic.type.endswith("_Event"):
                    raise ValueError("service event type lacks _Event suffix")
                service = get_service(topic.type[:-len("_Event")])
                for message in (service.Request, service.Response, service.Event):
                    check_for_type_support(message)
        except Exception as exc:
            raise CoordinationError(f"selected topic {topic.name} type {topic.type} unavailable: {exc}") from exc


def private_clock_command(command, topic):
    """rosbag2 argparse replaces repeated --remap groups; extend its existing group."""
    command = list(command)
    groups = [i for i, arg in enumerate(command) if arg in ("--remap", "-m")]
    if len(groups) > 1:
        raise CoordinationError("multiple rosbag2 remap groups are ambiguous")
    if groups:
        end = groups[0] + 1
        while end < len(command) and not command[end].startswith("-"):
            if command[end].split(":=", 1)[0] in ("clock", "/clock"):
                raise CoordinationError("coordinated playback owns the public clock remap")
            end += 1
        command.insert(end, "/clock:=" + topic)
    else:
        command += ["--remap", "/clock:=" + topic]
    return command


def check_latch(child, marker):
    if child is not None and child.poll() is not None:
        if child.returncode == 0 and marker.is_file() and marker.read_text().strip() == "empty":
            return
        raise CoordinationError(f"latch restore exited unexpectedly ({child.returncode})")


def stop(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--latch", default="[]")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("player command required")
    participant = Participant.from_env("player")
    if participant is None:
        raise CoordinationError("coordinated player requires a session")
    import rclpy
    from rosbag2_interfaces.srv import IsPaused, Resume
    from rosgraph_msgs.msg import Clock
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    rclpy.init()
    identity = os.environ["RIG_REPLAY_PARTICIPANT"]
    name = re.sub(r"[^A-Za-z0-9_]", "_", identity) + "_" + hashlib.sha256(identity.encode()).hexdigest()[:8]
    node = rclpy.create_node("coordinated_player", namespace="/" + name)
    children = []
    player = latch = readiness = None
    armed = False
    latest = None
    ready_file = Path("/tmp/latch_restore.ready")
    latch_args = json.loads(args.latch)
    if not isinstance(latch_args, list) or any(not isinstance(v, str) for v in latch_args):
        raise CoordinationError("invalid latch command")
    private_clock = "/rig_replay/" + name + "/clock"
    if "--clock" in command:
        command = private_clock_command(command, private_clock)
        publisher = node.create_publisher(Clock, "/clock", QoSProfile(depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT))
        def receive_clock(message):
            nonlocal latest
            latest = message
            if armed:
                publisher.publish(message)
        node.create_subscription(Clock, private_clock, receive_clock,
            QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT))
    paused = node.create_client(IsPaused, "/rosbag2_player/is_paused")
    resume = node.create_client(Resume, "/rosbag2_player/resume")

    def call(client, request):
        future = client.call_async(request)
        until = time.monotonic() + 0.25
        while not future.done() and time.monotonic() < until:
            rclpy.spin_once(node, timeout_sec=0.01)
        if not future.done():
            future.cancel()
            return None
        return future.result()

    def tick():
        rclpy.spin_once(node, timeout_sec=0.005)
        if player.poll() is not None:
            raise CoordinationError(f"bag player exited while held ({player.returncode})")
        if readiness.poll() not in (None, 0):
            raise CoordinationError("consumer readiness probe failed")
        check_latch(latch, ready_file)

    def prepared():
        if not paused.service_is_ready() or not resume.service_is_ready():
            return False
        response = call(paused, IsPaused.Request())
        return response is not None and response.paused

    def arm():
        nonlocal armed, latch
        armed = True
        if latest is not None:
            publisher.publish(latest)
        if latch_args:
            ready_file.unlink(missing_ok=True)
            latch = subprocess.Popen([sys.executable, str(Path(__file__).with_name("latch_restore.py")), *latch_args],
                env=dict(os.environ, RIG_LATCH_STRICT="1"))
            children.append(latch)

    try:
        validate_selected_types(command)
        player = subprocess.Popen(command)
        children.append(player)
        readiness = subprocess.Popen([sys.executable, str(Path(__file__).with_name("replay_readiness.py")),
            str(participant.directory / "requirements.json"), "--timeout", str(max(.1, min(3600,
                participant._deadline - time.time())))])
        children.append(readiness)
        instant = participant.wait_release(prepared_check=prepared,
            ready_check=lambda: not latch_args or ready_file.is_file(), on_arm=arm, on_tick=tick)
        response = call(resume, Resume.Request())
        if response is None or response.return_code != 0:
            raise CoordinationError("bag player did not acknowledge coordinated resume")
        actual = time.time()  # Observed resume acknowledgement, never an optimistic request timestamp.
        participant.started(actual)
        sys.stderr.write(f"coordinated-player: released at {actual:.6f}, target {instant:.6f}\n")
        while player.poll() is None:
            participant.poll()
            if readiness.poll() not in (None, 0):
                raise CoordinationError("consumer readiness probe failed")
            check_latch(latch, ready_file)
            rclpy.spin_once(node, timeout_sec=0.02)
        check_latch(latch, ready_file)
        if player.returncode:
            raise CoordinationError(f"bag player exited {player.returncode}")
        participant.completed()
        return 0
    except BaseException as exc:
        participant.failed(exc)
        raise
    finally:
        for child in reversed(children):
            stop(child)
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(main())
