#!/usr/bin/env python3
"""Builder-stage half of the fleet-ros-msgs overlay (runs INSIDE msgs/Dockerfile, not on the host):
validate the union manifest, clone each `source:` repo at its pin, and colcon-build the declared
interface packages into one merged install prefix (/opt/fleet-msgs).

Validation lives here — one place, container-side — so a hand-run `docker build` and a future
rig-driven build refuse the same manifests the same way:
  - an EMPTY manifest is refused: an overlay identical to the base is a pointless image, and under
    the future rig integration an empty union means "don't build the overlay at all";
  - `source:` entries need `repo` + `ref` + `packages` — the ref pin is MANDATORY because the overlay
    must publish wire-identical definitions to what the declaring service itself builds against;
  - the same repo declared at two different refs is a REFUSAL, not a manifest-order guess (two
    services disagreeing on a pin is the version skew this image exists to prevent — align the
    riggings); the same repo at the SAME ref from several services merges (packages union).
"""
import os
import subprocess
import sys

import yaml


def die(msg: str) -> None:
    print(f"build_msgs: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    manifest_path, prefix = sys.argv[1], sys.argv[2]
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}
    if not isinstance(manifest, dict):
        die(f"{manifest_path}: manifest must be a mapping with `apt:` and/or `source:` lists")
    apt = manifest.get("apt") or []
    source = manifest.get("source") or []
    if not isinstance(apt, list) or not isinstance(source, list):
        die(f"{manifest_path}: `apt:` and `source:` must be lists")
    if not apt and not source:
        die("empty manifest — no interface packages declared; refusing to build an overlay "
            "identical to the base image (with rig, an empty union should skip this build entirely)")

    os.makedirs(prefix, exist_ok=True)  # the final stage COPYs this even when source: is empty
    if not source:
        return

    pins: dict[str, str] = {}       # repo -> ref (conflict refusal)
    packages: dict[str, list] = {}  # repo -> union of declared packages
    for entry in source:
        if not isinstance(entry, dict):
            die(f"source entry must be a mapping: {entry!r}")
        repo, ref, pkgs = entry.get("repo"), entry.get("ref"), entry.get("packages")
        if not repo or not ref or not pkgs:
            die(f"source entry needs `repo`, `ref` (the pin is mandatory) and `packages`: {entry!r}")
        if repo in pins and pins[repo] != str(ref):
            die(f"{repo} declared at two different refs ('{pins[repo]}' vs '{ref}') — two services "
                f"disagree on the pin; align their `msgs:` declarations")
        pins[repo] = str(ref)
        packages.setdefault(repo, [])
        packages[repo] += [p for p in pkgs if p not in packages[repo]]

    ws = "/tmp/msgs_ws"
    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    for i, (repo, ref) in enumerate(pins.items()):
        dest = os.path.join(src, f"repo{i}")
        # full clone (no --depth): `ref` may be a tag, branch, or bare SHA — all must resolve
        subprocess.run(["git", "clone", repo, dest], check=True)
        subprocess.run(["git", "-C", dest, "checkout", "--detach", ref], check=True)

    wanted = sorted({p for pkgs in packages.values() for p in pkgs})
    # --packages-up-to: an interface package may depend on sibling interface packages in the same
    # repo; pull those in too. Anything heavier than message generation does not belong here —
    # the overlay is interface packages ONLY (doctrine, enforced by review not tooling).
    subprocess.run(
        ["colcon", "build", "--merge-install", "--install-base", prefix,
         "--packages-up-to", *wanted, "--cmake-args", "-DBUILD_TESTING=OFF"],
        cwd=ws, check=True)
    missing = [p for p in wanted if not os.path.isdir(os.path.join(prefix, "share", p))]
    if missing:
        die(f"declared packages not present after build: {missing} — wrong `packages:` names?")


if __name__ == "__main__":
    main()
