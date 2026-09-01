#!/bin/sh
# fleet-ros-msgs overlay build — the rig build contract shape: build-msgs.sh <registry> [tag].
#
# Builds <registry>/fleet-ros-msgs:<tag> = the deployment's base image + the UNION of the interface
# packages the deployment's services declare in their riggings' `msgs:` blocks (see
# msgs-manifest.example.yaml for both the per-service declaration and the union manifest — same
# schema). The ros2 bag logger's compose prefers this image over the bare base
# (BAG_LOGGER_IMAGE -> RIG_MSGS_IMAGE -> RIG_BASE_IMAGE -> composed fleet-ros ref), so building and
# exporting it is all it takes for the logger to start recording the fleet's custom types.
#
# Driven by `rig build` (rig >= v0.2.28): the zenoh-router + ros2-bag-logger riggings declare this
# script as `build.msgs_overlay` — rig unions the fleet's `msgs:` blocks, renders the union to a
# temp file (RIG_MSGS_MANIFEST), and runs this right after the base stage; `rig up` then exports
# RIG_MSGS_IMAGE and the logger records the fleet's custom types. Standalone use (no rig, or an
# older rig) stays supported — author the union by hand and point the logger at the result:
#   FLEET_MSGS_MANIFEST=./msgs.yaml ./msgs/build-msgs.sh <registry> <tag>
#   # then export BAG_LOGGER_IMAGE=<registry>/fleet-ros-msgs:<tag> (or RIG_MSGS_IMAGE) for `rig up`
#
# Env (the argv contract stays `<cmd> <registry> [tag]`; the rest arrives through the environment):
#   RIG_MSGS_MANIFEST    path to the union manifest — the name rig will own once it renders the
#                        union itself (rig-owned, set-or-popped, like RIG_ROS_RMW).
#   FLEET_MSGS_MANIFEST  the same for builds OUTSIDE rig (standalone), where nothing sets
#                        RIG_MSGS_MANIFEST; rig's var wins when both are set (the FLEET_ROS_RMW
#                        pattern from base/build.sh).
#   RIG_BASE_IMAGE       the deployment's base image — this overlay builds FROM it. Fallback:
#                        <registry>/fleet-ros:<tag>, the same composed ref the pull side uses.
#   ROS_DISTRO           vehicle.yaml `ros.distro`; maps `apt:` names to ros-<distro>-* packages.
#   RIG_BUILD_NO_CACHE   `rig build --no-cache`: full rebuild. Deliberately NO --pull here (unlike
#                        base/build.sh): the parent is the deployment's own base image, whose
#                        version authority is the stage-0 build on THIS box, not the registry.
set -eu
registry="${1:?usage: build-msgs.sh <registry> [tag]}"
tag="${2:-latest}"
msgs_dir="$(cd "$(dirname "$0")" && pwd)"
manifest="${RIG_MSGS_MANIFEST:-${FLEET_MSGS_MANIFEST:-}}"
if [ -z "$manifest" ] || [ ! -f "$manifest" ]; then
    echo "build-msgs.sh: set RIG_MSGS_MANIFEST (or FLEET_MSGS_MANIFEST) to the union manifest path" >&2
    echo "  (schema: msgs/msgs-manifest.example.yaml; empty manifests are refused by the build)" >&2
    exit 1
fi
distro="${ROS_DISTRO:-lyrical}"
base="${RIG_BASE_IMAGE:-${registry}/fleet-ros:${tag}}"
ref="${registry}/fleet-ros-msgs:${tag}"

# Stage a throwaway context: the manifest lives wherever the caller keeps it (rig will render a temp
# file; hand use points at a deployment-owned yaml) — never copied into msgs/ itself, which must stay
# a clean, self-contained build unit.
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
cp "$msgs_dir/Dockerfile" "$msgs_dir/build_msgs.py" "$ctx/"
cp "$manifest" "$ctx/msgs-manifest.yaml"

# ssh-form source repos clone inside the builder via BuildKit agent forwarding — the invoking
# machine's keys, never baked into a layer (msgs/Dockerfile's --mount=type=ssh RUN). The flag is
# passed only when the manifest needs it, so https-only fleets and agent-less CI are untouched.
# An EMPTY agent forwards fine and then fails the clone confusingly (macOS git uses key files
# directly, but BuildKit forwards the AGENT) — hence the ssh-add -l identity check, not just
# a socket check.
ssh_flag=""
if grep -Eq '^[[:space:]]*-?[[:space:]]*repo:[[:space:]]*["'"'"']?(git@|ssh://)' "$ctx/msgs-manifest.yaml"; then
    if [ -z "${SSH_AUTH_SOCK:-}" ] || ! ssh-add -l >/dev/null 2>&1; then
        echo "build-msgs.sh: the manifest declares ssh-form source repos, but no ssh-agent with" >&2
        echo "  loaded identities is available for BuildKit to forward. Fix on the build machine:" >&2
        echo "    eval \"\$(ssh-agent)\"   # if no agent is running" >&2
        echo "    ssh-add               # load the key that can clone those repos" >&2
        echo "  (keys are FORWARDED into the builder stage only — never baked into the image)" >&2
        exit 1
    fi
    ssh_flag="--ssh default"
fi

echo "fleet-ros-msgs: building ${ref} FROM ${base} (manifest=${manifest})${RIG_BUILD_NO_CACHE:+ --no-cache}${ssh_flag:+ ${ssh_flag}}" >&2
# Unquoted on purpose: ${VAR:+word} expands to exactly one word or none (safe under `set -u`).
docker build ${RIG_BUILD_NO_CACHE:+--no-cache} ${ssh_flag} \
    --build-arg "BASE_IMAGE=${base}" \
    --build-arg "ROS_DISTRO=${distro}" \
    -t "$ref" "$ctx"
docker push "$ref"
