# fleet-ros — the shared infra base image for rig fleets, and the DEPLOYMENT's base image.
#
# One small (~1 GB) image that carries exactly what the infra tier needs to join and record the
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
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
# The rmw to install, as its ROS name; build.sh sources it from vehicle.yaml `ros.rmw` (rig exports
# it as RIG_ROS_RMW, rig >= v0.2.23). The apt package is ros-<distro>-<name, '_' -> '-'> — exactly
# the mapping `rig image audit` uses to check the declared rmw is installed, so builder and checker
# agree by construction. The default only applies outside rig; note that a non-zenoh value yields an
# image with no rmw_zenohd in it, which zenoh-router cannot run on (see build.sh and the README).
ARG RMW_IMPLEMENTATION=rmw_zenoh_cpp
RUN rmw_pkg="ros-${ROS_DISTRO}-$(echo "${RMW_IMPLEMENTATION}" | tr '_' '-')" \
    && apt-get update && apt-get install -y --no-install-recommends \
        "${rmw_pkg}" \
        ros-${ROS_DISTRO}-rosbag2 \
        ros-${ROS_DISTRO}-rosbag2-storage-mcap \
    && rm -rf /var/lib/apt/lists/*
# The ros base image's entrypoint sources /opt/ros/<distro>/setup.bash — commands like
# `ros2 run rmw_zenoh_cpp rmw_zenohd` and the loggers' record.sh work as-is.
