"""zenoh-store on real Zenoh sessions: data gets (whole, ranges, errors), the chunked resumable fetch
client, puts from a producer session, events, index paging, descriptor, health and liveliness.
Needs `pip install eclipse-zenoh==1.9.0`; skipped (exit 0) without it.
Run: `python3 tests/test_store_zenoh.py`. Shared memory and multicast are off, endpoints are IPv4."""
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import zenoh
except ImportError:
    print("skipped: eclipse-zenoh is not installed")
    sys.exit(0)

from zenoh_store import config, keys  # noqa: E402
from zenoh_store.client import StoreClient, StoreError  # noqa: E402
from zenoh_store.store import Store  # noqa: E402
from zenoh_store.zenoh_io import ZenohStore  # noqa: E402

VID = "v1"
RUN = "20261109T120000Z_flight1"
OLD = "20261108T090000Z_flight0"
COMMON = {"transport/shared_memory/enabled": False, "scouting/multicast/enabled": False}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def peer(connect_port: int):
    conf = zenoh.Config()
    conf.insert_json5("mode", '"peer"')
    conf.insert_json5("connect/endpoints", json.dumps([f"tcp/127.0.0.1:{connect_port}"]))
    conf.insert_json5("listen/endpoints", json.dumps([f"tcp/127.0.0.1:{free_port()}"]))
    for k, v in COMMON.items():
        conf.insert_json5(k, json.dumps(v))
    return zenoh.open(conf)


