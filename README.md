# rig-infra — shared infra services for rig fleets

Ready-to-use shared (`infra:`) services for [rig](https://github.com/christomaszewski/rig) deployments,
plus the **`fleet-ros` base image** they default to. Each service dir is an ordinary rig-compatible
service: a launcher + `rigging.yaml` meeting the launcher contract (`rig certify` runs against every
one in CI).

- **`zenoh-router/`** — the vehicle's shared `rmw_zenoh` router (infra, order 0). Default: `fleet-ros`
  running `ros2 run rmw_zenoh_cpp rmw_zenohd` — the router and the sessions share one distro's zenoh
  packages by construction. Optional inline `router_config:` renders to a mounted `zenohd.json5`.
- **`ros2-bag-logger/`** — records the ROS 2 telemetry graph to `${RIG_DATA_DIR}` (run-aware:
  `current/bags/<name>`), zstd-compressed mcap by default. The recorder node name is pinned, so
  recording can be gated at runtime through rosbag2's own services
  (`/bag_logger/{pause,resume,split_bagfile,stop,…}`); trigger *policy* (arm/disarm, geofence)
  belongs in a separate node that calls them — see the example config. Default image: `fleet-ros`
  (rosbag2 + mcap + rmw_zenoh, ~1 GB — no camera image needed on camera-less vehicles).
- **`ros1-bag-logger/`** — the ROS 1 sibling (`rosbag record`), for ROS 1 fleets with a roscore.
- **`base/`** — the `fleet-ros` image: `ros:<distro>-ros-base` + `rmw-zenoh-cpp` + `rosbag2` (+ mcap).
  `base/build.sh <registry> [tag]` follows the rig build contract; the router and ros2 bag logger
  riggings declare it (`build: { command: ../base/build.sh, images: [fleet-ros] }`), so `rig build`
  builds + pushes it and certify enforces the compose pulls the same tag. The distro comes from
  vehicle.yaml's `ros.distro` (rig ≥ v0.1.29 exports it as `ROS_DISTRO`; a doctor ERROR flags a
  vehicle whose services target a different distro).

Use from a rig deployment (clone as a sibling):

```yaml
# services.yaml
services:
  zenoh-router:    { path: ../rig-infra/zenoh-router }
  ros2-bag-logger: { path: ../rig-infra/ros2-bag-logger }
```

or scaffold directly: `rig init my-vehicle --infra zenoh-router --infra ros2-bag-logger` (bare names
resolve by scanning the workspace) — wired + enabled, router pinned to order 0.
