"""zenoh-store's live index with real inotify: temp-then-rename and in-place writers, new directories,
settle mode for writers that close more than once, removals, overflow rescans, and the store
announcing each completed file exactly once. Run: `python3 tests/test_store_watch.py` (Linux)."""
import os
import pathlib
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from zenoh_store import config, keys  # noqa: E402
from zenoh_store.inotify import IN_Q_OVERFLOW, TreeWatcher  # noqa: E402
from zenoh_store.store import Store  # noqa: E402

RUN = "20261109T120000Z_flight1"


class Recorder:
    def __init__(self):
        self.files, self.gone, self.rescans = [], [], 0
        self.cond = threading.Condition()

    def on_file(self, rel):
        with self.cond:
            self.files.append(rel)
            self.cond.notify_all()

    def on_gone(self, rel):
        with self.cond:
            self.gone.append(rel)
            self.cond.notify_all()

    def on_rescan(self):
        with self.cond:
            self.rescans += 1
            self.cond.notify_all()

    def wait(self, pred, timeout=3.0):
        with self.cond:
            ok = self.cond.wait_for(pred, timeout)
        assert ok, f"timed out: files={self.files} gone={self.gone} rescans={self.rescans}"


def rename_into_place(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_bytes(data)
    os.rename(tmp, path)


def test_watcher_reports_completed_files_and_new_dirs():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "store"
        rec = Recorder()
        w = TreeWatcher(str(root), rec.on_file, rec.on_gone, rec.on_rescan)
        w.start()
        try:
            rename_into_place(root / "cam" / "keyframes" / "1.jpg", b"j")       # new dirs + rename
            rec.wait(lambda: "cam/keyframes/1.jpg" in rec.files)
            (root / "cam" / "keyframes" / "2.json").write_text("{}")         # in place: close_write
            rec.wait(lambda: "cam/keyframes/2.json" in rec.files)
            # a whole directory tree moved in at once (its files predate any watch on it)
            staged = pathlib.Path(td) / "staged" / "lidar_top" / "clouds"
            staged.mkdir(parents=True)
            (staged / "a.laz").write_bytes(b"L")
            os.rename(pathlib.Path(td) / "staged" / "lidar_top", root / "lidar_top")
            rec.wait(lambda: "lidar_top/clouds/a.laz" in rec.files)
            (root / "lidar_top" / "clouds" / "b.laz").write_bytes(b"L")     # watched after the move
            rec.wait(lambda: "lidar_top/clouds/b.laz" in rec.files)
            os.unlink(root / "cam" / "keyframes" / "2.json")
            rec.wait(lambda: "cam/keyframes/2.json" in rec.gone)
            assert not any(f.startswith(".") or "/." in f for f in rec.files), rec.files
            assert rec.files.count("cam/keyframes/1.jpg") == 1
            assert w.watching == 5          # store, cam, cam/keyframes, lidar_top, lidar_top/clouds
        finally:
            w.stop()


def test_watcher_settle_waits_for_quiet():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "store"
        (root / "log").mkdir(parents=True)
        rec = Recorder()
        w = TreeWatcher(str(root), rec.on_file, rec.on_gone, rec.on_rescan, settle_ms=300)
        w.start()
        try:
            f = root / "log" / "events.csv"
            t0 = time.monotonic()
            for i in range(4):                      # a writer that reopens and appends
                with open(f, "a") as h:
                    h.write(f"{i}\n")
                time.sleep(0.1)
            rec.wait(lambda: "log/events.csv" in rec.files)
            assert time.monotonic() - t0 >= 0.3 + 0.3, "reported before the writer went quiet"
            assert rec.files == ["log/events.csv"]
            rename_into_place(root / "log" / "b.json", b"{}")   # a rename is complete: no wait
            t1 = time.monotonic()
            rec.wait(lambda: "log/b.json" in rec.files)
            assert time.monotonic() - t1 < 0.25
        finally:
            w.stop()


def test_overflow_asks_for_rescan():
    with tempfile.TemporaryDirectory() as td:
        rec = Recorder()
        w = TreeWatcher(str(pathlib.Path(td) / "store"), rec.on_file, rec.on_gone, rec.on_rescan)
        w.start()
        try:
            w.handle(-1, IN_Q_OVERFLOW, "")
            assert rec.rescans == 1 and w.overflows == 1
        finally:
            w.stop()


def test_store_announces_each_file_once():
    with tempfile.TemporaryDirectory() as td:
        data = pathlib.Path(td) / "data"
        (data / "runs" / RUN).mkdir(parents=True)
        (data / "current").symlink_to(f"runs/{RUN}")
        pre = data / "runs" / RUN / "store" / "cam_front" / "keyframes" / "0.jpg"
        pre.parent.mkdir(parents=True)
        pre.write_bytes(b"old")
        settings = config.validate({"fetch": [{"match": "*/keyframes/*.jpg", "priority": 20}]})
        st = Store(settings, str(data), "v1", "store")
        rec = Recorder()
        st.add_observer(lambda run, e: rec.on_file((run, e.path, e.size)))
        st.start()
        try:
            assert st.watch_mode == "inotify"
            store_dir = data / "runs" / RUN / "store"
            rename_into_place(store_dir / "cam_front" / "keyframes" / "1.jpg", b"jpeg")
            rec.wait(lambda: (RUN, "cam_front/keyframes/1.jpg", 4) in rec.files)
            r = st.handle_put(keys.data_key("v1", RUN, "lidar_top/clouds/1.laz"), b"L" * 9)
            assert r["ok"]
            rec.wait(lambda: (RUN, "lidar_top/clouds/1.laz", 9) in rec.files)
            time.sleep(0.3)                          # let the watcher see the put's own rename
            st.rescan()                              # and a safety-net rescan finds nothing new
            paths = [p for _, p, _ in rec.files]
            assert paths.count("cam_front/keyframes/1.jpg") == 1, rec.files
            assert paths.count("lidar_top/clouds/1.laz") == 1, rec.files
            assert "cam_front/keyframes/0.jpg" not in paths     # pre-existing: listed, not announced
            q = st.index_query("")
            assert [f["path"] for f in q["files"]] == ["cam_front/keyframes/0.jpg", "cam_front/keyframes/1.jpg",
                                                       "lidar_top/clouds/1.laz"]
            v = st.health()["status"][0]["values"]
            assert v["watch"] == "inotify" and v["files"] == 3
        finally:
            st.stop()


def test_store_falls_back_to_polling():
    import zenoh_store.store as store_mod
    from zenoh_store.inotify import InotifyUnavailable

    class NoInotify(TreeWatcher):
        def start(self):
            raise InotifyUnavailable("inotify is not available: test")

    with tempfile.TemporaryDirectory() as td:
        data = pathlib.Path(td) / "data"
        (data / "runs" / RUN).mkdir(parents=True)
        (data / "current").symlink_to(f"runs/{RUN}")
        saved, saved_poll = store_mod.TreeWatcher, store_mod.POLL_S
        store_mod.TreeWatcher, store_mod.POLL_S = NoInotify, 0.1
        try:
            st = Store(config.validate({}), str(data), "v1", "store")
            rec = Recorder()
            st.add_observer(lambda run, e: rec.on_file(e.path))
            st.start()
            assert st.watch_mode == "polling"
            rename_into_place(data / "runs" / RUN / "store" / "logger" / "a.txt", b"x")
            rec.wait(lambda: "logger/a.txt" in rec.files)
            assert st.health()["status"][0]["level"] == 1
            st.stop()
        finally:
            store_mod.TreeWatcher, store_mod.POLL_S = saved, saved_poll


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
