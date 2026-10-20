# Rig replay plan contract

The launcher retains standalone config and legacy environment support. New Rig replay
workflows pass `RIG_REPLAY_PLAN`, an absolute host path to a versioned JSON document:

```json
{
  "version": 1,
  "source": "/deployment/var/replay/example/generation/source",
  "bag": {
    "logger": "bag_logger",
    "session": "exact-session",
    "start_ns": 100000000000,
    "duration_ns": 10000000000
  },
  "topics": ["/old/input"],
  "remap": {"/old/input": "/new/input"},
  "services": [],
  "calls": null,
  "clock_only": false
}
```

The source uses the normal `bags/LOGGER/SESSION/` layout. Rig resolves `latest` before
handoff. Timing fields, when present, must agree with the session's current metadata.
Topic names are exact absolute ROS names. Remaps apply to normal playback and the
pre-window latched-message helper.

The plan replaces standalone topic, exclusion, and service/call-script selection.
Conflicting legacy selector environment values fail rather than merging silently.
An empty data selection with no verbatim services runs a clock-only timeline; it
never falls back to playing every topic. `RIG_SIM_TIME` controls clock publication.
Windows, playback rate, looping, and the common release gate also apply to this
timeline. A supplied call script runs alongside it.

The optional `replay-capabilities` launcher verb prints a read-only capability
document (`plan: 1`, `remap`, `clock_only`, and `reindex`). Rig uses this to refuse an
unsupported launcher before starting deployment services.

`replay-reindex DERIVED_BAG_DIRECTORY` repairs ROS 2 MCAP/SQLite indexes in a copy
prepared by Rig. The helper uses an existing local image, disables networking and
image pulls, and bounds container lifetime. Never pass an original recording to
this maintenance verb.

Host tests use the existing `tests/test_*.py` scripts. `tests/test_plan_runtime.py`
also runs real ROS acceptance when `rclpy` and rosbag2 are installed. Run it in a
local ROS image with `RMW_IMPLEMENTATION=rmw_zenoh_cpp`; it creates its own isolated
router and tiny temporary bags. Without ROS, those runtime tests skip explicitly.
