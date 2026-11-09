#!/bin/sh
# zenoh-store image build — the rig build contract: build.sh <registry> [tag].
# Declared as `build: { command: build.sh, images: [zenoh-store] }`. The build context is this
# directory alone (Dockerfile + zenoh_store/; .dockerignore keeps tests and config out).
#
# Env:
#   RIG_BUILD_NO_CACHE / RIG_BUILD_PLATFORMS / RIG_BUILD_RESULT / PUSH   handled by rig-build.sh
#   ZENOH_STORE_PYTHON_IMAGE   base image override (a mirrored python:3.12-slim)
#   RIG_BASE_IMAGE             not consumed: the store runs no ROS, so it does not build FROM the
#                              fleet's ROS base.
set -eu
registry="${1:?usage: build.sh <registry> [tag]}"
tag="${2:-latest}"
dir="$(cd "$(dirname "$0")" && pwd)"
ref="${registry}/zenoh-store:${tag}"

echo "zenoh-store: building ${ref}${RIG_BUILD_NO_CACHE:+ --no-cache --pull}" >&2
# shellcheck source=zenoh-store/rig-build.sh
. "$dir/rig-build.sh"
if [ -n "${ZENOH_STORE_PYTHON_IMAGE:-}" ]; then
    rig_build "$ref" --build-arg "PYTHON_IMAGE=${ZENOH_STORE_PYTHON_IMAGE}" "$dir"
else
    rig_build "$ref" "$dir"
fi
