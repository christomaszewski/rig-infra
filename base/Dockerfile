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
FROM ros:${ROS_DISTRO}-ros-core AS runtime-deps
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
        python3-yaml \
        ros-${ROS_DISTRO}-ros-base \
        "${rmw_pkg}" \
        ros-${ROS_DISTRO}-rosbag2 \
        ros-${ROS_DISTRO}-rosbag2-storage-mcap \
    && rm -rf /var/lib/apt/lists/*
COPY check_replay_runtime.py /usr/local/lib/rig/check_replay_runtime.py
RUN . /opt/ros/${ROS_DISTRO}/setup.sh \
    && python3 /usr/local/lib/rig/check_replay_runtime.py
# Advertise only after the installed runtime passes the player API/storage contract.
LABEL io.rig.rosbag2.replay="1"
LABEL io.rig.replay.coordination="1"
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
