# rig-infra — shared infra services for rig fleets

Ready-to-use shared (`infra:`) services for [rig](https://github.com/christomaszewski/rig) deployments,
plus the **`fleet-ros` base image** they default to. Each service dir is an ordinary rig-compatible
service: a launcher + `rigging.yaml` meeting the launcher contract (`rig certify` runs against every
one in CI).

- **`zenoh-router/`** — the vehicle's shared `rmw_zenoh` router (infra, order 0). Default: `fleet-ros`
  running `ros2 run rmw_zenoh_cpp rmw_zenohd` — the router and the sessions share one distro's zenoh
  packages by construction. On the default image it **requires `ros.rmw: rmw_zenoh_cpp`**; see
  [The router and the fleet's rmw](#the-router-and-the-fleets-rmw). Optional inline `router_config:`
  reaches rmw_zenohd as
  `ZENOH_CONFIG_OVERRIDE` pairs on top of its shipped router defaults (and as a mounted
  `zenohd.json5` for the standalone `zenohd -c` path).
- **`ros2-bag-logger/`** — records the ROS 2 telemetry graph to `${RIG_DATA_DIR}` (run-aware:
  `current/bags/<name>`), zstd-compressed mcap by default. The recorder node name is pinned, so
  recording can be gated at runtime through rosbag2's own services
  (`/bag_logger/{pause,resume,split_bagfile,stop,…}`); trigger *policy* (arm/disarm, geofence)
  belongs in a separate node that calls them — see the example config. Default image: `fleet-ros`
  (rosbag2 + mcap + rmw_zenoh, ~1 GB — no camera image needed on camera-less vehicles).
- **`ros1-bag-logger/`** — the ROS 1 sibling (`rosbag record`), for ROS 1 fleets with a roscore.
- **`base/`** — the `fleet-ros` image: `ros:<distro>-ros-base` + the fleet's rmw + `rosbag2` (+ mcap).
  `base/build.sh <registry> [tag]` follows the rig build contract; the router and ros2 bag logger
  riggings declare it (`build: { command: ../base/build.sh, images: [fleet-ros], provides: base }`),
  so `rig build` builds + pushes it and certify enforces the compose pulls the same tag. Both the
  distro and the rmw come from vehicle.yaml — `ros.distro` as `ROS_DISTRO` (rig ≥ v0.1.29; a doctor
  ERROR flags a vehicle whose services target a different distro) and `ros.rmw` as `RIG_ROS_RMW`
  (rig ≥ v0.2.23). `provides: base` makes fleet-ros the whole deployment's base image — see below.
- **`msgs/`** — the `fleet-ros-msgs` interface overlay: the base image + the custom message packages
  the fleet's services publish, which the bag logger must have installed to record their topics.
  Built from the union of the services' `msgs:` declarations — see
  [Custom message types](#custom-message-types--the-fleet-ros-msgs-overlay).

## The deployment's base image (`RIG_BASE_IMAGE`, rig ≥ v0.2.23)

Two images in one deployment that `apt-get install` the same ROS packages at different times drift to
different versions — and a skewed `rmw_zenoh_cpp` means zenoh sessions that cannot talk. rig prevents
that with ONE base image per deployment; rig-infra owns `fleet-ros`, so it is the provider side of
that contract.

- **Declared.** `zenoh-router` and `ros2-bag-logger` both carry
  `build: { command: ../base/build.sh, images: [fleet-ros], provides: base }` — `images[0]` names the
  base image. `rig build` builds it FIRST and exports its composed ref
  (`<registry>/fleet-ros:<tag>`) to every other build and to every launcher as `RIG_BASE_IMAGE`, so
  one image pins the fleet's distro+rmw packages. The declaration is kept on **both** riggings so
  that either service alone still supplies a fleet's base; they name the same image from the same
  script, so rig treats them as one base and builds it once.
- **Providers must agree.** Agreeing on the image *name* is not agreeing on the base. Providers of
  one base that declare a different name, a different `build.platforms`, or a different build script
  are an ERROR in `rig doctor` and/or `rig build` — never a manifest-order guess (the last one would
  have two builds racing for one tag). Practical consequence here: if fleet-ros ever needs a
  `platforms:` matrix (an L4T/CUDA base, say), it has to be added to **both** provider riggings in
  the same change.
- **Consumed.** The composes that RUN the base resolve their image as
  `${<SVC>_IMAGE:-${RIG_BASE_IMAGE:-<registry>/fleet-ros:<tag>}}` — operator override, then the
  deployment's base, then the composed pull ref. That last branch matters: `rig certify` runs with
  `RIG_BASE_IMAGE` deliberately UNSET (it is optional, and launchers must carry a fallback), so the
  fallback is what its registry/tag build-pull agreement checks see, and what a standalone `docker
  compose` gets. A service that builds its own image should build `FROM ${RIG_BASE_IMAGE}`.
- **Overridden.** `vehicle.yaml images.base` (or `rig build --base-image REF`) overrides the provider
  with an external ref, used verbatim. rig then skips the fleet-ros build — nothing would pull it —
  and the stacks run that ref, so it has to carry the fleet's `ros.distro`, its declared rmw, and
  rosbag2. fleet-ros is never re-parented onto it: it is the ROOT of the chain, always built `FROM
  ros:<distro>-ros-base`, and rig pops `RIG_BASE_IMAGE` for that stage-0 build.
- **Built for the fleet's rmw.** vehicle.yaml `ros.rmw` reaches `base/build.sh` as `RIG_ROS_RMW`
  (rig-owned and set-or-popped — deliberately not the conventional `RMW_IMPLEMENTATION`, which most
  ROS shells export), and the image installs `ros-<distro>-<rmw, '_' → '-'>`. That is the same
  mapping `rig image audit` uses to check it, so the builder and the checker agree by construction.
  `FLEET_ROS_RMW` is the same choice for standalone builds outside rig, where nothing sets
  `RIG_ROS_RMW`; rig's var wins when both are set, and the default is `rmw_zenoh_cpp`.
- **Checked, and repaired.** `rig image audit` inspects what the stacks actually resolve to (one
  distro, the declared rmw installed, `ros-*` versions agreeing across images); `rig build --no-cache`
  re-converges a fleet that already drifted, and `base/build.sh` opts into it via
  `RIG_BUILD_NO_CACHE`. A one-base deployment resolves to a single ROS image, and audit says so
  explicitly — nothing to skew against is the contract working, not a check that didn't run.

### The router and the fleet's rmw

Since the base installs the rmw the vehicle *declares*, a `rmw_cyclonedds_cpp` fleet gets a fleet-ros
with no zenoh in it — while `zenoh-router`'s compose runs `ros2 run rmw_zenoh_cpp rmw_zenohd` out of
that same image. It builds clean and dies on `up`. rig ≥ v0.2.23 WARNs at doctor time on an *enabled*
zenoh router under a non-zenoh `ros.rmw`.

**The rule: on the default fleet-ros image, `zenoh-router` requires `ros.rmw: rmw_zenoh_cpp`.** That
is not a limitation to work around — a zenoh router serves rmw_zenoh sessions, so a DDS fleet running
one has a misconfiguration, and the honest fix is to disable the router or switch the rmw.

We deliberately do **not** install zenoh unconditionally alongside the declared rmw. It would cost
image size on every DDS fleet, but the real objection is that it converts a loud failure into a
silent one: the router would start, join nothing, and route nothing, on a fleet where no session
speaks zenoh. A container that crash-loops with `Package 'rmw_zenoh_cpp' not found` is a better
diagnostic than one that sits green in `rig status` doing nothing.

A fleet that genuinely wants a zenoh router without `rmw_zenoh_cpp` sessions already has a supported
path that never touches the base image: the standalone `eclipse/zenoh` route documented in
`zenoh-router/rigging.yaml` and `docker/compose.deploy.yaml` — point `ZENOH_ROUTER_IMAGE` at a pinned
`eclipse/zenoh`, comment out the `command:`, and swap the `build:` for a `mirror:`. That is the
configuration to reach for; the doctor WARN is the preflight that sends you here.

## Custom message types — the `fleet-ros-msgs` overlay

rosbag2 records raw CDR and never deserializes — but it still **refuses any topic whose message
package is not installed** ("Topic … has unknown type … Only topics with known type are supported"):
the recorder needs the typesupport library to create its subscription and the `.msg`/`.idl` sources
to embed the mcap schema. Verified live against fleet-ros v1.3.0 / rosbag2 0.33.3; the REP 2011
type-description plumbing ships in the distro but rosbag2 does not use it. The failure is **quiet**:
in `all`/`exclude` mode the recorder logs one WARN, skips the topic, and the bag silently lacks it.
Everything in ros-base/common_interfaces (`sensor_msgs`, `geometry_msgs`, `nav_msgs`, `tf2_msgs`, …)
is covered by the base image; anything custom (`px4_msgs`, vendor msgs, your own packages) is not.

The fix is one thin image, never a fat one: `fleet-ros-msgs` = the deployment's base + the fleet's
*interface packages only* (tens of MB, not a 3 GB driver image).

- **Declared, per service.** A service that publishes custom types carries a top-level `msgs:` block
  in its rigging.yaml naming the interface packages — `apt:` for distro-released ones, `source:`
  (repo + **mandatory** ref pin + packages) for source-built ones. Schema and rules:
  `msgs/msgs-manifest.example.yaml`. Current rig ignores unknown top-level keys, so the block is
  inert (and self-documenting) until rig aggregates it.
- **Built.** `msgs/build-msgs.sh <registry> [tag]` (the rig build contract shape) builds
  `<registry>/fleet-ros-msgs:<tag>` `FROM ${RIG_BASE_IMAGE}` out of the **union manifest** the env
  points at (`RIG_MSGS_MANIFEST`, or `FLEET_MSGS_MANIFEST` standalone — same schema as the
  per-service block). Until rig renders the union itself, author it by hand in the deployment
  (e.g. `config/msgs.yaml`). An empty manifest is refused; the same repo at two different refs is a
  refusal, not a manifest-order guess. The manifest is baked at `/opt/fleet-msgs/manifest.yaml` as
  provenance for a future `rig image audit` check.
- **Consumed.** The ros2 bag logger's compose resolves
  `BAG_LOGGER_IMAGE → RIG_MSGS_IMAGE → RIG_BASE_IMAGE → composed fleet-ros ref`. The moment an
  overlay exists and `RIG_MSGS_IMAGE` names it, the logger records the fleet's custom types — no
  config change. rig does not export `RIG_MSGS_IMAGE` yet; until it does, export it yourself (or
  set `BAG_LOGGER_IMAGE`). With no overlay, everything degrades to the bare base, which is correct.
- **Pin discipline.** A `source:` ref that drifts from what the declaring service builds against
  means the overlay's definitions are wire-incompatible with what the service publishes — and the
  failure (schema mismatch in the bag) is silent. The pin in the `msgs:` block must move with the
  service's own pin, in the same change.
- **ROS 1 is exempt.** `rosbag record` embeds message definitions from the connection headers on the
  wire — `ros1-bag-logger` needs none of this.

The rig-side aggregation (union rendering, `provides`-style role, `RIG_MSGS_IMAGE` export) is queued
as a rig feature; the handoff spec lives at `../rig-msgs-image-handoff.md` in the parent workspace.

Use from a rig deployment (clone as a sibling):

```yaml
# services.yaml
services:
  zenoh-router:    { path: ../rig-infra/zenoh-router }
  ros2-bag-logger: { path: ../rig-infra/ros2-bag-logger }
```

or scaffold directly: `rig init my-vehicle --infra zenoh-router --infra ros2-bag-logger` (bare names
resolve by scanning the workspace) — wired + enabled, router pinned to order 0.
