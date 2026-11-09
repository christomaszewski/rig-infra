# Vehicle store over Zenoh: plan

Status: **plan**, with Phase 0 (spike) done and decisions 1–6 confirmed; Phase 1 in progress.

## Goal

Get selected run data, starting with camera-service's `frame-capture` keyframes (JPEG, 16-bit TIFF,
JSON sidecars) but open to any file type a service produces (point clouds, logs, reports), from a
vehicle to the ground **while the run is still going**. It has to survive a
spotty radio link and must not starve telemetry or commands on that link. `rig fleet sync` stays the
way whole sealed runs come home; this covers the gap before that.

## Decisions

1. **The store is a small Python service on the fleet's Zenoh binding, not `zenohd` with the
   storage-manager and filesystem-backend plugins.**
   - We are not using Zenoh replication. Replication is symmetric: deletes propagate, and a wiped
     replica re-downloads everything. That doesn't fit a vehicle with a small disk feeding a ground
     station.
   - Without replication, the plugin stack costs a lot for little: a Rust build pinned to one
     compiler and Zenoh version, a second router on the vehicle, and a RocksDB folder in the run.
   - It also returns each stored value whole, which the spike shows is what hurts the link.
   - `eclipse-zenoh==1.9.0` is already on the vehicle (camera-service, the dashboard agent). A
     Python store adds no new Zenoh version. `rmw_zenoh` on rolling pins Zenoh 1.10.1 plus fixes,
     and the 1.9 sessions already talk to that router.
2. **The ground pulls, in chunks it paces; nothing is pushed over the link.** The spike numbers are
   below.
3. **A run's `store/` folder is the contract.** Any vehicle service can write files into it, or put
   to the matching keys. The store serves both the same way.
4. **frame-capture writes its files straight into `store/`** (new `file_root: store`), rather than
   putting them through the router.
5. **The store serves every run on disk, read-only once sealed,** so a ground station that missed
   the end of a run can still catch up.
