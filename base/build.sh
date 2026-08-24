#!/bin/sh
# fleet-ros image build — the rig build contract: build.sh <registry> [tag].
#
# Declared by zenoh-router and ros2-bag-logger as
#   build: { command: ../base/build.sh, images: [fleet-ros], provides: base }
# `provides: base` (rig >= v0.2.21) makes fleet-ros the DEPLOYMENT's base image: images[0] names it,
# rig composes the ref exactly like the pull side (<registry>/fleet-ros:<tag>), builds it as stage 0
# — before every other service — and exports it to every OTHER build command and to every launcher
# as RIG_BASE_IMAGE. Both services name the same image from the same script, so rig dedupes them and
# this runs ONCE per `rig build`; run standalone, the double invocation is a docker-cache no-op
# building the identical image twice.
#
# Dedupe is by CONTENT (rig >= v0.2.25): script name + the git tree hash of the script's directory.
# That makes base/ the unit of identity, so it must stay SELF-CONTAINED — Dockerfile + build.sh,
# nothing referenced outside it; this script already passes base/ as the whole docker build context,
# and anything it pulled in from elsewhere would be invisible to the dedupe key.
#
# Env (the argv contract stays `<cmd> <registry> [tag]`; the rest arrives through the environment):
#   ROS_DISTRO          vehicle.yaml `ros.distro` (rig >= v0.1.29): the distro this image bakes, so
#                       the router and the sessions get one set of zenoh packages. The default
#                       (lyrical) applies only outside rig.
#   RIG_BUILD_NO_CACHE  `rig build --no-cache`: full rebuild, no layer cache AND --pull — a
#                       deliberate refresh must re-pull the ros base parent, the fleet's version
#                       authority, so every image FROM it advances together. This is the way to
#                       re-converge apt-level drift after `rig image audit` reports skew; without
#                       --pull, --no-cache would reuse the stale local parent and a "fresh" build
#                       would still inherit its old versions (the Dockerfile's --no-upgrade
#                       deliberately holds parent-carried packages at the parent's level).
#   RIG_ROS_RMW         vehicle.yaml `ros.rmw` (rig >= v0.2.23): the rmw this image installs, as
#                       ros-<distro>-<name, '_' -> '-'> — the same mapping `rig image audit` uses to
#                       check it, so the builder and the checker agree by construction. Rig-owned
#                       and set-or-popped; deliberately NOT the conventional RMW_IMPLEMENTATION,
#                       which most ROS shells export (a dev box's .bashrc must not decide what a
#                       fleet image contains).
#                       NOTE the consequence: on a non-zenoh fleet this image carries no
#                       rmw_zenoh_cpp, so it cannot run `rmw_zenohd` — zenoh-router requires
#                       `ros.rmw: rmw_zenoh_cpp` when it runs on fleet-ros. rig's doctor WARNs on
#                       that combination at preflight; see the README for the standalone router
#                       path a non-zenoh fleet would use instead.
#   FLEET_ROS_RMW       the same choice for builds OUTSIDE rig (standalone `./base/build.sh`), where
#                       nothing sets RIG_ROS_RMW. RIG_ROS_RMW wins when both are set.
#   RIG_BASE_IMAGE      NOT consumed, deliberately: fleet-ros IS the deployment's base image and
#                       cannot be built FROM itself, so rig pops the variable for this stage-0 build.
#                       (An explicit vehicle.yaml `images.base` doesn't re-parent fleet-ros either —
#                       it REPLACES it: rig skips this build entirely and the composes run that ref.)
set -eu
registry="${1:?usage: build.sh <registry> [tag]}"
tag="${2:-latest}"
base_dir="$(cd "$(dirname "$0")" && pwd)"
ref="${registry}/fleet-ros:${tag}"
distro="${ROS_DISTRO:-lyrical}"
rmw="${RIG_ROS_RMW:-${FLEET_ROS_RMW:-rmw_zenoh_cpp}}"

echo "fleet-ros: building ${ref} (ROS_DISTRO=${distro} rmw=${rmw})${RIG_BUILD_NO_CACHE:+ --no-cache --pull}" >&2
# Unquoted on purpose: each ${VAR:+word} expands to exactly one word or none (safe under `set -u`).
docker build ${RIG_BUILD_NO_CACHE:+--no-cache} ${RIG_BUILD_NO_CACHE:+--pull} \
    --build-arg "ROS_DISTRO=${distro}" \
    --build-arg "RMW_IMPLEMENTATION=${rmw}" \
    -t "$ref" "$base_dir"
docker push "$ref"
