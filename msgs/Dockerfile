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
# a throwaway builder stage and land in /opt/fleet-msgs (merge-install). Optional second input: the
# `local-sources` named context (opt-in local checkouts that replace `source:` clones; see below).
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
# Install declared apt interfaces once, in the common parent of both stages.
# Source interfaces can depend on these packages while generating typesupport.
FROM ${BASE_IMAGE} AS interfaces
ARG ROS_DISTRO=lyrical
COPY msgs-manifest.yaml /opt/fleet-msgs/manifest.yaml
RUN pkgs="$(python3 -c "import os, yaml; m = yaml.safe_load(open('/opt/fleet-msgs/manifest.yaml')) or {}; d = os.environ['ROS_DISTRO']; print(' '.join(sorted('ros-' + d + '-' + p.replace('_', '-') for p in (m.get('apt') or []))))")" \
    && if [ -n "$pkgs" ]; then \
        apt-get update && apt-get install -y --no-install-recommends --no-upgrade $pkgs \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Host-side sources (build-msgs.sh): snapshots of working trees (the FLEET_MSGS_LOCAL dev loop) and
# rig's exports of the declared pins from its local git mirrors (RIG_BUILD_SOURCES — what makes
# `rig build --offline` work) arrive as the `local-sources` named build context, which REPLACES this
# stage; without one (a standalone build, or any hand-run `docker build`) it stays empty and
# build_msgs.py clones every source repo at its pin.
FROM scratch AS local-sources

FROM interfaces AS builder
ARG ROS_DISTRO
# An apt-only overlay needs no compiler or repository tooling. Source overlays
# use explicit generator/build tools instead of the broad ros-dev-tools bundle.
RUN if python3 -c "import yaml, sys; sys.exit(not bool((yaml.safe_load(open('/opt/fleet-msgs/manifest.yaml')) or {}).get('source')))"; then \
        apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
            git openssh-client build-essential cmake python3-colcon-common-extensions \
            ros-${ROS_DISTRO}-rosidl-default-generators \
        && rm -rf /var/lib/apt/lists/*; \
    fi
COPY build_msgs.py /msgs/build_msgs.py
# Agent forwarding is temporary; keys never enter an image layer. The local snapshots are a
# read-only bind for the same reason: build_msgs.py copies what it builds into the throwaway stage.
RUN --mount=type=ssh --mount=type=bind,from=local-sources,target=/msgs/local \
    . /opt/ros/${ROS_DISTRO}/setup.sh \
    && GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" \
       python3 /msgs/build_msgs.py /opt/fleet-msgs/manifest.yaml /opt/fleet-msgs /msgs/local

FROM interfaces AS runtime
COPY --from=builder /opt/fleet-msgs /opt/fleet-msgs
ENV AMENT_PREFIX_PATH=/opt/fleet-msgs \
    LD_LIBRARY_PATH=/opt/fleet-msgs/lib