6. **Nothing crosses the link unless a rule says so.** The store's config lists fetch rules by
   path pattern. A matching file is fetched as soon as it appears (`auto`), or only the newest one is
   (`latest`); everything else is listed but fetched only on request. The example config makes
   keyframe sidecars and JPEGs `auto`; TIFFs, point clouds and anything else stay on request until
   a rule names them. See [File types and fetch rules](#file-types-and-fetch-rules).
7. **Where the ground mirror runs** (a rig row in the ground station's deployment, a laptop tool, or
   part of the dashboard): *(open)*.

## Phase 0 findings (spike)

Scripts in [zenoh-store-spike/](zenoh-store-spike/). There is no `tc` here, so a "vehicle" and a
"ground" Zenoh peer talk through `link_relay.py`, a rate-limited TCP relay that keeps any backlog in
the sender's socket buffer, as a radio would. Settings: 1 Mbit/s, 30 ms each way. Telemetry is a
20 Hz publisher at default priority; its delay at idle is ~32 ms.

| Transfer | Time | Telemetry delay during it |
|---|---|---|
| 656 KB TIFF, whole reply (query at low priority) | 5.8 s | p50 2.7 s, max 5.7 s |
| 656 KB TIFF, pushed to a subscriber | 5.8 s | p50 2.7 s, max 5.8 s |
| 656 KB TIFF, 64 KB chunks, one in flight | 6.5 s | p50 255 ms, max 602 ms |
| 30 KB JPEG, whole | 0.3 s | max ~290 ms |

| Chunk size | TIFF transfer | Telemetry max |
|---|---|---|
| 16 KB | 8.5 s (+46%) | 174 ms |
| 32 KB | 7.2 s (+24%) | 314 ms |
| 64 KB | 6.5 s (+12%) | 602 ms |
| 128 KB | 6.1 s (+5%) | 1.1 s |

- **Zenoh priority alone does not protect telemetry.** Once a large reply is in the TCP stream,
  everything behind it waits. Pacing from the receiver does protect it: added delay ≈ chunk size ÷
  link rate. The cost is roughly RTT ÷ (RTT + chunk time) in throughput. So the mirror sizes chunks
  to the link rate it measures, aiming for a target delay (default 250 ms: ~32 KB at 1 Mbit/s,
  ~300 KB at 10 Mbit/s).
- **Reply QoS comes from the query.** In zenoh-python 1.9, `priority` and `congestion_control` on
  `Query.reply` are deprecated and ignored. The ground sets `priority=DATA_LOW` on its `get`.
- **Same-host tests need `transport/shared_memory/enabled: false`.** Zenoh otherwise moves large
  payloads between peers on one machine through shared memory, outside TCP. That invalidated the
  first spike run (0.7 s for 656 KB over a 125 KB/s link).
- **Index cost:** one scan of a 32,400-file store (3 cameras × 3 files × 3,600 captures) takes about
  70 ms of CPU on a 2.1 GHz Xeon core. Polling that every 2 s would be ~3.5% of a core. So the store
  keeps its index in memory, maintained with inotify, and scans only at startup or as an occasional
  safety net.
- **File ownership:** camera-service and the bag loggers run as root (no `user:` in their compose
  files). A store container running as root can serve and write their files. Revisit if any
  producer runs as a different uid.

## Contract

### Data

```
key:   fleet/<vid>/store/<run_id>/<producer>/<path…>/<name>.<ext>
file:  <data_dir>/runs/<run_id>/store/<producer>/<path…>/<name>.<ext>
```

`<producer>` is the writing service's instance name (e.g. `cam_front`). frame-capture uses
`<instance>/keyframes/<utc_ns>.{jpg,tif,json}`.

**Producers** do one of two things:
- **Write files** into the open run's `store/`:
  - write to a dot-prefixed temp name in the same directory, then rename (the store ignores
    dotfiles and announces a file when it is renamed into place);
  - write a sidecar last, if there is one;
  - use unique names and never rewrite or delete (retention is per run).
- **Put** to the data key, open run only. The store validates the key and writes the file the same
  way. Wildcard keys, path escapes (`..`, empty or dot-prefixed chunks), deletes, and payloads over
  `max_put_mb` are refused and logged.

**Gets** on `fleet/<vid>/store/**` are answered from disk, for any run that has a `store/`:
- `…/<file>`: the whole file. Meant for small files; the mirror chunks anything bigger than its
  chunk size.
- `…/<file>?offset=<n>;length=<n>`: that byte range.
- Every reply carries an attachment `{"size", "offset", "mtime_ns"}` and an encoding taken from the
  extension. A missing file or bad range is a `reply_err` with `{"error": …}`.

### Management (`fleet/<vid>/svc/<instance>/store…`)

| Key | Kind | Content |
|---|---|---|
| `…/store` | liveliness + queryable | descriptor: vehicle, instance, open run, runs on disk, put limits, a chunk-size hint |
| `…/store/index` | queryable | `?run=<id\|open>;after=<cursor>;prefix=<producer/…>;fetch=<auto\|latest\|on_request>;limit=<n>` → `{run, files: [{key, size, mtime_ns, fetch, priority}], next, truncated}`; no file contents |
| `…/store/events` | publisher | `{run, key, size, mtime_ns, fetch, priority}` for each completed file, however it was written |
| `…/health`, `…/health/state` | queryable / publisher | camera-service's `HEALTH.md` shape: files, bytes, free disk, last write per producer, index/inotify state. The dashboard's health widget shows it unchanged. |

## File types and fetch rules

Any file type works: the store serves bytes. The extension only picks the reply's encoding, from
built-in defaults (`jpg`, `tif`, `json`, `png`, `las`, `laz`, `pcd`, `ply`, `csv`, `yaml`, `mcap`, …)
plus any the config adds; unknown extensions are `application/octet-stream`. File names must be
valid Zenoh key chunks (no `*`, `$`, `?`, `#`); others are skipped and counted in health.

What crosses the link is decided by **fetch rules** in the store's config, so the policy is
versioned with the vehicle's deployment and captured in each run's config snapshot:

```yaml
store:
  fetch:                                # first match wins; unmatched files are on_request
    - {match: "*/keyframes/*.json", fetch: auto, priority: 10}
    - {match: "*/keyframes/*.jpg",  fetch: auto, priority: 20}
    # - {match: "lidar_*/clouds/*.laz",    fetch: auto,   priority: 50, max_mb: 20}
    # - {match: "lidar_*/snapshots/*.laz", fetch: latest, priority: 40}
  mime:
    pcd: application/x-pcd              # extension -> encoding, on top of the defaults
```

| `fetch` | The mirror… |
|---|---|
| `auto` | fetches every matching file as it appears |
| `latest` | fetches only the newest matching file per directory, skipping older ones queued during an outage (situational-awareness streams) |
| `on_request` (default) | lists it, and fetches it only when someone asks |

- `match` is a path glob relative to the run's `store/` (`*` within one directory level, `**`
  across levels).
- `priority`: lower is fetched first, so keyframe JPEGs go ahead of a backlog of clouds.
- `max_mb`: an `auto` or `latest` file bigger than this is treated as `on_request`.

