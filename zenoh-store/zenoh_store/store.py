"""The store itself, without Zenoh: answers gets, puts and index queries from a run's store/ folder,
keeps the open run's index live, and reports health. zenoh_io.py puts it on the wire.

Contract: docs/zenoh-store-plan.md (rig-infra). Error replies are `{"error": <code>, "detail": ...}`
with codes bad_key, no_such_run, not_found, bad_range, too_large, not_open_run, exists, read_only,
internal_error (and busy, from the adapter).
"""
from __future__ import annotations

import errno
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import keys
from .files import DigestCache, RangeError, TooLarge, read_range, write_atomic
from .index import Entry, RunIndex
from .inotify import InotifyUnavailable, TreeWatcher
from .policy import Policy
from .runs import Registry

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SERVICE = "zenoh-store"
POLL_S = 2.0                 # rescan period when inotify is unavailable
MB = 1024 * 1024


@dataclass
class Reply:
    ok: bool
    payload: bytes = b""
    attachment: dict = field(default_factory=dict)
    encoding: str = "application/octet-stream"
    error: Optional[dict] = None


def _err(code: str, detail: str, **extra) -> Reply:
    return Reply(False, error={"error": code, "detail": detail, **extra})


def parse_parameters(raw) -> dict:
    """Selector parameters `a=1;b=2;flag` (also `&`-separated) as a dict; a bare flag maps to ""."""
    out = {}
    for part in str(raw or "").replace("&", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def _int(params: dict, key: str, default=None, minimum: int = 0):
    if key not in params or params[key] == "":
        return default
    try:
        value = int(params[key])
    except ValueError:
        raise RangeError(f"{key}: expected an integer, got {params[key]!r}") from None
    if value < minimum:
        raise RangeError(f"{key}: must be >= {minimum}")
    return value


class Store:
    def __init__(self, settings: dict, data_dir: str, vehicle: str, instance: str,
                 clock: Callable[[], int] = time.time_ns, monotonic: Callable[[], float] = time.monotonic,
                 epoch: Optional[str] = None):
        self.settings = settings
        self.vehicle = vehicle
        self.instance = instance
        self.registry = Registry(data_dir)
        self.policy = Policy.from_settings(settings)
        self._clock = clock
        self._mono = monotonic
        self.started_ns = clock()
        self.epoch = epoch or format(self.started_ns, "x")
        self.open_run = self.registry.current()          # pinned for the life of the process
        self.max_put = int(settings["max_put_mb"] * MB)
        self.max_reply = int(settings["max_reply_mb"] * MB)
        self._indexes: dict = {}
        self._index_lock = threading.Lock()
        self._observers: list = []
        self._digests = DigestCache()
        self._rescan_now = threading.Event()
        self._stop = threading.Event()
        self._rescanner: Optional[threading.Thread] = None
        self.watcher: Optional[TreeWatcher] = None
        self.watch_mode = "none"
        self.watch_error: Optional[str] = None
        self.run_drift: Optional[str] = None
        self.counters = {"gets": 0, "gets_failed": 0, "bytes_served": 0, "puts": 0, "puts_refused": 0,
                         "puts_duplicate": 0, "events": 0}
        self.last_put_error: Optional[tuple] = None       # (monotonic, detail)

    # ---- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self.open_run is None:
            log.info("store: no open run; serving %d run(s) read-only", len(self.registry.runs(None)))
        else:
            idx = self.index(self.open_run)
            log.info("store: open run %s: %d file(s), %.1f MB", self.open_run, len(idx), idx.bytes / MB)
            self.watcher = TreeWatcher(idx.root, self._on_file, self._on_gone, self._rescan_now.set,
                                       settle_ms=self.settings["settle_ms"], clock=self._mono)
            try:
                self.watcher.start()
                self.watch_mode = "inotify"
                self._rescan_now.set()        # catch files written between the scan and the watch
            except (InotifyUnavailable, OSError) as e:
                self.watcher = None
                self.watch_mode = "polling"
                self.watch_error = str(e)
                log.warning("store: %s; rescanning every %gs instead", e, POLL_S)
        self._rescanner = threading.Thread(target=self._rescan_loop, name="store-rescan", daemon=True)
        self._rescanner.start()

    def stop(self) -> None:
        self._stop.set()
        self._rescan_now.set()
        if self.watcher is not None:
            self.watcher.stop()
        if self._rescanner is not None:
            self._rescanner.join(2.0)

    def add_observer(self, fn: Callable[[str, Entry], None]) -> None:
        self._observers.append(fn)

    def _emit(self, run: str, entries) -> None:
        for entry in entries:
            self.counters["events"] += 1
            for fn in self._observers:
                try:
                    fn(run, entry)
                except Exception as e:   # noqa: BLE001 -- a broken observer never stops indexing
                    log.warning("store: event observer failed: %s", e)

    def _on_file(self, rel: str) -> None:
        if self.open_run is None:
            return
        entry = self.index(self.open_run).note(rel)
        if entry is not None:
            self._emit(self.open_run, [entry])

    def _on_gone(self, rel: str) -> None:
        if self.open_run is None:
            return
        idx = self.index(self.open_run)
        idx.forget_prefix(rel) if rel.endswith("/") else idx.forget(rel)

    def rescan(self) -> list:
        if self.open_run is None:
            return []
        changed = self.index(self.open_run).scan(self._mono())
        self._emit(self.open_run, changed)
        current = self.registry.current()
        if current != self.open_run:
            if self.run_drift != current:
                log.warning("store: the open run moved to %s under a running store (still serving %s; "
                            "restart to follow)", current, self.open_run)
            self.run_drift = current or "none"
        return changed

    def _rescan_loop(self) -> None:
        period = POLL_S if self.watch_mode == "polling" else float(self.settings["rescan_s"])
        while not self._stop.is_set():
            self._rescan_now.wait(period)
            self._rescan_now.clear()
            if self._stop.is_set():
                return
            try:
                self.rescan()
            except Exception as e:   # noqa: BLE001
                log.warning("store: rescan failed: %s", e)

    # ---- indexes -------------------------------------------------------------------
    def index(self, run: str) -> RunIndex:
        """The run's index: the open run's is live; any other run is scanned on first use and
        again once it is older than sealed_ttl_s."""
        with self._index_lock:
            idx = self._indexes.get(run)
            if idx is None:
                idx = RunIndex(run, self.registry.store_dir(run), self.epoch)
                self._indexes[run] = idx
        if idx.scanned_mono is None or (run != self.open_run
                                         and self._mono() - idx.scanned_mono > self.settings["sealed_ttl_s"]):
            idx.scan(self._mono())
        return idx

    def _run_or_error(self, run: str) -> Optional[Reply]:
        if run == "open":
            return _err("no_such_run", "there is no open run") if self.open_run is None else None
        if not self.registry.exists(run):
            return _err("no_such_run", f"no run {run!r} on this vehicle")
        return None

    # ---- data gets -------------------------------------------------------------------
    def handle_get(self, key: str, parameters="") -> Reply:
        self.counters["gets"] += 1
        reply = self._get(key, parse_parameters(parameters))
        if reply.ok:
            self.counters["bytes_served"] += len(reply.payload)
        else:
            self.counters["gets_failed"] += 1
        return reply

    def _get(self, key: str, params: dict) -> Reply:
        try:
            run, rel = keys.parse_data_key(key, self.vehicle)
        except keys.KeyError_ as e:
            return _err("bad_key", str(e))
        missing = self._run_or_error(run)
        if missing:
            return missing
        if run == "open":                   # alias for gets (the reply keeps the key that was asked)
            run = self.open_run
        root = self.registry.store_dir(run)
        try:
            path = keys.resolve(root, rel)
            offset = _int(params, "offset", 0)
            length = _int(params, "length", None, minimum=1)
        except keys.KeyError_ as e:
            return _err("bad_key", str(e))
        except RangeError as e:
            return _err("bad_range", str(e))
        meta_only = "meta" in params
        try:
            if meta_only:
                st = os.stat(path, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    raise FileNotFoundError(path)
                data, size, mtime = b"", st.st_size, st.st_mtime_ns
            else:
                data, size, mtime = read_range(path, offset, length, self.max_reply)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return _err("not_found", f"{rel} is not in run {run}")
        except TooLarge as e:
            return _err("too_large", str(e), size=os.path.getsize(path))
        except RangeError as e:
            return _err("bad_range", str(e), size=os.path.getsize(path))
        except OSError as e:
            if e.errno == errno.ELOOP:      # O_NOFOLLOW: the file itself is a symlink
                return _err("not_found", f"{rel} is not in run {run}")
            return _err("internal_error", f"{rel}: {e.strerror or e}")
        fetch, priority = self.policy.classify(rel, size)
        att = {"size": size, "offset": 0 if meta_only else offset, "mtime_ns": mtime,
               "fetch": fetch, "priority": priority}
        if "sha256" in params:
            try:
                att["sha256"] = self._digests.sha256(path, size, mtime)
            except OSError as e:
                return _err("internal_error", f"{rel}: {e.strerror or e}")
        return Reply(True, data, att, self.policy.mime(rel))

    # ---- puts ----------------------------------------------------------------------
    def refuse(self, code: str, detail: str) -> dict:
        self.counters["puts_refused"] += 1
        self.last_put_error = (self._mono(), f"{code}: {detail}")
        log.warning("store: put refused (%s): %s", code, detail)
        return {"ok": False, "error": code, "detail": detail}

    def handle_put(self, key: str, payload: bytes) -> dict:
        try:
            run, rel = keys.parse_data_key(key, self.vehicle)
        except keys.KeyError_ as e:
            return self.refuse("bad_key", str(e))
        if self.open_run is None or run != self.open_run:
            return self.refuse("not_open_run", f"{key}: puts go to the open run only "
                                                f"({self.open_run or 'none is open'})")
        if len(payload) > self.max_put:
            return self.refuse("too_large", f"{key}: {len(payload)} bytes > max_put_mb "
                                             f"({self.settings['max_put_mb']} MB)")
        idx = self.index(run)       # before the write: a first scan must not swallow this file's event
        try:
            path = keys.resolve(idx.root, rel)
            write_atomic(path, payload)
        except keys.KeyError_ as e:
            return self.refuse("bad_key", str(e))
        except FileExistsError:
            if os.path.getsize(path) == len(payload):
                self.counters["puts_duplicate"] += 1
                return {"ok": True, "duplicate": True, "path": rel}
            return self.refuse("exists", f"{rel} already exists with different content (files are "
                                          f"never rewritten)")
        except OSError as e:
            return self.refuse("write_failed", f"{rel}: {e.strerror or e}")
        self.counters["puts"] += 1
        entry = idx.note(rel)
        if entry is not None:
            self._emit(run, [entry])
        return {"ok": True, "path": rel}

    def handle_delete(self, key: str) -> dict:
        return self.refuse("read_only", f"{key}: deletes are not accepted (retention is per run)")

    # ---- management ------------------------------------------------------------------
    def entry_dict(self, entry: Entry, run: Optional[str] = None) -> dict:
        fetch, priority = self.policy.classify(entry.path, entry.size)
        d = {"path": entry.path, "size": entry.size, "mtime_ns": entry.mtime_ns,
             "fetch": fetch, "priority": priority}
        if run is not None:
            d = {"run": run, **d, "cursor": f"{self.epoch}.{entry.seq}"}
        return d

    def index_query(self, parameters="") -> dict:
        params = parse_parameters(parameters)
        run = params.get("run") or "open"
        missing = self._run_or_error(run)
        if missing:
            return missing.error
        if run == "open":
            run = self.open_run
        try:
            limit = min(_int(params, "limit", 500, minimum=1), 5000)
        except RangeError as e:
            return {"error": "bad_request", "detail": str(e)}
        modes = {m for m in params.get("fetch", "").split(",") if m}
        bad = modes - {"auto", "latest", "on_request"}
        if bad:
            return {"error": "bad_request", "detail": f"fetch: unknown mode(s) {sorted(bad)}"}
        idx = self.index(run)
        after, reset = idx.parse_cursor(params.get("after"))
        accept = (lambda e: self.policy.classify(e.path, e.size)[0] in modes) if modes else None
        entries, nxt, more = idx.list(after, params.get("prefix", ""), accept, limit)
        return {"run": run, "state": self.registry.state(run, self.open_run), "reset": reset,
                "files": [self.entry_dict(e) for e in entries], "next": nxt, "more": more}

    def descriptor(self) -> dict:
        runs = []
        for info in self.registry.runs(self.open_run):
            idx = self._indexes.get(info.run)
            runs.append({"run": info.run, "state": info.state, "has_store": info.has_store,
                         "files": len(idx) if idx is not None else None,
                         "bytes": idx.bytes if idx is not None else None})
        return {"schema_version": SCHEMA_VERSION, "service": SERVICE, "vehicle": self.vehicle,
                "instance": self.instance, "epoch": self.epoch, "started_unix_ns": self.started_ns,
                "data_key": keys.data_prefix(self.vehicle), "open_run": self.open_run, "runs": runs,
                "put": {"enabled": self.open_run is not None, "max_mb": self.settings["max_put_mb"]},
                "max_reply_mb": self.settings["max_reply_mb"], "chunk_kb": self.settings["chunk_kb"],
                "fetch": self.policy.describe()}

    def health(self) -> dict:
        name = lambda comp: f"{self.instance}: {comp}"   # noqa: E731
        now_ns, now_mono = self._clock(), self._mono()
        status = []
        values = {"run": self.open_run, "watch": self.watch_mode, **{k: v for k, v in self.counters.items()}}
        level, problems = 0, []
        if self.open_run is not None:
            idx = self.index(self.open_run)
            values.update({"files": len(idx), "bytes": idx.bytes, "skipped_names": len(idx.skipped),
                           "rescan_age_s": round(now_mono - idx.scanned_mono, 1) if idx.scanned_mono else None})
            if self.watcher is not None:
                values.update({"watched_dirs": self.watcher.watching, "overflows": self.watcher.overflows})
            for prod, (n, newest) in sorted(self.index(self.open_run).producers().items())[:16]:
                values[f"files.{prod}"] = n
                values[f"age_s.{prod}"] = round(max(0, now_ns - newest) / 1e9, 1)
            message = f"run {self.open_run}: {len(idx)} file(s), {idx.bytes / MB:.1f} MB"
            if idx.skipped:
                level = max(level, 1)
                problems.append(f"{len(idx.skipped)} file name(s) unusable as keys")
        else:
            message = f"no open run; serving {len(self.registry.runs(None))} run(s) read-only"
        if self.watch_mode == "polling":
            level = max(level, 1)
            problems.append(f"inotify unavailable, polling every {POLL_S:g}s")
        if self.run_drift:
            level = max(level, 1)
            problems.append(f"open run changed to {self.run_drift}; restart the store to follow")
        if self.last_put_error and now_mono - self.last_put_error[0] < 60:
            level = max(level, 1)
            problems.append(f"put refused: {self.last_put_error[1]}")
        if not os.path.isdir(self.registry.data_dir):
            level = 2
            problems = [f"data dir {self.registry.data_dir} is missing"]
        status.append({"level": level, "name": name("store"),
                       "message": "; ".join(problems) if problems else message,
                       "hardware_id": "", "values": values})
        status.append(self._disk_status(name("disk")))
        return {"schema_version": 1, "service": SERVICE, "instance": self.instance,
                "stamp_unix_ns": now_ns, "level": max(s["level"] for s in status), "status": status}

    def _disk_status(self, name: str) -> dict:
        try:
            st = os.statvfs(self.registry.data_dir)
        except OSError as e:
            return {"level": 3, "name": name, "message": f"statvfs failed: {e.strerror}", "hardware_id": "",
                    "values": {}}
        free = st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize or 1
        pct = 100.0 * free / total
        level = 2 if pct < 2 else 1 if pct < 10 else 0
        return {"level": level, "name": name, "message": f"{free / 1e9:.1f} GB free ({pct:.0f}%)",
                "hardware_id": "", "values": {"disk.free_gb": round(free / 1e9, 2), "disk.free_pct": round(pct, 1)}}
