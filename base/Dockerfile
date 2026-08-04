# fleet-ros — the shared infra base image for rig fleets.
#
# One small (~1 GB) image that carries exactly what the infra tier needs to join and record the
# vehicle's ROS 2 graph: rmw_zenoh (router + sessions from the SAME distro packages — the
# router/session version-match holds by construction) and rosbag2 with mcap storage. The zenoh-router
# and ros2-bag-logger services default to this image, so a camera-less vehicle never pulls a driver
# image just to run its router and logger.
#
# Built + pushed by base/build.sh (the rig build contract); pulled by the composes as
# ${RIG_IMAGE_REGISTRY}/fleet-ros:${RIG_IMAGE_TAG} — certify enforces the build/pull tag agreement.
ARG ROS_DISTRO=lyrical
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-${ROS_DISTRO}-rmw-zenoh-cpp \
        ros-${ROS_DISTRO}-rosbag2 \
        ros-${ROS_DISTRO}-rosbag2-storage-mcap \
    && rm -rf /var/lib/apt/lists/*
# The ros base image's entrypoint sources /opt/ros/<distro>/setup.bash — commands like
# `ros2 run rmw_zenoh_cpp rmw_zenohd` and the loggers' record.sh work as-is.
