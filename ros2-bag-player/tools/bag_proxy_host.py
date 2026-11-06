#!/usr/bin/env python3
"""Run the offline converter in the player's native, locally pinned ROS image."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform as host_platform
import re
import subprocess
import sys
import uuid

from bag_proxy import policy
from runtime_images import native_platform, pinned_image


def mapped_request(request):
    if request.get("version") != 1 or not request.get("bags"):
        raise ValueError("bag proxy: expected version 1 and at least one selected bag")
    source = Path(request["source_root"]).resolve(strict=True)
    destination = Path(request["destination"]).resolve(strict=True)
    result = Path(request["result"]).resolve()
    if not source.is_dir() or not destination.is_dir() or not result.is_relative_to(destination):
        raise ValueError("bag proxy: invalid source, destination, or result path")
    paths = [source, destination, Path(__file__).parent.resolve()]
    if any("," in str(p) or "\n" in str(p) for p in paths):
        raise ValueError("bag proxy: mount paths cannot contain commas or newlines")
    bags = []
    for bag in request["bags"]:
        path = Path(bag["path"]).resolve(strict=True)
        if not path.is_relative_to(source) or not (path / "metadata.yaml").is_file():
            raise ValueError("bag proxy: selected bags require metadata inside the source root")
        if bag["storage"] not in ("mcap", "sqlite3"):
            raise ValueError("bag proxy: unsupported input storage")
        bags.append({**bag, "path": str(Path("/source") / path.relative_to(source))})
    return dict(version=1, source_root="/source", bags=bags, destination="/output",
                result=str(Path("/output") / result.relative_to(destination)), policy=policy(request["policy"]))


def image_identity(env):
    image = env.get("BAG_PLAYER_IMAGE") or env.get("RIG_MSGS_IMAGE") or env.get("RIG_BASE_IMAGE")
    if not image:
        registry = env.get("RIG_IMAGE_REGISTRY", "")
        image = (registry + "/" if registry else "") + "fleet-ros:" + (env.get("RIG_IMAGE_TAG") or "latest")
    image = pinned_image(image, env)
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                            text=True, timeout=30, env=env)
    if result.returncode:
        raise ValueError(f"bag proxy: acquire the native player image first: {image}\n{result.stderr.strip()}")
    info = json.loads(result.stdout)[0]
    identity = info.get("Id", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise ValueError("bag proxy: Docker returned an invalid image identity")
    env = dict(env)
    env.setdefault("RIG_TARGET_ARCH", {"aarch64": "arm64", "x86_64": "amd64"}.get(
        host_platform.machine().lower(), host_platform.machine().lower()))
    platform = native_platform(env)
    if info.get("Os") != "linux" or info.get("Architecture") != env["RIG_TARGET_ARCH"]:
        raise ValueError("bag proxy: player image must match the native replay architecture")
    return identity, platform


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise ValueError("usage: bag_proxy_host.py REQUEST_JSON")
    request = json.loads(Path(args[0]).read_text())
    mapped = mapped_request(request)
    image, platform = image_identity(os.environ)
    stage = Path(request["destination"]).resolve()
    control = stage / ".container-request.json"
    control.write_text(json.dumps(mapped))
    name = "rig-bag-proxy-" + uuid.uuid4().hex
    command = ["docker", "run", "--rm", "--name", name, "--pull=never", "--network=none",
               "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
               f"--platform={platform}", "--user", f"{os.getuid()}:{os.getgid()}",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--env", "HOME=/tmp",
               "--env", "ROS_LOG_DIR=/tmp/ros-log", "--env", "PYTHONDONTWRITEBYTECODE=1",
               "--env", "SQLITE_TMPDIR=/output", "--env", "RCUTILS_LOGGING_USE_STDOUT=0",
               "--mount", f"type=bind,src={Path(request['source_root']).resolve()},dst=/source,readonly",
               "--mount", f"type=bind,src={stage},dst=/output",
               "--mount", f"type=bind,src={Path(__file__).parent.resolve()},dst=/tools,readonly",
               "--entrypoint", "/bin/bash", image, "-c",
               'set -e; . "/opt/ros/$ROS_DISTRO/setup.bash"; '
               'if [ -f /opt/fleet-msgs/setup.bash ]; then . /opt/fleet-msgs/setup.bash; fi; '
               'exec python3 /tools/bag_proxy.py /output/.container-request.json']
    try:
        code = subprocess.run(command).returncode
        if code:
            return code
        output = Path(request["result"])
        result = json.loads(output.read_text())
        result.setdefault("producer", {})["image_id"] = image
        output.write_text(json.dumps(result, indent=2) + "\n")
        return 0
    finally:
        control.unlink(missing_ok=True)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write(f"bag proxy: {exc}\n")
        sys.exit(2)
