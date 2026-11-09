# zenoh-store — the vehicle's run store over Zenoh

Serves every rig run's `store/` folder on Zenoh, so a ground station can fetch files from a vehicle
**while the run is still going**: keyframes from camera-service's `frame-capture`, point clouds,
reports, anything a service writes. One per vehicle, infra tier, right after the router.
Design, decisions and the link measurements behind them: [docs/zenoh-store-plan.md](../docs/zenoh-store-plan.md).

```
<data_dir>/runs/<run>/store/<producer>/<path…>/<name>.<ext>
        ⇕
fleet/<vehicle>/store/<run>/<producer>/<path…>/<name>.<ext>
```

## Writing into the store (producers)

Any vehicle service can, in one of two ways:

- **Write files** into the open run's `<data_dir>/current/store/<its instance>/…`: write a dot-prefixed
  temp name in the same directory, then rename it into place (the store ignores dotfiles and
  announces the file on the rename). Write a sidecar last if there is one. Use unique names; never
  rewrite or delete (retention is per run). In-place writers work too: a file is announced when it
  is closed; set `settle_ms` for writers that close and reopen.
- **Put** the bytes to `fleet/<vehicle>/store/<open run>/<producer>/…/<name>`. The store writes the
  file the same way. Puts to any other run, wildcard keys, deletes, names that would escape the
  folder, and payloads over `max_put_mb` are refused and logged (health shows the last refusal).

File names must also be valid Zenoh key chunks: no `*`, `$`, `?`, `#`, no leading `.` or `@`. Other
files are skipped and counted in health.

## Reading from the store (consumers)

| Key | What |
|---|---|
| `fleet/<v>/store/<run>/<path>` | the file (`<run>` may be `open`), capped at `max_reply_mb` |
| `…?offset=<n>;length=<n>` | a byte range: how anything big crosses a slow link |
| `…?meta` | no payload, just the attachment; add `;sha256` for a digest |
| `fleet/<v>/svc/<name>/store` | descriptor (liveliness token too): open run, runs on disk, fetch rules, limits |
| `fleet/<v>/svc/<name>/store/index` | `?run=<id\|open>;after=<cursor>;prefix=<p>;fetch=auto,latest;limit=<n>` → `{run, state, files: [{path, size, mtime_ns, fetch, priority}], next, more, reset}` |
| `fleet/<v>/svc/<name>/store/events` | one JSON message per new or changed file, with its `cursor` |
| `fleet/<v>/svc/<name>/health[/state]` | camera-service's HEALTH.md shape: files, bytes, per-producer age, puts, disk |

Every data reply carries a JSON attachment `{size, offset, mtime_ns, fetch, priority}` and an
encoding from the extension. Errors are `reply_err` with `{"error": <code>, "detail": …}`:
`bad_key`, `no_such_run`, `not_found`, `bad_range`, `too_large`, `busy`.

**Cursors** are `<epoch>.<seq>`: sequence numbers the store assigns as it sees files, so a vehicle
clock that jumps after boot cannot hide one. After a store restart old cursors are stale and the
index lists from the start with `reset: true`.

**Ask at low priority from the ground.** Zenoh replies inherit the query's priority; fetch with
`priority=DATA_LOW`, one chunk in flight, and file transfers queue behind telemetry instead of
stalling it (the plan's Phase 0 numbers).

### The client

```bash
python3 -m zenoh_store.client --vehicle v1 --connect tcp/<vehicle-router>:7447 describe
python3 -m zenoh_store.client --vehicle v1 --connect … ls --fetch auto
python3 -m zenoh_store.client --vehicle v1 --connect … get open lidar_top/clouds/000123.laz -o cloud.laz
```

`get` fetches in 32 KB chunks at DATA_LOW, resumes from `<out>.part`, and renames the file into
place once its size matches. It is the seed of the ground mirror (Phase 2).

## Fetch rules: what crosses the link on its own

Nothing does unless a rule says so. Rules live in the store's config (versioned with the vehicle's
deployment, captured in each run's config snapshot) and are tagged onto every listing, event and
reply; the ground mirror follows them and can only narrow them.

```yaml
store:
  fetch:                                # first match wins; unmatched files are on_request
    - {match: "*/keyframes/*.json", fetch: auto, priority: 10}
    - {match: "*/keyframes/*.jpg",  fetch: auto, priority: 20}
    - {match: "lidar_*/clouds/*.laz",    fetch: auto,   priority: 50, max_mb: 20}
    - {match: "lidar_*/snapshots/*.laz", fetch: latest, priority: 40}
```

`auto`: every matching file as it appears. `latest`: only the newest per directory (skips a
backlog). `on_request` (the default): listed, fetched when asked. Lower `priority` goes first;
`max_mb` demotes bigger files to `on_request`. Globs: `*` within a directory, `**` across,
`{a,b}` either. See [config/zenoh-store.example.yaml](config/zenoh-store.example.yaml) for every knob.

## Runs

- The **open run** is read once at startup from `<data_dir>/current` inside the container (never a
  compose variable). Rig refuses to rotate a run while any row is up, so the store restarts at every
  run boundary, like every recorder. If the link moves anyway, health warns until a restart.
- **Sealed and interrupted runs** are served read-only and indexed on first use.
- **No registry** (a bare data dir): `<data_dir>/store/` is served as the run `flat`.
- No open run: everything on disk is still served; puts are refused.

## Deployment

```yaml
# services.yaml
services:
  zenoh-store: { path: ../rig-infra/zenoh-store }
# vehicle.yaml
infra:
  - {name: zenoh-router, service: zenoh-router, order: 0}
  - {name: zenoh-store,  service: zenoh-store,  order: 1}
```

Image: `python:3.12-slim` + `eclipse-zenoh==1.9.0` (the fleet's zenoh-python pin) + this package,
built by `build.sh` under the rig build contract (arm64 + amd64). It mounts `${RIG_DATA_DIR}` at
`/data` read-write and uses host networking. The session is a Zenoh peer connecting to
`tcp/localhost:7447` and listening on loopback only (`zenoh_listen`), which also keeps it working
where IPv6 is disabled.

On a radio uplink, the router's ACL should allow queries in and replies out on `fleet/*/store/**`
and `fleet/*/svc/*/store/**`, and deny puts in either direction on `fleet/*/store/**` (see the plan's
router section). The example's 256 KB uplink size cap must sit above the largest chunk you fetch.

## Tests

```bash
python3 tests/test_store_core.py     # config, keys, rules, registry, index, gets/puts (stdlib)
python3 tests/test_store_watch.py    # live index with real inotify (Linux)
python3 tests/test_store_zenoh.py    # real Zenoh sessions; needs eclipse-zenoh==1.9.0, else skipped
```
