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
  (rosbag2 + mcap + rmw_zenoh, ~1 GB — no camera image needed on camera-less vehicles). A
  `graph:` block in the config additionally starts the **graph-snapshotter sidecar**, recording
  the live graph *topology* (who talked to what) beside the bags — see
  [Graph topology capture](#graph-topology-capture--the-graph-snapshotter-sidecar).
- **`ros2-bag-player/`** — the SIL replay player (`autonomy` tier): joins the graph like any
  service and plays a selected topic subset from a SOURCE run's bags, mounted read-only. It never
  decides what to play — `rig replay` (or the standalone config) hands it the set. An exited
  player is a finished replay, not a failure. See
  [SIL replay](#sil-replay--the-ros2-bag-player-service).
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
  `msgs/msgs-manifest.example.yaml`. rig >= v0.2.28 validates the block strictly and aggregates it
  fleet-wide; older rigs ignore unknown top-level keys, so on them the block is inert (and
  self-documenting).
- **Built.** `msgs/build-msgs.sh <registry> [tag]` (the rig build contract shape) builds
  `<registry>/fleet-ros-msgs:<tag>` `FROM ${RIG_BASE_IMAGE}` out of the **union manifest** the env
  points at (`RIG_MSGS_MANIFEST`, or `FLEET_MSGS_MANIFEST` standalone — same schema as the
  per-service block). rig >= v0.2.28 renders the union itself and runs this build right after the
  base stage (the zenoh-router + ros2-bag-logger riggings declare it as `build.msgs_overlay`);
  standalone, author the union by hand in the deployment (e.g. `config/msgs.yaml`). An empty
  manifest is refused; the same repo at two different refs is a refusal, not a manifest-order
  guess. The manifest is baked at `/opt/fleet-msgs/manifest.yaml` as provenance for a future
  `rig image audit` check.
- **Consumed.** The ros2 bag logger's compose resolves
  `BAG_LOGGER_IMAGE → RIG_MSGS_IMAGE → RIG_BASE_IMAGE → composed fleet-ros ref`. The moment an
  overlay exists and `RIG_MSGS_IMAGE` names it, the logger records the fleet's custom types — no
  config change. rig >= v0.2.28 exports `RIG_MSGS_IMAGE` whenever the fleet's riggings declare
  `msgs:` blocks; on older rigs or standalone, export it yourself (or set `BAG_LOGGER_IMAGE`).
  With no overlay, everything degrades to the bare base, which is correct.
- **Pin discipline.** A `source:` ref that drifts from what the declaring service builds against
  means the overlay's definitions are wire-incompatible with what the service publishes — and the
  failure (schema mismatch in the bag) is silent. The pin in the `msgs:` block must move with the
  service's own pin, in the same change.
- **Provenance — pin skew made detectable.** Every image that *builds* declared interface repos
  bakes `/opt/fleet-msgs/provenance.yaml`: per repo, the ref the checkout actually used and the
  commit SHA it resolved to (a symbolic ref is not content identity — a moved tag or a re-built
  branch is a different tree under the same name). The overlay writes it automatically
  (`build_msgs.py`); service Dockerfiles use the copyable `msgs/provenance-record.sh` right after
  each checkout — and multi-stage builds must `COPY` the file into the final image. Two rules:
  write the *truth* (the very variable that drove the checkout, never a re-echo of the rigging
  value — that makes the check circular), and record `rev: unknown` explicitly for vendored
  snapshots (unverifiable beats invisible). apt-installed interface packages need nothing — dpkg is
  their record. Schema + doctrine: `msgs/provenance.example.yaml`; consumed by `rig image audit`
  (rig ≥ v0.2.30: absent file WARNs, ref mismatch ERRORs, same-ref-different-SHA ERRORs).
- **ROS 1 is exempt.** `rosbag record` embeds message definitions from the connection headers on the
  wire — `ros1-bag-logger` needs none of this.

The rig-side aggregation shipped in rig v0.2.28 (`msgs:` union + the `build.msgs_overlay` trigger +
`RIG_MSGS_IMAGE` export; doctor WARNs when `msgs:` is declared with no overlay mechanism wired).
The contract handoff lives at `../rig-msgs-image-handoff.md` in the parent workspace.

## SIL replay — the ros2-bag-player service

A sealed run dir holds everything a software-in-the-loop test needs — rendered configs, pins,
mcap bags recorded under the msgs overlay, graph epochs saying who consumed what — but nothing
feeds that data back through an updated autonomy service. `ros2-bag-player` is that piece: a
normal rig service (a launcher + rigging, certified like the rest) that joins the graph and plays
a **selected topic subset** from a source run's bags. Division of labor, on purpose: rig owns run
selection, topic-set computation, and the safety guards; this service owns only the playback
mechanics. The player never decides what to play. Replay is *current code against old data* —
tag pins, not digests; never bit-exact reproduction.

**The selector env contract** (`rig replay`, rig ≥ v0.2.33, exports these; all absent under every
other verb):

- `RIG_REPLAY_SOURCE` — absolute host path to the source run, mounted **read-only** at `/replay`
  (verified: the player cannot write it). This var is **fleet-general**, not player-private: rig
  sends it to every launcher in a replay up-set, and a future per-sensor replay source (e.g.
  camera-service replaying its own recordings, paced against the same clock) consumes the same
  var the same way — this player is merely its first consumer. Standalone use sets the config's
  `source.run` instead.
- `RIG_REPLAY_TOPICS` (space-separated allow-list, from graph epochs) **xor**
  `RIG_REPLAY_EXCLUDE` (one regex, the pre-epoch namespace fallback). Exactly one is ever set;
  the launcher refuses both-set as defense in depth. Either shadows the config's `play.topics`
  (WARN). The config's `play.exclude` regexes apply in both modes — they *filter* the allow-list
  before `--topics` (an emptied selection is refused: a bare `ros2 bag play` would play
  everything), or merge into the exclude alternation. Topics named but absent from the bag are
  tolerated (rig's selector may include topics the bag never captured).
- `RIG_SIM_TIME=1` → the player adds `--clock` (verified: 40 Hz `/clock` by default, and sim
  time scales with `play.rate` — rate 2.0 advances sim exactly 2× wall). There is **no**
  `play.clock` config knob and one is refused at parse: clock coherence is one rig-owned token
  with two consumers (`--clock` here, `use_sim_time` in the services under test) — the
  incoherent state is unrepresentable.

Playback knobs (`rate`, `loop`, `start_offset_s`, `start_paused`) live in the **config**, not rig
CLI flags, so rig's run snapshot records every experiment's parameters — the run self-documents.
Session resolution is host-side at render (the source run is static, unlike the logger's live
`current`): `session: latest` picks the greatest stamp, multiple sessions WARN naming the skipped
ones, and a recording split into many files plays as **one** session (verified across a 4-way
split).

**Ordering is load-bearing.** The rigging says `tier: autonomy` and the vehicle.yaml row takes a
high `order` (rig replay declares it `enabled: false`, order ~999 — explicit names win at
dispatch, so a normal `rig up` never starts it): the player comes up **last**, after the services
under test, so their subscriptions are standing before data flows — volatile durability would
silently drop the head of the bag — and goes down **first**, so consumers are never stopped while
still being fed.

**Exit semantics.** `restart: "no"` (the logger's `unless-stopped` would loop the bag
invisibly): the bag ends → the container exits 0 → the replay is **finished**, not failed. A
finished player disappears from plain `status` (`ps` hides exited containers) — `status -a`
shows the `Exited (0)` row. `loop: true` is for soak only and the launcher WARNs: `down` becomes
the only exit, and `/clock` jumps backwards at every wrap (verified — TF and anything stateful
will object).

**Standalone invocation** (no rig — this is also the SIL path before rig v0.2.33 lands):

```bash
ROS_DOMAIN_ID=7 RMW_IMPLEMENTATION=rmw_zenoh_cpp BAG_PLAYER_IMAGE=<registry>/fleet-ros-msgs:<tag> \
  ./ros2-bag-player/ros2-bag-player-up my-replay.yaml up -d
```

with `source.run` set to the run dir's absolute path and `play.topics`/`play.exclude` selecting;
or hand-export the `RIG_REPLAY_*` vars to exercise the rig channel.

Findings worth knowing (verified on lyrical): recorded QoS restores durability on play — a
subscriber joining mid-replay still receives `/tf_static`-style transient-local messages, even
from a split recording; but `start_offset_s` **skips** latched messages recorded before the
offset (the same gap that motivated the logger's `repeat_transient_local` knob — recording with
it narrows the loss to the current split). Player memory is dominated by rosbag2's read-ahead
queue (1000 messages): ~1 GB on a bag of ~1 MB messages, trivial on telemetry-sized ones; a
900 MB mcap starts feeding subscribers ~2.7 s after `up` (dev-box numbers).

## Graph topology capture — the graph-snapshotter sidecar

Bags record data, not topology: rosbag2 keeps topic names and types but no node identity, nothing
about subscribers, and no services — so a run's bag cannot answer *who talked to what*, and in
particular a service's **inputs** are invisible from its own run (which is exactly what
`rig replay` needs). The live graph API is the instrument: a `graph:` block in the ros2 bag
logger's config starts a second, compose-profile-gated container beside the recorder that walks
the graph every `interval_s` (via `get_node_names_and_namespaces` + the four per-node NodeGraph
calls — works under rmw_zenoh, whose graph cache rides liveliness tokens) and records per-node
**pubs / subs / service servers (`provides:`) / clients (`requires:`)**, with a type on every
edge.

**The artifact — append-only, change-deduped epochs.** One YAML file per distinct graph state at
`<run>/graph/<name>/epoch_<UTCstamp>.yaml` (the pinned open run; flat `<data>/graph/<name>`
without a run registry — mirrors `bags/`), carrying its validity window:

- Each tick the observed graph is canonicalized and hashed (the hash covers the `nodes:` body
  only). Unchanged → the SAME file is atomically rewritten with `last:` bumped — the liveness
  signal; a crash loses at most one interval. Changed → a NEW file opens (`first:` = now) and the
  old one is never touched again, so a flapping node *looks different* from a stable one.
- A sidecar (re)start always opens a new epoch — no file is ever read back, trusted, or resumed.
  Overlapping epochs are harmless: readers dedup by content.
- **Self-exclusion only.** The snapshotter drops its own node (a measurement artifact) and
  nothing else. Notably the bag recorder itself IS recorded, as the graph sees it: subscriptions
  to everything it records, its control services (`/<node>/pause`, …), and its
  `/events/rosbag2_messages_lost` / `/events/write_split` publishers. Writer dumb, reader smart:
  no filtering, no namespace→instance grouping — rig derives every view (union across epochs,
  instance grouping, declared-vs-observed checks) at read time (`rig graph`, rig ≥ v0.2.32).

**Enabling.** Uncomment `graph:` in the example config (`enabled: true`; `interval_s`, `settle_s`
knobs documented there). The launcher gates the sidecar by compose profile: `--profile graph` on
`up` only when enabled, and unconditionally on `down`/`status`/`logs`/`ps` — a sidecar left
running from a since-disabled config is still torn down and visible. No `graph:` block = no
sidecar, byte-identical behavior to earlier releases. The sidecar reuses the logger's image chain
and env (`ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, the data-root mount) — custom-type introspection
works wherever the recorder's does, and there are no new rig-owned env vars. Cost is negligible:
a ~28-node graph walks in single-digit milliseconds per tick and writes ~35 KB per epoch.

Use from a rig deployment (clone as a sibling):

```yaml
# services.yaml
services:
  zenoh-router:    { path: ../rig-infra/zenoh-router }
  ros2-bag-logger: { path: ../rig-infra/ros2-bag-logger }
```

or scaffold directly: `rig init my-vehicle --infra zenoh-router --infra ros2-bag-logger` (bare names
resolve by scanning the workspace) — wired + enabled, router pinned to order 0.
