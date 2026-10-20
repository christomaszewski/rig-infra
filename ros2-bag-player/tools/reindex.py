"""Bounded storage-only helper. The caller must supply a disposable derived bag directory."""
import os
from pathlib import Path
import subprocess
import sys


def command(path, env):
    path = Path(path).resolve()
    files = [*path.glob("*.mcap"), *path.glob("*.db3")]
    if not path.is_dir() or not files:
        raise SystemExit("reindex: expected a derived ROS 2 bag directory")
    stores = {"mcap" if p.suffix == ".mcap" else "sqlite3" for p in files}
    if len(stores) != 1:
        raise SystemExit("reindex: mixed storage formats are ambiguous")
    image = env.get("BAG_PLAYER_IMAGE") or env.get("RIG_MSGS_IMAGE") or env.get("RIG_BASE_IMAGE")
    if not image:
        prefix = env.get("RIG_IMAGE_REGISTRY", "")
        image = (prefix + "/" if prefix else "") + "fleet-ros:" + env.get("RIG_IMAGE_TAG", "latest")
    return ["docker", "run", "--rm", "--pull=never", "--network=none", "--cap-drop=ALL",
            "--user", f"{os.getuid()}:{os.getgid()}", "--mount", f"type=bind,src={path},dst=/bag",
            "--entrypoint", "/bin/bash", image, "-c",
            'set -e; . "/opt/ros/$ROS_DISTRO/setup.bash"; exec ros2 bag reindex --storage "$1" /bag',
            "--", next(iter(stores))]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: reindex.py DERIVED_BAG_DIRECTORY")
    import uuid
    name = "rig-replay-index-" + uuid.uuid4().hex
    argv = command(sys.argv[1], os.environ)
    argv[2:2] = ["--name", name]
    try:
        raise SystemExit(subprocess.run(argv, timeout=540).returncode)
    except subprocess.TimeoutExpired:
        raise SystemExit("reindex: storage helper timed out")
    finally:
        # Killing a Docker client does not stop its container. Limit cleanup to our unique helper.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
