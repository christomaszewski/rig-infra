# fleet-ros — the shared infra base image for rig fleets, and the DEPLOYMENT's base image.
#
# A runtime image that carries exactly what the infra tier needs to join and record the
# vehicle's ROS 2 graph: rmw_zenoh (router + sessions from the SAME distro packages — the
# router/session version-match holds by construction) and rosbag2 with mcap storage. The zenoh-router
# and ros2-bag-logger services default to this image, so a camera-less vehicle never pulls a driver
# image just to run its router and logger.
#
# Both of those riggings declare it `build: { command: ../base/build.sh, images: [fleet-ros],
# provides: base }`, which makes this the DEPLOYMENT's base image: rig builds it first and exports
# the composed ref to every other build and to every launcher as ${RIG_BASE_IMAGE}, so one image
# pins the fleet's distro+rmw packages. The composes here RUN it; a service that builds its own image
# should build `FROM ${RIG_BASE_IMAGE}`. (`rig image audit` is the detection side of the same
# invariant; `rig build --no-cache` the remediation.)
#
# This image is the ROOT of that chain — it is built FROM the stock ROS base, never FROM
# ${RIG_BASE_IMAGE}, which is itself: rig deliberately pops that variable for the base build.
#
# Built + pushed by base/build.sh (the rig build contract); pulled by the composes as
# ${RIG_BASE_IMAGE}, falling back to ${RIG_IMAGE_REGISTRY}/fleet-ros:${RIG_IMAGE_TAG} — certify
# (which never inherits a RIG_BASE_IMAGE) enforces that build/pull tag agreement.
ARG ROS_DISTRO=lyrical
FROM ros:${ROS_DISTRO}-ros-core AS packages
ARG ROS_DISTRO
# The rmw to install, as its ROS name; build.sh sources it from vehicle.yaml `ros.rmw` (rig exports
# it as RIG_ROS_RMW, rig >= v0.2.23). The apt package is ros-<distro>-<name, '_' -> '-'> — exactly
# the mapping `rig image audit` uses to check the declared rmw is installed, so builder and checker
# agree by construction. The default only applies outside rig; note that a non-zenoh value yields an
# image with no rmw_zenohd in it, which zenoh-router cannot run on (see build.sh and the README).
ARG RMW_IMPLEMENTATION=rmw_zenoh_cpp
# --no-upgrade: installing a package the parent image already carries silently upgrades it to the
# repo's current candidate — pushing this image past the parent, and past every sibling image FROM
# the same parent that merely inherits the package (the skew `rig image audit` flags). The local
# ros base parent is the version authority: parent-carried packages (rosbag2 and its storage
# plugins here) stay at ITS versions and advance only when the parent itself is re-pulled
# deliberately (build.sh: --pull rides with --no-cache); packages the parent lacks (the rmw)
# install fresh at the candidate. Known residual, same as rig's v0.2.24 consumer doctrine:
# --no-upgrade holds only the packages LISTED here — dependency resolution can still upgrade
# what it drags in. Accepted.
RUN rmw_pkg="ros-${ROS_DISTRO}-$(echo "${RMW_IMPLEMENTATION}" | tr '_' '-')" \
    && apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
        python3-yaml python3-lz4 python3-zstandard \
        ros-${ROS_DISTRO}-ros-base \
        "${rmw_pkg}" \
        ros-${ROS_DISTRO}-rosbag2 \
        ros-${ROS_DISTRO}-rosbag2-storage-mcap \
    && rm -rf /var/lib/apt/lists/*

# MCAP channel metadata and the full publication sequence are needed during bag export.
# This pure Python wheel is common to arm64 and amd64. Its codecs come from Ubuntu above.
ADD --checksum=sha256:9098685d67288a8087166504cf4adf617cfa8639bb60e936af113f62c11c293f https://files.pythonhosted.org/packages/63/22/dad47e86047344f110a9d589d90de49d9c54db7a7ea4f0ef91fb3e8e9f3f/mcap-1.3.1-py3-none-any.whl /tmp/mcap.whl
RUN python3 -m zipfile -e /tmp/mcap.whl /usr/lib/python3/dist-packages \
    && rm /tmp/mcap.whl && python3 -c 'import mcap.reader, lz4.frame, zstandard'

# The capture extension is pinned to the installed rosbag2 ABI. Only three implementation
# libraries change; public structs, CLI, Python bindings and other ROS libraries stay stock.
# The compression implementation carries identity across both MESSAGE and FILE zstd paths.
# Fail the version guard on an upstream upgrade until its source patch is revalidated.
FROM packages AS provenance-build
ARG ROS_DISTRO
RUN apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
        build-essential cmake patch \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:8bbdcf3cb93981de6040ced30c874f1c854b325d8f85f8f667ef656428e915e8 https://github.com/ros2/rosbag2/archive/refs/tags/0.33.4.tar.gz /tmp/rosbag2.tar.gz
COPY provenance /provenance
RUN tar -xzf /tmp/rosbag2.tar.gz -C /tmp \
    && bash /provenance/build.sh /tmp/rosbag2-0.33.4

FROM packages AS provenance-runtime
ARG ROS_DISTRO
COPY --from=provenance-build /rig-provenance/ /opt/ros/${ROS_DISTRO}/

# The replay gate: check_replay_runtime.py exercises the packages installed above against the
# player API/storage contract. It runs in a stage of its own with the script bind-mounted, and
# the image receives one constant file from it, never the script. BuildKit keys a COPY on the
# content it copies, so when only the script changes the gate runs again, writes the same marker,
# and the COPY below and every later step stay cached: the image keeps its layers and its
# per-architecture manifest, and nothing built FROM it rebuilds. (A script copied into the image
# gave the base new layers, and every dependent a full rebuild, each time the script changed.)
# A failing check writes no marker and fails the build. Keep the marker's text constant: editing
# it changes the image.
FROM provenance-runtime AS replay-gate
ARG ROS_DISTRO
RUN --mount=type=bind,source=check_replay_runtime.py,target=/check_replay_runtime.py \
    --mount=type=bind,source=test_publication_provenance.py,target=/test_publication_provenance.py \
    . /opt/ros/${ROS_DISTRO}/setup.sh \
    && python3 /check_replay_runtime.py \
    && python3 /test_publication_provenance.py \
    && mkdir /replay-gate \
    && printf 'base/check_replay_runtime.py passed when this image was built.\n' \
        > /replay-gate/replay-runtime-checked

FROM provenance-runtime AS runtime-deps
# Advertise only after the installed runtime passes the player API/storage contract: the labels
# follow the gate's marker, in the stage both published targets below start from.
COPY --from=replay-gate /replay-gate/replay-runtime-checked /usr/local/share/rig/replay-runtime-checked
LABEL io.rig.rosbag2.replay="1"
LABEL io.rig.rosbag2.multi_input="1"
LABEL io.rig.replay.coordination="1"
LABEL io.rig.rosbag2.publication_provenance="1"
# The ros base image's entrypoint sources /opt/ros/<distro>/setup.bash — commands like
# `ros2 run rmw_zenoh_cpp rmw_zenohd` and the loggers' record.sh work as-is.

# Keep the familiar ROS CLI/common interfaces, but not the upstream ros-base
# IMAGE's compiler, colcon, rosdep and VCS layer. ROS Debian packages still carry
# some headers; removing those behind dpkg's back would break the base contract.
# Optional local SDK: docker build --target sdk -t fleet-ros-sdk base/
FROM runtime-deps AS sdk
ARG ROS_DISTRO
RUN apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
        build-essential cmake git openssh-client python3-colcon-common-extensions \
        ros-${ROS_DISTRO}-rosidl-default-generators \
    && rm -rf /var/lib/apt/lists/*

# The default/published target always excludes the SDK tools.
FROM runtime-deps AS runtime