The store attaches each file's rule result (`fetch`, `priority`) to the index, events and replies.
The mirror follows it, and the ground can only narrow it (deny patterns, a link budget). Point
clouds are fetched in the same paced, resumable chunks as everything else, so a 30 MB cloud costs
link time but never blocks telemetry or commands.

## Vehicle service: `zenoh-store` (rig-infra)

- **Row:** infra tier, order 1, right after the router. Launcher, compose and example config follow
  `zenoh-router/`; certified like the other rows.
- **Image:** `python:3.12-slim` + `eclipse-zenoh==1.9.0`, multi-arch through the repo's build
  contract. inotify via `ctypes`, no extra packages.
- **Run:** resolved at startup from `readlink $RIG_DATA_DIR/current`, inside the container (never a
  compose variable, which would get frozen into baked artifacts). Rig refuses to rotate a run while
  any row is up, so the store restarts at every run boundary, like the plugin. Replay runs get their
  own run ids.
- **Index:** in memory, `{key: (size, mtime)}`. One scan of every run's `store/` at startup, inotify
  on the open run, a rescan every few minutes as a safety net. Sealed runs are scanned on first use
  and cached.
- **Session:** Zenoh peer connecting to `tcp/localhost:7447` (`ZENOH_CONNECT` overrides), same
  settings as camera-service's control plane.
- **Failure behaviour:**
  - no `data_dir`, or no open run: serves what is on disk and refuses puts;
  - disk errors: the put is refused, and health goes WARN or ERROR;
  - Zenoh unreachable: keeps indexing and retries.

## Plugin change: camera-service

`frame-capture` gains `file_root: recordings | store`. With `store`, the file sink writes
`<run>/store/<instance>/keyframes/`. Files then appear at
`fleet/<vid>/store/<run>/<instance>/keyframes/…`, the same keys its Zenoh output already uses, and
the plugin no longer depends on the store being up. The Zenoh output remains for producers that
don't share the vehicle's disk.

## Ground mirror

- **Finds vehicles** by the store's liveliness token. Follows `…/store/events` from every vehicle
  (small messages).
- **Catches up** after a reconnect or its own restart by asking each store's index for anything
  newer than its saved position.
- **Follows the store's fetch rules** (`auto`, `latest`, `on_request`, priority), narrowed by its own
  config if needed. A small query (or CLI) on the ground fetches on request:
  `fetch <vehicle> <run> <glob>`.
- **Fetching:**
  - one request in flight per vehicle, `priority=DATA_LOW`;
  - chunks sized to the measured link rate and the target delay, clamped to 8 KB – 1 MB;
  - partial files kept as `.part` and resumed from the last chunk;
  - size checked against the index before renaming into place.
- **Layout:** `fleet-runs/<label>/<vehicle>/<run-id>/store/…`, the tree `rig fleet sync` uses, so
  the later full sync finds the files already there. Vehicle names come from the `fleet.yaml`
  roster, labels from the run id.

## Router configuration (zenoh-router example)

- **On the uplink, allow:**
  - incoming queries, and outgoing replies, on `fleet/*/store/**` and `fleet/*/svc/*/store/**`;
  - outgoing puts on `fleet/*/svc/*/store/events`.
- **Deny outgoing puts on `fleet/*/store/**`**, so a misconfigured subscriber can't drag files over
  the radio.
- **Size cap:** the example's 256 KB uplink `low_pass_filter` must exclude the store keys, or sit
  above the mirror's largest chunk.

## Rig integration

- `vehicle.yaml`: `{name: zenoh-store, service: zenoh-store, order: 1}` under `infra:`.
- `store/` lives inside the run, so `fleet sync` carries it and run retention cleans it.
  `export_profiles` can leave it out with `omit: ["store/**"]`.
- Nothing in rig itself changes.

## Testing

- **Unit:** key/path mapping and refusals, chunked reads, index and inotify updates, put writes.
- **Integration:** real Zenoh peers, IPv4 endpoints, shared memory off.
- **Link regression:** the spike's relay, asserting telemetry delay stays bounded during a chunked
  TIFF transfer.
- **Run rotation:** restart into a new open run.
- **End to end:** plugin → `store/` → index and events → mirror → `fleet-runs/`.

## Phases

| Phase | Work | Repo |
|---|---|---|
| 0 | Spike (done, above) | rig-infra |
| 1 | `zenoh-store` service, tests, certify, docs; plugin `file_root: store` | rig-infra, camera-service |
| 2 | Ground mirror (location per decision 7), fetch rules, resume | rig-infra |
| 3 | Router example rules; dashboard keyframe gallery and store health | rig-infra, dashboard |
