#!/usr/bin/env python3
"""Bounded isolated container wrapper for the read-only recorded-request inspector."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

from inspect_calls import load_request
from runtime_images import native_platform, pinned_image


def command(request_path, env, name):
    if "RIG_RUNTIME_IMAGE_PINS" not in env:
        raise ValueError("recorded-request inspection requires verified RIG_RUNTIME_IMAGE_PINS")
    request_path = Path(request_path).resolve(strict=True)
    request = load_request(request_path)
    session = Path(request["session"]).resolve(strict=True)
    if str(session) != request["session"] or not session.is_dir() or not (session / "metadata.yaml").is_file():
        raise ValueError("inspection session must be the canonical exact bag directory with metadata.yaml")
    helper = Path(__file__).with_name("inspect_calls.py").resolve()
    for path in (session, request_path, helper):
        if "," in str(path):
            raise ValueError("inspection mount paths cannot contain commas")
    image = env.get("BAG_PLAYER_IMAGE") or env.get("RIG_MSGS_IMAGE") or env.get("RIG_BASE_IMAGE")
    if not image:
        prefix = env.get("RIG_IMAGE_REGISTRY", "")
        image = (prefix + "/" if prefix else "") + "fleet-ros:" + (env.get("RIG_IMAGE_TAG") or "latest")
    image = pinned_image(image, env)
    platform = native_platform(env)
    if not platform:
        raise ValueError("recorded-request inspection requires native RIG_TARGET_ARCH")
    return ["docker", "run", "--rm", "--name", name, "--pull=never", "--network=none",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
            f"--platform={platform}", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--env", "HOME=/tmp", "--env", "ROS_LOG_DIR=/tmp/ros-log",
            "--env", "RCUTILS_LOGGING_USE_STDOUT=0", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,src={session},dst=/bag,readonly",
            "--mount", f"type=bind,src={request_path},dst=/request.json,readonly",
            "--mount", f"type=bind,src={helper},dst=/inspect_calls.py,readonly",
            "--entrypoint", "/bin/bash", image, "-c",
            'set -e; . "/opt/ros/$ROS_DISTRO/setup.bash"; '
            'if [ -f /opt/fleet-msgs/setup.bash ]; then . /opt/fleet-msgs/setup.bash; fi; '
            'exec python3 /inspect_calls.py /request.json /bag']


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise ValueError("usage: inspect_calls_host.py REQUEST_JSON")
    name = "rig-replay-inspect-" + uuid.uuid4().hex
    cmd = command(args[0], os.environ, name)
    try:
        return subprocess.run(cmd, timeout=90).returncode
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("recorded-request inspection helper exceeded 90 seconds") from exc
    finally:
        # Killing a Docker client does not stop its container; target only this unique helper.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write(f"replay-inspect-calls: {exc}\n")
        sys.exit(2)