class Fixture:
    def __init__(self, td: pathlib.Path):
        self.data = td / "data"
        for run, ended in ((RUN, False), (OLD, True)):
            (self.data / "runs" / run).mkdir(parents=True)
            (self.data / "runs" / run / "manifest.yaml").write_text("ended: '2026-11-09T13:00:00Z'\n" if ended else "")
        (self.data / "current").symlink_to(f"runs/{RUN}")
        self.cloud = os.urandom(200_000)
        f = self.data / "runs" / OLD / "store" / "lidar_top" / "clouds" / "000001.laz"
        f.parent.mkdir(parents=True)
        f.write_bytes(self.cloud)
        settings = config.validate({"fetch": [{"match": "*/keyframes/*.json", "priority": 10},
                                              {"match": "*/keyframes/*.jpg", "priority": 20}],
                                    "max_reply_mb": 0.1, "health_interval_s": 0.2, "workers": 2})
        self.port = free_port()
        self.store = Store(settings, str(self.data), VID, "store")
        self.store.start()
        self.adapter = ZenohStore(self.store, [], extra_config={
            "listen/endpoints": [f"tcp/127.0.0.1:{self.port}"], **COMMON})
        self.adapter.start()
        assert self.adapter.active
        self.session = peer(self.port)
        self.client = StoreClient(self.session, VID, "store", timeout_s=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:          # wait for the peers to see each other
            try:
                self.client.describe()
                break
            except StoreError:
                time.sleep(0.1)

    def close(self):
        self.session.close()
        self.adapter.close()
        self.store.stop()


def with_fixture(fn):
    def run():
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(pathlib.Path(td))
            try:
                fn(fx)
            finally:
                fx.close()
    run.__name__ = fn.__name__
    return run


@with_fixture
def test_gets_ranges_and_errors(fx):
    c = fx.client
    data, att = c.get(OLD, "lidar_top/clouds/000001.laz", offset=1000, length=5000)
    assert data == fx.cloud[1000:6000] and att["size"] == 200_000 and att["offset"] == 1000
    assert att["fetch"] == "on_request" and att["priority"] == 100
    _, att = c.get(OLD, "lidar_top/clouds/000001.laz", meta=True)
    assert att["size"] == 200_000
    try:
        c.get(OLD, "lidar_top/clouds/000001.laz")              # 200 KB > max_reply_mb 0.1
        raise AssertionError("whole-file get over the cap succeeded")
    except StoreError as e:
        assert e.code == "too_large" and e.obj["size"] == 200_000
    for run, path, code in [(OLD, "lidar_top/clouds/nope.laz", "not_found"),
                            ("20000101T000000Z_x", "a/b.bin", "no_such_run")]:
        try:
            c.get(run, path)
            raise AssertionError(f"{path}: no error")
        except StoreError as e:
            assert e.code == code, e
    replies = list(fx.session.get(f"fleet/{VID}/store/{OLD}/lidar_top/**", timeout=5))
    assert len(replies) == 1 and replies[0].err is not None
    assert json.loads(replies[0].err.payload.to_bytes())["error"] == "bad_key"


@with_fixture
def test_chunked_fetch_resumes(fx):
    with tempfile.TemporaryDirectory() as out:
        dest = os.path.join(out, "fleet-runs", "000001.laz")
        os.makedirs(os.path.dirname(dest))
        with open(dest + ".part", "wb") as f:              # an interrupted earlier fetch
            f.write(fx.cloud[:70_000])
        calls = []
        size = fx.client.fetch(OLD, "lidar_top/clouds/000001.laz", dest, chunk=32 * 1024,
                               progress=lambda have, total: calls.append(have))
        assert size == 200_000 and open(dest, "rb").read() == fx.cloud
        assert calls[0] == 70_000 + 32 * 1024 and len(calls) == 4 and not os.path.exists(dest + ".part")


@with_fixture
def test_puts_events_and_index(fx):
    events, got = [], threading.Event()

    def on_event(sample):
        events.append(json.loads(sample.payload.to_bytes()))
        if len(events) >= 3:
            got.set()

    sub = fx.session.declare_subscriber(f"fleet/{VID}/svc/store/store/events", on_event)
    time.sleep(0.3)
    producer = peer(fx.port)
    try:
        time.sleep(0.3)
        k = keys.data_key(VID, RUN, "cam_front/keyframes/1762689600000000000.jpg")
        producer.put(k, b"\xff\xd8jpeg", encoding=zenoh.Encoding("image/jpeg"))
        producer.put(keys.data_key(VID, RUN, "cam_front/keyframes/1762689600000000000.json"), b"{}")
        producer.put(keys.data_key(VID, OLD, "cam_front/keyframes/x.jpg"), b"nope")       # sealed: refused
        producer.delete(k)                                                                 # refused
        f = fx.data / "runs" / RUN / "store" / "lidar_top" / "clouds" / "000002.laz"       # a file writer
        f.parent.mkdir(parents=True)
        tmp = f.parent / ".000002.laz.tmp"
        tmp.write_bytes(b"L" * 1234)
        os.rename(tmp, f)
        assert got.wait(5), events
    finally:
        producer.close()
        sub.undeclare()
    by_path = {e["path"]: e for e in events}
    assert set(by_path) == {"cam_front/keyframes/1762689600000000000.jpg",
                            "cam_front/keyframes/1762689600000000000.json", "lidar_top/clouds/000002.laz"}
    jpg = by_path["cam_front/keyframes/1762689600000000000.jpg"]
    assert jpg["run"] == RUN and jpg["fetch"] == "auto" and jpg["priority"] == 20 and jpg["size"] == 6
    assert by_path["lidar_top/clouds/000002.laz"]["fetch"] == "on_request"
    assert (fx.data / "runs" / RUN / "store" / "cam_front" / "keyframes" / "1762689600000000000.jpg").exists()
    assert not (fx.data / "runs" / OLD / "store" / "cam_front").exists()
    assert fx.store.counters["puts_refused"] == 2
    data, att = fx.client.get(RUN, "cam_front/keyframes/1762689600000000000.jpg")
    assert data == b"\xff\xd8jpeg" and att["fetch"] == "auto"
    autos = list(fx.client.list_all(fetch="auto"))
    assert [a["path"] for a in autos] == ["cam_front/keyframes/1762689600000000000.jpg",
                                          "cam_front/keyframes/1762689600000000000.json"]
    page = fx.client.index(limit=1)
    assert page["more"] and len(page["files"]) == 1
    rest = fx.client.index(after=page["next"])
    assert len(rest["files"]) == 2 and not rest["more"]
    last = max(e["cursor"] for e in events)
    assert fx.client.index(after=last)["files"] == []          # an event's cursor resumes the index


@with_fixture
def test_descriptor_health_and_liveliness(fx):
    d = fx.client.describe()
    assert d["open_run"] == RUN and d["vehicle"] == VID and d["instance"] == "store"
    assert {r["run"]: r["state"] for r in d["runs"]} == {RUN: "open", OLD: "sealed"}
    tokens = [str(r.ok.key_expr) for r in fx.session.liveliness().get(f"fleet/{VID}/svc/*/**", timeout=3)]
    assert sorted(tokens) == ["fleet/v1/svc/store/health", "fleet/v1/svc/store/store"], tokens
    snap = json.loads(next(iter(fx.session.get(f"fleet/{VID}/svc/store/health", timeout=3))).ok.payload.to_bytes())
    assert snap["service"] == "zenoh-store" and [s["name"] for s in snap["status"]] == ["store: store", "store: disk"]
    states = []
    sub = fx.session.declare_subscriber(f"fleet/{VID}/svc/store/health/state",
                                        lambda s: states.append(json.loads(s.payload.to_bytes())))
    time.sleep(0.8)
    sub.undeclare()
    assert len(states) >= 2 and states[-1]["instance"] == "store"


@with_fixture
def test_busy_when_the_get_queue_is_full(fx):
    fx.adapter._stop.set()                        # park the workers: nothing drains the queue
    time.sleep(0.6)
    for _ in range(fx.store.settings["queue"]):
        fx.adapter._gets.put_nowait((None, "", ""))
    replies = list(fx.session.get(keys.data_key(VID, OLD, "lidar_top/clouds/000001.laz") + "?meta", timeout=3))
    assert replies and json.loads(replies[0].err.payload.to_bytes())["error"] == "busy"
    while not fx.adapter._gets.empty():
        fx.adapter._gets.get_nowait()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                t0 = time.monotonic()
                fn()
                print(f"ok   {name} ({time.monotonic() - t0:.2f}s)")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
