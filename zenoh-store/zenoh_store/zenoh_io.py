"""The store on one peer-mode Zenoh session (keys: docs/zenoh-store-plan.md "Contract").

    fleet/<vid>/store/**                    queryable (data gets, answered by worker threads)
                                            subscriber (puts to the open run, written by one thread)
    fleet/<vid>/svc/<instance>/store        liveliness token + queryable: the descriptor
    fleet/<vid>/svc/<instance>/store/index  queryable: file listings after a cursor
    fleet/<vid>/svc/<instance>/store/events publisher: one message per new or changed file
    fleet/<vid>/svc/<instance>/health       liveliness token + queryable (HEALTH.md shape)
    fleet/<vid>/svc/<instance>/health/state publisher, every health_interval_s

Reads happen off Zenoh's callback threads, so a big reply never stalls the session. Replies inherit
the priority of the query that asked (zenoh 1.9): a ground fetcher asks at DATA_LOW so file
chunks queue behind telemetry on the link. Best-effort: Zenoh being unreachable at startup only
delays advertising; indexing goes on.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from typing import Callable, Optional

from .store import Store

log = logging.getLogger(__name__)

DEFAULT_CONNECT = "tcp/localhost:7447"
CONNECT_TIMEOUT_MS = 2000


def encode_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")


def endpoints(raw: Optional[str]) -> list:
    return [e.strip() for e in str(raw or "").split(",") if e.strip()]


def connect_endpoints(configured: Optional[str]) -> list:
    """The config's endpoints, else $ZENOH_CONNECT, else the vehicle's router. Comma-separated;
    an empty string means scout only."""
    return endpoints(configured if configured is not None else os.environ.get("ZENOH_CONNECT", DEFAULT_CONNECT))


def service_key(vehicle: str, instance: str) -> str:
    return f"fleet/{vehicle}/svc/{instance}"


def _bytes(zbytes) -> bytes:
    if zbytes is None:
        return b""
    return zbytes.to_bytes() if hasattr(zbytes, "to_bytes") else bytes(zbytes)


class ZenohStore:
    def __init__(self, store: Store, connect, listen=(), session_factory: Optional[Callable] = None,
                 retry_s: float = 5.0, extra_config: Optional[dict] = None):
        s = store.settings
        self.store = store
        base = service_key(store.vehicle, store.instance)
        self.data_key = f"fleet/{store.vehicle}/store/**"
        self.store_key = base + "/store"
        self.index_key = base + "/store/index"
        self.events_key = base + "/store/events"
        self.health_key = base + "/health"
        self.health_state_key = base + "/health/state"
        self._connect = list(connect or [])
        self._listen = list(listen or [])
        self._extra = dict(extra_config or {})
        self._open = session_factory or self._open_zenoh
        self._retry_s = retry_s
        self._zenoh = None
        self._session = None
        self._decls: list = []
        self._tokens: list = []
        self._events = None
        self._health_pub = None
        self._gets: "queue.Queue" = queue.Queue(maxsize=s["queue"])
        self._puts: "queue.Queue" = queue.Queue(maxsize=16)
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._get_worker, name=f"store-get-{i}", daemon=True)
                         for i in range(s["workers"])]
        self._threads.append(threading.Thread(target=self._put_worker, name="store-put", daemon=True))
        self._threads.append(threading.Thread(target=self._health_loop, name="store-health", daemon=True))
        self.busy = 0
        store.add_observer(self._publish_event)

    @property
    def active(self) -> bool:
        return bool(self._tokens)

    # ---- session -------------------------------------------------------------------
    def _open_zenoh(self, connect):
        import zenoh
        self._zenoh = zenoh
        try:
            zenoh.init_log_from_env_or("error")
        except Exception:   # noqa: BLE001 -- older binding
            pass
        conf = zenoh.Config()
        conf.insert_json5("mode", '"peer"')
        if connect:
            conf.insert_json5("connect/endpoints", json.dumps(list(connect)))
        conf.insert_json5("connect/timeout_ms", str(CONNECT_TIMEOUT_MS))
        conf.insert_json5("connect/exit_on_failure", "false")
        if self._listen:
            conf.insert_json5("listen/endpoints", json.dumps(self._listen))
        for path, value in self._extra.items():
            conf.insert_json5(path, json.dumps(value))
        return zenoh.open(conf)

    def start(self) -> None:
        for t in self._threads:
            t.start()
        if not self.advertise():
            log.info("zenoh: not reachable yet; retrying every %gs", self._retry_s)
            threading.Thread(target=self._retry_loop, name="store-zenoh-retry", daemon=True).start()

    def _retry_loop(self) -> None:
        while not self._stop.wait(self._retry_s):
            if self.advertise():
                return

    def advertise(self) -> bool:
        if self._session is not None:
            return self.active
        try:
            self._session = self._open(self._connect)
            ses = self._session
            self._decls.append(ses.declare_queryable(self.data_key, self._on_data_query))
            self._decls.append(ses.declare_subscriber(self.data_key, self._on_sample))
            self._decls.append(ses.declare_queryable(self.store_key, self._on_descriptor))
            self._decls.append(ses.declare_queryable(self.index_key, self._on_index))
            self._decls.append(ses.declare_queryable(self.health_key, self._on_health))
            self._events = ses.declare_publisher(self.events_key)
            self._health_pub = ses.declare_publisher(self.health_state_key)
            self._tokens = [ses.liveliness().declare_token(self.store_key),
                            ses.liveliness().declare_token(self.health_key)]
            log.info("zenoh: serving %s and %s (connect=%s)", self.data_key, self.store_key,
                     self._connect or "scout")
            return True
        except Exception as e:   # noqa: BLE001
            log.warning("zenoh: advertise failed (%s); indexing continues", e)
            self._close_session()
            return False

    def close(self) -> None:
        self._stop.set()
        self._close_session()
        for t in self._threads:
            t.join(1.0)

    def _close_session(self) -> None:
        for obj in self._tokens + [self._events, self._health_pub] + self._decls:
            try:
                if obj is not None:
                    obj.undeclare()
            except Exception:   # noqa: BLE001
                pass
        self._tokens, self._decls, self._events, self._health_pub = [], [], None, None
        if self._session is not None:
            try:
                self._session.close()
            except Exception:   # noqa: BLE001
                pass
        self._session = None

    # ---- replies -------------------------------------------------------------------
    def _encoding(self, mime: str):
        z = self._zenoh
        if z is None:
            return None
        return z.Encoding.APPLICATION_JSON if mime == "application/json" else z.Encoding(mime)

    def _reply_json(self, query, key: str, obj) -> None:
        try:
            query.reply(key, encode_json(obj), encoding=self._encoding("application/json"))
        except Exception as e:   # noqa: BLE001
            log.warning("zenoh: reply on %s failed: %s", key, e)

    def _reply_err(self, query, obj) -> None:
        try:
            query.reply_err(encode_json(obj), encoding=self._encoding("application/json"))
        except Exception as e:   # noqa: BLE001
            log.warning("zenoh: error reply failed: %s", e)

    def _on_descriptor(self, query) -> None:
        self._reply_json(query, self.store_key, self.store.descriptor())

    def _on_index(self, query) -> None:
        try:
            result = self.store.index_query(str(query.parameters or ""))
        except Exception as e:   # noqa: BLE001
            log.exception("zenoh: index query failed")
            result = {"error": "internal_error", "detail": str(e)}
        if "error" in result:
            self._reply_err(query, result)
        else:
            self._reply_json(query, self.index_key, result)

    def _on_health(self, query) -> None:
        self._reply_json(query, self.health_key, self.store.health())

    # ---- data gets -------------------------------------------------------------------
    def _on_data_query(self, query) -> None:
        """Zenoh thread: hand the query to a worker, or say busy now."""
        try:
            self._gets.put_nowait((query, str(query.key_expr), str(query.parameters or "")))
        except queue.Full:
            self.busy += 1
            self._reply_err(query, {"error": "busy", "detail": f"{self._gets.maxsize} get(s) already queued"})

    def _get_worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._gets.get(timeout=0.5)
            except queue.Empty:
                continue
            query, key, params = job
            try:
                reply = self.store.handle_get(key, params)
                if reply.ok:
                    query.reply(key, reply.payload, encoding=self._encoding(reply.encoding),
                                attachment=encode_json(reply.attachment))
                else:
                    self._reply_err(query, reply.error)
            except Exception as e:   # noqa: BLE001
                log.exception("zenoh: get %s failed", key)
                self._reply_err(query, {"error": "internal_error", "detail": str(e)})
            del query, job          # a zenoh query is finalized when its last reference goes

    # ---- puts ------------------------------------------------------------------------
    def _on_sample(self, sample) -> None:
        key = str(sample.key_expr)
        kind = getattr(sample, "kind", None)
        if self._zenoh is not None and kind == self._zenoh.SampleKind.DELETE:
            self.store.handle_delete(key)
            return
        try:
            self._puts.put_nowait((key, _bytes(sample.payload)))
        except queue.Full:
            self.store.refuse("busy", f"{key}: {self._puts.maxsize} put(s) already waiting to be written")

    def _put_worker(self) -> None:
        while not self._stop.is_set():
            try:
                key, payload = self._puts.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.store.handle_put(key, payload)
            except Exception:   # noqa: BLE001
                log.exception("zenoh: put %s failed", key)

    # ---- publishers ------------------------------------------------------------------
    def _publish_event(self, run, entry) -> None:
        pub = self._events
        if pub is None:
            return
        try:
            pub.put(encode_json(self.store.entry_dict(entry, run)), encoding=self._encoding("application/json"))
        except Exception as e:   # noqa: BLE001
            log.debug("zenoh: event publish failed: %s", e)

    def _health_loop(self) -> None:
        period = float(self.store.settings["health_interval_s"])
        while not self._stop.wait(period):
            pub = self._health_pub
            if pub is None:
                continue
            try:
                pub.put(encode_json(self.store.health()), encoding=self._encoding("application/json"))
            except Exception as e:   # noqa: BLE001
                log.debug("zenoh: health publish failed: %s", e)
