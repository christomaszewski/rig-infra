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
document (`plan: 2`, `multi_input: 1`, `remap`, `clock_only`, and `reindex`). Rig uses this to refuse an
unsupported launcher before starting deployment services.

## Multiple inputs and bag proxies

Version 1 remains supported for a single original session. Version 2 supplies an
exact input list and one aggregate timeline:

```json
{
  "version": 2,
  "source": "/data/runs/collected-run",
  "bags": [
    {"logger": "front", "session": "session-1", "relative_path": "bags/front/session-1", "storage": "mcap"},
    {"logger": "rear", "session": "session-2", "relative_path": "bags/rear/session-2", "storage": "sqlite3"}
  ],
  "timeline": {"start_ns": 100000000000, "duration_ns": 10000000000},
  "topics": ["/imu/data"],
  "remap": {},
  "services": [],
  "calls": null,
  "clock_only": false
}
```

Inputs are recording directories, including all their split files. Paths must be
canonical relative paths contained by `source`. The timeline starts at the earliest
input start and ends at the latest input end. A nonempty recording whose messages
share a single timestamp has a minimum 1 ns replay interval; metadata is not rewritten.
One native `ros2 bag play` process receives repeated `--input` arguments and publishes
one clock. Originals use received timestamps and retain duplicate messages. Window
restoration and service-call inspection/export use the same input set and origin.
The base image advertises `io.rig.rosbag2.multi_input=1` only after its build-time
runtime check verifies the required rosbag2 CLI and storage APIs.

A selected bag proxy supplies one input under
`exports/inputs/bags/PROFILE/ID/bag`, plus a top-level `proxy` object containing
`export_id`, `time_source`, and `deduplication`. The proxy already carries the chosen
schedule in its receive timestamps; runtime playback performs no deduplication or
sensor-time conversion. Configured `play.time_source`, if present, must agree with
this artifact. Without a proxy, only `received` is permitted.

`replay-export-bags REQUEST_JSON` invokes the offline exporter in the native local
player image, pinned to its inspected image ID. The request identifies exact input
directories, a disposable output staging directory, and the full conversion policy.
The original run is mounted read-only. The tool uses a disk-backed index, resolves
duplicates in original receive order, then sorts retained records by effective time.
It preserves serialized payloads and original send times, verifies the written bag,
and writes per-message receive/send/schedule and deduplication provenance. Rig verifies
source/output hashes and atomically publishes the immutable generation.

Profiles in `export-profiles/` combine arrival, middleware send, or sensor timing with
`off`, `conservative`, or `aggressive` deduplication. Conservative mode retains ordinary
equal-payload messages: typical ROS bags cannot prove their original publisher identity.
It can remove identical service events with matching client GID, sequence, event kind,
and event timestamp across inputs. Aggressive mode additionally collapses equal payloads
on the same topic/type within `dedup_window_ms`; it can remove legitimate repetitions
and must be explicitly selected. Both preserve repetitions within an individual input.

Sensor profiles use `header.stamp` by default and explicitly fall back to receive time
when unavailable. `play.sensor_timestamp_field`, `topic_timestamp_fields`,
`timestamp_fallback`, `max_clock_skew_s`, and `dedup_window_ms` refine export policy.
An incompatible clock domain fails conversion; no clock correction is inferred.
Original indexes must already be valid for export. Missing images are reported with
an acquisition instruction; this helper does not pull images or start replay services.

`tests/test_multi_input_runtime.py` and `tests/test_bag_proxy_runtime.py` exercise native
multi-input playback and proxy generation with temporary MCAP/SQLite bags. They skip
explicitly when the ROS runtime is unavailable.

`replay-reindex DERIVED_BAG_DIRECTORY` repairs ROS 2 MCAP/SQLite indexes in a copy
prepared by Rig. The helper uses an existing local image, disables networking and
image pulls, and bounds container lifetime. Never pass an original recording to
this maintenance verb.

Host tests use the existing `tests/test_*.py` scripts. `tests/test_plan_runtime.py`
also runs real ROS acceptance when `rclpy` and rosbag2 are installed. Run it in a
local ROS image with `RMW_IMPLEMENTATION=rmw_zenoh_cpp`; it creates its own isolated
router and tiny temporary bags. Without ROS, those runtime tests skip explicitly.

## Coordinated startup

A coordinated invocation binds a fresh control directory and supplies
`RIG_REPLAY_CONTROL_DIR`, `RIG_REPLAY_SESSION_ID`, `RIG_REPLAY_PLAN_ID`, and
`RIG_REPLAY_PARTICIPANT`. The `coordination` capability advertises protocol version 1
and the `player`, `calls`, and `readiness` components. The actual selected image must
carry `io.rig.replay.coordination=1`, verified by the base image's runtime checker.
Coordinated launch requires immutable runtime image pins and labels each created
container with its session and plan IDs.

The player opens the bag paused. Its clock stays on a private topic until the
coordinator arms the session. Arm permits a constant public clock and the window
latch restore; ordinary bag messages, recorded requests, scripted calls (including
timestamp zero), and advancing clock-only playback remain held. The readiness
participant checks the exact configured ROS consumer endpoints without calling
services. All participants acknowledge one future release proposal, then release
only after the matching commit. A missing/late commit, lost readiness, expired
startup deadline, foreign session/plan, or abort fails closed.

Participant status files include immutable `held_at_unix_s`, `ready_at_unix_s`,
`release_at_unix_s`, and `actual_release_at_unix_s` as each phase is reached.
Heartbeats continue through preparation, holding, and execution. Session control
files are owned by Rig; companion processes never invent or move a release time.
The old `RIG_REPLAY_START_AT_UNIX_S` path remains available for explicitly selected
legacy startup and does not provide this readiness contract.

The launcher also passes its resolved `play.rate` as `CALL_INJECTOR_RATE`; wall-clock
scripts multiply elapsed time from the committed origin by that rate. Their window
remains lower-inclusive and upper-exclusive, and startup time never shifts the origin.
