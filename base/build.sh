#!/bin/sh
# fleet-ros image build — the rig build contract: build.sh <registry> [tag].
#
# Declared by zenoh-router and ros2-bag-logger as
#   build: { command: ../base/build.sh, images: [fleet-ros] }
# (rig runs it with cwd = the service dir; this script self-locates, so the double invocation when
# both services are in one manifest is a docker-cache no-op building the identical image twice).
#
# ROS_DISTRO selects the base distro: `rig build` (≥ v0.1.29) exports it from vehicle.yaml's
# `ros.distro` automatically, so the image bakes the fleet's distro; the default (lyrical) applies
# only when run standalone outside rig.
set -eu
registry="${1:?usage: build.sh <registry> [tag]}"
tag="${2:-latest}"
base_dir="$(cd "$(dirname "$0")" && pwd)"
ref="${registry}/fleet-ros:${tag}"

echo "fleet-ros: building ${ref} (ROS_DISTRO=${ROS_DISTRO:-lyrical})" >&2
docker build --build-arg "ROS_DISTRO=${ROS_DISTRO:-lyrical}" -t "$ref" "$base_dir"
docker push "$ref"
