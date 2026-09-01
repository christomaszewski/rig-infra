# syntax=docker/dockerfile:1
# check=skip=InvalidDefaultArgInFrom
# (^ parser directives, contiguous at the top by requirement. syntax: the >=1.2 frontend for the
# builder RUN's --mount=type=ssh. check-skip: BASE_IMAGE has no default ON PURPOSE — build-msgs.sh
# always passes it, and a hand-run `docker build` without it must fail LOUDLY rather than silently
# compile against some default; the base image is the fleet's version authority.)
# fleet-ros-msgs — the deployment's interface-package overlay on its base image.
#
# WHY: the ros2 bag logger records raw CDR and never deserializes, but Lyrical rosbag2 still refuses
# any topic whose message package is not installed ("Topic ... has unknown type ... Only topics with
# known type are supported") — the recorder needs the typesupport .so to create the subscription and
# the .msg/.idl sources to embed the mcap schema. Verified live against fleet-ros v1.3.0 / rosbag2
# 0.33.3: the REP 2011 type-description plumbing ships in the distro but rosbag2 does not use it.
# So a fleet with custom types needs ONE thin image = base + every interface package the fleet's
# services publish. That is this image: interface packages ONLY, never driver stacks.
#
# INPUT: msgs-manifest.yaml (staged into the context by build-msgs.sh) — the UNION of the `msgs:`
# blocks declared by the deployment's service riggings (see msgs-manifest.example.yaml for the
# schema). `apt:` entries install dpkg-tracked into /opt/ros/<distro>; `source:` entries are built in
# a throwaway builder stage and land in /opt/fleet-msgs (merge-install).
#
# TWO RECORDS BAKED, two different consumers — keep them separate:
#   /opt/fleet-msgs/manifest.yaml    the DECLARED union, verbatim (COPYd below) — rig >= v0.2.29's
#                                    stale-overlay check diffs it against the current riggings.
#   /opt/fleet-msgs/provenance.yaml  what the build RESOLVED: per source repo, the declared ref plus
#                                    the commit SHA actually checked out (written by build_msgs.py in
#                                    the builder stage; rides the prefix COPY below). The service-side
#                                    half of the same convention lives in provenance-record.sh /
#                                    provenance.example.yaml; `rig image audit` (>= v0.2.30) compares
#                                    the two to catch pin skew symbolic refs cannot show.
#
# ENV BAKED IN: AMENT_PREFIX_PATH=/opt/fleet-msgs + LD_LIBRARY_PATH=/opt/fleet-msgs/lib. The ros
# entrypoint's `source /opt/ros/<distro>/setup.bash` PREPENDS the distro prefix to whatever is
# already in the environment, so at runtime both prefixes are searched — record.sh and the loggers'
# composes need no changes to pick the overlay up. (That is everything a C++ node — the recorder —
# needs. PYTHONPATH is deliberately NOT baked (its site-packages path embeds the python minor
# version): a PYTHON node that wants a source-built overlay type, e.g. `ros2 topic pub` for
# debugging, must `source /opt/fleet-msgs/setup.bash` first or it fails with "The passed message
# type is invalid". apt-installed entries land in /opt/ros/<distro> and have no such wrinkle.)
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS builder
ARG ROS_DISTRO=lyrical
# --no-upgrade: same version-authority doctrine as base/Dockerfile — the base image is the fleet's
# version authority, and generated code must be compiled against ITS rosidl runtime, so base-carried
# packages (rosidl generators' deps among them) are held at the base's versions; only what the base
# lacks (the toolchain) installs fresh.
RUN apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
        git openssh-client build-essential cmake ros-dev-tools \
        ros-${ROS_DISTRO}-rosidl-default-generators \
    && rm -rf /var/lib/apt/lists/*
COPY msgs-manifest.yaml /msgs/manifest.yaml
COPY build_msgs.py /msgs/build_msgs.py
# --mount=type=ssh forwards the INVOKING machine's ssh-agent into THIS RUN only — keys never
# enter a layer or the image history. build-msgs.sh passes `--ssh default` iff the manifest
# declares ssh-form repos (git@… / ssh://…); the mount is optional-by-default, so https-only
# fleets (and agent-less CI) build exactly as before. accept-new: a fresh container has no
# known_hosts, and the build's provenance record pins the resolved SHA anyway — TOFU is the
# right posture here.
RUN --mount=type=ssh . /opt/ros/${ROS_DISTRO}/setup.sh \
    && GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" \
       python3 /msgs/build_msgs.py /msgs/manifest.yaml /opt/fleet-msgs

FROM ${BASE_IMAGE}
ARG ROS_DISTRO=lyrical
COPY msgs-manifest.yaml /opt/fleet-msgs/manifest.yaml
# apt entries: ROS package names mapped ros-<distro>-<name, '_' -> '-'> — the same mapping
# base/build.sh uses for the rmw, so a future audit check agrees with the builder by construction.
# --no-upgrade: the base image stays the version authority (see base/Dockerfile).
RUN pkgs="$(python3 -c "import os, yaml; m = yaml.safe_load(open('/opt/fleet-msgs/manifest.yaml')) or {}; d = os.environ['ROS_DISTRO']; print(' '.join(sorted('ros-' + d + '-' + p.replace('_', '-') for p in (m.get('apt') or []))))")" \
    && if [ -n "$pkgs" ]; then \
        apt-get update && apt-get install -y --no-install-recommends --no-upgrade $pkgs \
        && rm -rf /var/lib/apt/lists/*; \
    fi
COPY --from=builder /opt/fleet-msgs /opt/fleet-msgs
ENV AMENT_PREFIX_PATH=/opt/fleet-msgs \
    LD_LIBRARY_PATH=/opt/fleet-msgs/lib
