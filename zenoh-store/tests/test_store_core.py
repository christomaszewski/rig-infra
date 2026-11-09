"""zenoh-store without Zenoh: config validation, key/path rules, fetch rules, the run registry, the
index and its cursors, range reads, puts, and the store's replies, health and descriptor.
Run: `python3 tests/test_store_core.py` (stdlib only; PyYAML for the launcher render check)."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from zenoh_store import config, keys  # noqa: E402
from zenoh_store.files import RangeError, TooLarge, read_range, write_atomic  # noqa: E402
from zenoh_store.index import RunIndex  # noqa: E402
from zenoh_store.policy import Policy, glob_regex  # noqa: E402
from zenoh_store.runs import FLAT_RUN, Registry, manifest_sealed  # noqa: E402
from zenoh_store.store import Store, parse_parameters  # noqa: E402

VID = "v1"
RUN = "20261109T120000Z_flight1"
OLD = "20261108T090000Z_flight0"


def raises(exc, fn, *a, match=None, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        assert match is None or match in str(e), f"{match!r} not in {e!r}"
        return e
    raise AssertionError(f"{fn.__name__} did not raise {exc.__name__}")


def write(path: pathlib.Path, data: bytes, mtime_ns=None) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def registry(tmp: pathlib.Path, sealed_old=True) -> pathlib.Path:
    """A rig data dir: an open run (current) and an older sealed one, each with a store/."""
    data = tmp / "data"
    for run, ended in ((RUN, False), (OLD, sealed_old)):
        d = data / "runs" / run
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(f"run: {run}\nstarted: '2026-11-09T12:00:00Z'\n"
                                         + ("ended: '2026-11-09T13:00:00Z'\n" if ended else ""))
    (data / "current").symlink_to(f"runs/{RUN}")
    write(data / "runs" / OLD / "store" / "cam_front" / "keyframes" / "1.jpg", b"J" * 100)
    return data


def settings(**over):
    s = config.validate({"fetch": [{"match": "*/keyframes/*.json", "fetch": "auto", "priority": 10},
                                   {"match": "*/keyframes/*.jpg", "fetch": "auto", "priority": 20},
                                   {"match": "lidar_*/clouds/*.laz", "fetch": "auto", "max_mb": 1},
                                   {"match": "lidar_*/snap/*.laz", "fetch": "latest", "priority": 40}],
                         **over})
    return s


def make_store(data, **over) -> Store:
    clock = iter(range(10**18, 10**18 + 10**12, 10**9))
    return Store(settings(**over), str(data), VID, "store", clock=lambda: next(clock), epoch="e1")


# ---- config ------------------------------------------------------------------------------
def test_config_defaults_and_strictness():
    s = config.validate(None)
    assert s["fetch"] == [] and s["max_put_mb"] == 32 and s["chunk_kb"] == 32 and s["zenoh_connect"] is None
    raises(config.ConfigError, config.validate, {"fetch_rules": []}, match="unknown key")
    raises(config.ConfigError, config.validate, {"fetch": [{"match": "*.jpg", "fetch": "always"}]}, match="fetch")
    raises(config.ConfigError, config.validate, {"fetch": [{"match": "/abs/*.jpg"}]}, match="relative")
    raises(config.ConfigError, config.validate, {"fetch": [{"match": "a/../*.jpg"}]}, match="relative")
    raises(config.ConfigError, config.validate, {"fetch": [{"match": "*.{jpg,json"}]}, match="unclosed")
    raises(config.ConfigError, config.validate, {"fetch": [{"match": "*.jpg", "pri": 1}]}, match="unknown key")
    raises(config.ConfigError, config.validate, {"fetch": [{"match": "*.jpg", "max_mb": 0}]}, match="max_mb")
    raises(config.ConfigError, config.validate, {"max_put_mb": 0}, match="max_put_mb")
    raises(config.ConfigError, config.validate, {"workers": 0}, match="workers")
    raises(config.ConfigError, config.validate, {"settle_ms": -1}, match="settle_ms")
    raises(config.ConfigError, config.validate, {"mime": {"pcd": "pcd"}}, match="mime")
    assert config.validate({"settle_ms": 0})["settle_ms"] == 0
    assert config.validate({"fetch": [{"match": "*.jpg"}]})["fetch"] == [{"match": "*.jpg", "fetch": "auto"}]
    assert config.validate({"mime": {".PCD": "application/x-pcd"}})["mime"] == {"pcd": "application/x-pcd"}
    assert config.validate({"zenoh_connect": ["tcp/a:1", " tcp/b:2 "]})["zenoh_connect"] == "tcp/a:1,tcp/b:2"
    assert config.validate(None)["zenoh_listen"] == "tcp/127.0.0.1:0"
    assert config.validate({"zenoh_listen": ""})["zenoh_listen"] == ""
    raises(config.ConfigError, config.validate, {"zenoh_listen": None}, match="zenoh_listen")
    raises(config.ConfigError, config.validate, {"zenoh_connect": [1]}, match="zenoh_connect")


def test_config_render_is_canonical_and_round_trips():
    s = settings()
    text = config.render(s)
    assert config.render(config.from_json(text)) == text
    raises(config.ConfigError, config.from_json, "{nope", match="not JSON")


def test_launcher_render_cli():
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("     (skipped: PyYAML not installed)")
        return
    with tempfile.TemporaryDirectory() as td:
        cfg = pathlib.Path(td) / "s.yaml"
        cfg.write_text("service: zenoh-store\nname: st\nstore:\n  fetch:\n    - {match: '*/k/*.jpg'}\n")
        out = subprocess.run([sys.executable, str(HERE.parent / "zenoh_store" / "config.py"), "render", str(cfg)],
                             capture_output=True, text=True, check=True).stdout
        name, js = out.rstrip("\n").split("\t", 1)
        assert name == "st" and json.loads(js)["fetch"] == [{"match": "*/k/*.jpg", "fetch": "auto"}]
        cfg.write_text("name: st\nstore: {fetch: [{match: '*.jpg', fetch: sometimes}]}\n")
        r = subprocess.run([sys.executable, str(HERE.parent / "zenoh_store" / "config.py"), "render", str(cfg)],
                           capture_output=True, text=True)
        assert r.returncode == 2 and "store.fetch[0].fetch" in r.stderr, r.stderr
        cfg.write_text("name: 'a/b'\n")
        r = subprocess.run([sys.executable, str(HERE.parent / "zenoh_store" / "config.py"), "render", str(cfg)],
                           capture_output=True, text=True)
        assert r.returncode == 2 and "key chunk" in r.stderr


def test_example_config_validates():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return
    name, s = config.load_file(str(HERE.parent / "config" / "zenoh-store.example.yaml"))
    assert name == "zenoh-store"
    pol = Policy.from_settings(s)
    assert pol.classify("cam_front/keyframes/1762689600000000000.jpg", 3_000_000) == ("auto", 20)
    assert pol.classify("cam_front/keyframes/1762689600000000000.json", 900) == ("auto", 10)
    assert pol.classify("cam_thermal/keyframes/1762689600000000000.tif", 655_000)[0] == "on_request"
    assert pol.classify("lidar_top/clouds/000123.laz", 30_000_000)[0] == "on_request"


# ---- keys --------------------------------------------------------------------------------
def test_data_keys_round_trip_and_refusals():
    k = keys.data_key(VID, RUN, "cam_front/keyframes/1.jpg")
    assert k == f"fleet/v1/store/{RUN}/cam_front/keyframes/1.jpg"
    assert keys.parse_data_key(k, VID) == (RUN, "cam_front/keyframes/1.jpg")
    for bad, why in [(f"fleet/v2/store/{RUN}/a/b.jpg", "not under"),
                     (f"fleet/v1/store/{RUN}/a/*.jpg", "wildcard"),
                     (f"fleet/v1/store/{RUN}/**", "wildcard"),
                     (f"fleet/v1/store/{RUN}/b.jpg", "two chunks"),
                     (f"fleet/v1/store/{RUN}/a/.b.jpg.tmp", "dot-prefixed"),
                     (f"fleet/v1/store/{RUN}/a/../../etc/passwd", "dot-prefixed"),
                     (f"fleet/v1/store/{RUN}/a//b.jpg", "empty"),
                     (f"fleet/v1/store/{RUN}/@a/b.jpg", "verbatim"),
                     ("fleet/v1/store/.hidden/a/b.jpg", "dot-prefixed")]:
        raises(keys.KeyError_, keys.parse_data_key, bad, VID, match=why)


def test_resolve_refuses_symlink_escape():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "store"
        (root / "cam").mkdir(parents=True)
        outside = pathlib.Path(td) / "secret"
        outside.mkdir()
        (outside / "key.pem").write_text("x")
        (root / "evil").symlink_to(outside)
        assert keys.resolve(str(root), "cam/a.jpg") == str(root / "cam" / "a.jpg")
        raises(keys.KeyError_, keys.resolve, str(root), "evil/key.pem", match="outside")


# ---- policy ------------------------------------------------------------------------------
def test_glob_semantics():
    def m(pat, path):
        return bool(glob_regex(pat).match(path))
    assert m("*/keyframes/*.jpg", "cam/keyframes/1.jpg")
    assert not m("*/keyframes/*.jpg", "cam/sub/keyframes/1.jpg")      # * stays in one level
    assert m("**/*.jpg", "a/b/c.jpg") and m("**/*.jpg", "c.jpg")       # ** spans levels, incl. none
    assert m("cam/**/x.laz", "cam/x.laz") and m("cam/**/x.laz", "cam/a/b/x.laz")
    assert m("*/keyframes/*.{jpg,json}", "c/keyframes/1.json") and not m("*/keyframes/*.{jpg,json}", "c/keyframes/1.tif")
    assert m("lidar_?/*.laz", "lidar_1/a.laz") and not m("lidar_?/*.laz", "lidar_12/a.laz")
    assert m("[ab]*/x", "alpha/x") and not m("[!ab]*/x", "alpha/x")
    assert not m("*.jpg", "a.jpg.part") and m("a.b/*", "a.b/c") and not m("a.b/*", "aXb/c")


def test_classify_first_match_default_and_max_mb():
    pol = Policy.from_settings(settings())
    assert pol.classify("cam/keyframes/1.json", 10) == ("auto", 10)
    assert pol.classify("cam/keyframes/1.tif", 10) == ("on_request", 100)
    assert pol.classify("lidar_top/clouds/1.laz", 1048576) == ("auto", 100)
    assert pol.classify("lidar_top/clouds/2.laz", 1048577) == ("on_request", 100)   # max_mb demotes
    assert pol.classify("lidar_top/snap/2.laz", 10) == ("latest", 40)
    assert pol.classify("anything/else.bin", 1) == ("on_request", 100)


def test_mime_table():
    pol = Policy(mime={"pcd": "application/vnd.pcd", "xyz": "text/x-xyz"})
    assert pol.mime("a/b.JPG") == "image/jpeg" and pol.mime("a/b.tif") == "image/tiff"
    assert pol.mime("a/b.laz") == "application/vnd.laszip" and pol.mime("a/b.pcd") == "application/vnd.pcd"
    assert pol.mime("a/b.xyz") == "text/x-xyz"
    assert pol.mime("a/README") == "application/octet-stream" and pol.mime("a/b.weird") == "application/octet-stream"


# ---- registry ----------------------------------------------------------------------------
def test_registry_states_and_flat_mode():
    assert manifest_sealed("run: x\nended: '2026-01-01T00:00:00Z'\n")
    assert not manifest_sealed("run: x\nended:\n") and not manifest_sealed("run: x\nended: null\n")
    assert not manifest_sealed("run: x\n  ended: nested\n")
    with tempfile.TemporaryDirectory() as td:
        data = registry(pathlib.Path(td))
        (data / "runs" / "20261107T000000Z_crashed").mkdir()
        reg = Registry(str(data))
        assert reg.current() == RUN and not reg.flat
        states = {r.run: (r.state, r.has_store) for r in reg.runs(RUN)}
        assert states == {RUN: ("open", False), OLD: ("sealed", True),
                          "20261107T000000Z_crashed": ("interrupted", False)}
        (data / "current").unlink()
        (data / "current").symlink_to("/etc")
        assert reg.current() is None                       # a link outside runs/ is no open run
    with tempfile.TemporaryDirectory() as td:
        reg = Registry(td)
        assert reg.flat and reg.current() == FLAT_RUN and reg.store_dir(FLAT_RUN) == os.path.join(td, "store")


# ---- index -------------------------------------------------------------------------------
def test_index_scan_orders_by_mtime_then_lists_after_cursor():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "store"
        write(root / "cam" / "k" / "b.jpg", b"b" * 2, mtime_ns=2_000)
        write(root / "cam" / "k" / "a.jpg", b"a" * 3, mtime_ns=3_000)
        write(root / "cam" / "k" / ".c.jpg.tmp", b"tmp")              # temp: ignored
        write(root / "cam" / ".hidden" / "x.jpg", b"x")              # hidden dir: ignored
        write(root / "cam" / "k" / "bad#name.jpg", b"x")             # unusable: skipped, reported
        write(root / "top.jpg", b"x")                                # no producer dir: skipped
        idx = RunIndex(RUN, str(root), "e1")
        changed = idx.scan(1.0)
        assert [e.path for e in changed] == ["cam/k/b.jpg", "cam/k/a.jpg"]
        assert idx.bytes == 5 and idx.skipped == {"cam/k/bad#name.jpg", "top.jpg"}
        files, nxt, more = idx.list(0, limit=1)
        assert [e.path for e in files] == ["cam/k/b.jpg"] and more and nxt == "e1.1"
        after, reset = idx.parse_cursor(nxt)
        files, nxt, more = idx.list(after)
        assert [e.path for e in files] == ["cam/k/a.jpg"] and not more and nxt == "e1.2"
        assert idx.scan(2.0) == []                                   # unchanged: no new seqs
        write(root / "cam" / "k" / "b.jpg", b"b" * 10, mtime_ns=4_000)   # grew: re-announced
        write(root / "lidar" / "c.laz", b"L" * 7, mtime_ns=1_000)        # old mtime: still new
        changed = idx.scan(3.0)
        assert [e.path for e in changed] == ["lidar/c.laz", "cam/k/b.jpg"]
        files, nxt, _ = idx.list(idx.parse_cursor("e1.2")[0])
        assert [(e.path, e.size) for e in files] == [("lidar/c.laz", 7), ("cam/k/b.jpg", 10)]
        files, _, _ = idx.list(0)                                    # the superseded seq is gone
        assert [e.path for e in files] == ["cam/k/a.jpg", "lidar/c.laz", "cam/k/b.jpg"]
        assert idx.bytes == 20


def test_index_cursor_reset_and_filters():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "store"
        for i in range(6):
            write(root / ("cam" if i % 2 else "lidar") / f"{i}.bin", b"x" * i, mtime_ns=i + 1)
        idx = RunIndex(RUN, str(root), "e2")
        idx.scan(0.0)
        assert idx.parse_cursor(None) == (0, False)
        assert idx.parse_cursor("e1.3") == (0, True)                  # another epoch: start over
        assert idx.parse_cursor("e2.99") == (0, True)                 # from the future: start over
        assert idx.parse_cursor("junk") == (0, True)
        files, nxt, more = idx.list(0, prefix="cam/", limit=2)
        assert [e.path for e in files] == ["cam/1.bin", "cam/3.bin"] and more
        files, nxt, more = idx.list(idx.parse_cursor(nxt)[0], prefix="cam/", limit=2)
        assert [e.path for e in files] == ["cam/5.bin"] and not more and nxt == "e2.6"
        files, nxt, more = idx.list(0, accept=lambda e: e.size >= 4)
        assert [e.path for e in files] == ["lidar/4.bin", "cam/5.bin"]
        idx.forget("cam/5.bin")
        idx.forget_prefix("lidar/")
        assert [e.path for e in idx.list(0)[0]] == ["cam/1.bin", "cam/3.bin"] and idx.bytes == 4


def test_index_compacts_superseded_entries():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "store"
        f = write(root / "log" / "a.txt", b"")
        idx = RunIndex(RUN, str(root), "e")
        for i in range(3000):
            f.write_bytes(b"x" * (i + 1))
            idx.note("log/a.txt")
        assert len(idx._order) < 2 * len(idx) + 1025
        assert [(e.path, e.size) for e in idx.list(0)[0]] == [("log/a.txt", 3000)]


# ---- files -------------------------------------------------------------------------------
def test_read_range_and_caps():
    with tempfile.TemporaryDirectory() as td:
        p = write(pathlib.Path(td) / "f.bin", bytes(range(256)) * 4)
        data, size, mtime = read_range(str(p), 0, None, 2048)
        assert len(data) == 1024 and size == 1024 and mtime == p.stat().st_mtime_ns
        assert read_range(str(p), 1000, 100, 2048)[0] == (bytes(range(256)) * 4)[1000:]
        assert read_range(str(p), 1024, 10, 2048)[0] == b""           # at EOF: empty, not an error
        raises(RangeError, read_range, str(p), 1025, 1, 2048, match="outside")
        raises(TooLarge, read_range, str(p), 0, None, 1000, match="capped")
        os.symlink(p, pathlib.Path(td) / "link.bin")
        raises(OSError, read_range, str(pathlib.Path(td) / "link.bin"), 0, None, 2048)


def test_write_atomic_refuses_overwrite_and_leaves_no_temp():
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "a" / "b" / "c.json"
        write_atomic(str(p), b"{}")
        assert p.read_bytes() == b"{}"
        raises(FileExistsError, write_atomic, str(p), b"{1}")
        assert p.read_bytes() == b"{}" and sorted(os.listdir(p.parent)) == ["c.json"]


# ---- store -------------------------------------------------------------------------------
def test_parse_parameters():
    assert parse_parameters("offset=10;length=5;meta") == {"offset": "10", "length": "5", "meta": ""}
    assert parse_parameters("a=1&b=2") == {"a": "1", "b": "2"} and parse_parameters(None) == {}


def test_get_whole_range_meta_sha_and_errors():
    with tempfile.TemporaryDirectory() as td:
        data = registry(pathlib.Path(td))
        st = make_store(data, max_reply_mb=0.001)                   # ~1 KB replies
        k = keys.data_key(VID, OLD, "cam_front/keyframes/1.jpg")
        r = st.handle_get(k)
        assert r.ok and r.payload == b"J" * 100 and r.encoding == "image/jpeg"
        assert r.attachment["size"] == 100 and r.attachment["fetch"] == "auto" and r.attachment["priority"] == 20
        r = st.handle_get(k, "offset=90;length=50")
        assert r.payload == b"J" * 10 and r.attachment["offset"] == 90
        r = st.handle_get(k, "meta;sha256")
        assert r.ok and r.payload == b"" and len(r.attachment["sha256"]) == 64
        big = write(data / "runs" / OLD / "store" / "lidar_top" / "clouds" / "1.laz", b"L" * 5000)
        r = st.handle_get(keys.data_key(VID, OLD, "lidar_top/clouds/1.laz"))
        assert not r.ok and r.error["error"] == "too_large" and r.error["size"] == 5000
        r = st.handle_get(keys.data_key(VID, OLD, "lidar_top/clouds/1.laz"), "offset=4096;length=1000")
        assert r.ok and len(r.payload) == 904 and r.encoding == "application/vnd.laszip"
        assert r.attachment["fetch"] == "auto"                       # 5 KB < max_mb 1
        for key, params, code in [
            (keys.data_key(VID, OLD, "cam_front/keyframes/2.jpg"), "", "not_found"),
            (keys.data_key(VID, "20000101T000000Z_nope", "a/b.jpg"), "", "no_such_run"),
            (k, "offset=101", "bad_range"), (k, "offset=x", "bad_range"), (k, "length=0", "bad_range"),
            (f"fleet/{VID}/store/{OLD}/cam_front/**", "", "bad_key"),
            (keys.data_key(VID, OLD, "cam_front/keyframes"), "", "not_found")]:   # a directory
            r = st.handle_get(key, params)
            assert not r.ok and r.error["error"] == code, (key, params, r.error)
        assert st.counters["gets_failed"] == 8 and st.counters["bytes_served"] == 100 + 10 + 904
        os.symlink(big, data / "runs" / OLD / "store" / "lidar_top" / "clouds" / "alias.laz")
        r = st.handle_get(keys.data_key(VID, OLD, "lidar_top/clouds/alias.laz"), "offset=0;length=10")
        assert not r.ok and r.error["error"] == "not_found"


def test_put_open_run_only_and_events():
    with tempfile.TemporaryDirectory() as td:
        data = registry(pathlib.Path(td))
        st = make_store(data, max_put_mb=0.001)
        seen = []
        st.add_observer(lambda run, e: seen.append((run, e.path, e.size)))
        k = keys.data_key(VID, RUN, "lidar_top/clouds/000001.laz")
        assert st.handle_put(k, b"L" * 500) == {"ok": True, "path": "lidar_top/clouds/000001.laz"}
        f = data / "runs" / RUN / "store" / "lidar_top" / "clouds" / "000001.laz"
        assert f.read_bytes() == b"L" * 500 and seen == [(RUN, "lidar_top/clouds/000001.laz", 500)]
        assert st.handle_put(k, b"L" * 500)["duplicate"] and len(seen) == 1
        r = st.handle_get(f"fleet/{VID}/store/open/lidar_top/clouds/000001.laz")   # `open` alias
        assert r.ok and r.payload == b"L" * 500
        assert st.handle_put(f"fleet/{VID}/store/open/a/b.bin", b"x")["error"] == "not_open_run"
        assert st.handle_put(k, b"M" * 400)["error"] == "exists"
        assert st.handle_put(keys.data_key(VID, OLD, "a/b.bin"), b"x")["error"] == "not_open_run"
        assert st.handle_put(keys.data_key(VID, RUN, "a/b.bin"), b"x" * 2000)["error"] == "too_large"
        assert st.handle_put(f"fleet/{VID}/store/{RUN}/a/*.bin", b"x")["error"] == "bad_key"
        assert st.handle_delete(k)["error"] == "read_only" and f.exists()
        assert st.counters["puts"] == 1 and st.counters["puts_refused"] == 6
        assert st.health()["level"] == 1                                # a recent refusal warns


def test_index_query_and_descriptor():
    with tempfile.TemporaryDirectory() as td:
        data = registry(pathlib.Path(td))
        st = make_store(data)
        for i in range(3):
            st.handle_put(keys.data_key(VID, RUN, f"cam_front/keyframes/{i}.jpg"), b"j" * 10)
            st.handle_put(keys.data_key(VID, RUN, f"cam_front/keyframes/{i}.tif"), b"t" * 20)
        q = st.index_query("fetch=auto;limit=2")
        assert q["run"] == RUN and q["state"] == "open" and q["more"] and not q["reset"]
        assert [f["path"] for f in q["files"]] == ["cam_front/keyframes/0.jpg", "cam_front/keyframes/1.jpg"]
        q2 = st.index_query(f"after={q['next']};fetch=auto")
        assert [f["path"] for f in q2["files"]] == ["cam_front/keyframes/2.jpg"] and not q2["more"]
        q3 = st.index_query(f"after={q2['next']}")
        assert q3["files"] == [] and q3["next"] == q2["next"]
        q4 = st.index_query("fetch=on_request")
        assert [f["fetch"] for f in q4["files"]] == ["on_request"] * 3
        q5 = st.index_query(f"run={OLD}")
        assert q5["state"] == "sealed" and [f["path"] for f in q5["files"]] == ["cam_front/keyframes/1.jpg"]
        assert st.index_query("after=other.5")["reset"]
        assert st.index_query("run=20000101T000000Z_x")["error"] == "no_such_run"
        assert st.index_query("fetch=sometimes")["error"] == "bad_request"
        assert st.index_query("limit=0")["error"] == "bad_request"
        d = st.descriptor()
        assert d["open_run"] == RUN and d["put"]["enabled"] and d["data_key"] == "fleet/v1/store"
        runs = {r["run"]: r for r in d["runs"]}
        assert runs[RUN]["files"] == 6 and runs[OLD]["state"] == "sealed"
        assert d["fetch"]["rules"][0] == {"match": "*/keyframes/*.json", "fetch": "auto", "priority": 10}
        json.dumps(d, allow_nan=False)


def test_no_open_run_serves_read_only():
    with tempfile.TemporaryDirectory() as td:
        data = registry(pathlib.Path(td))
        (data / "current").unlink()
        st = make_store(data)
        assert st.open_run is None
        assert st.handle_get(keys.data_key(VID, OLD, "cam_front/keyframes/1.jpg")).ok
        assert st.handle_put(keys.data_key(VID, RUN, "a/b.bin"), b"x")["error"] == "not_open_run"
        assert st.index_query("")["error"] == "no_such_run"
        h = st.health()
        assert h["status"][0]["level"] == 1 and "put refused" in h["status"][0]["message"]


def test_health_shape():
    with tempfile.TemporaryDirectory() as td:
        data = registry(pathlib.Path(td))
        st = make_store(data)
        st.handle_put(keys.data_key(VID, RUN, "cam_front/keyframes/1.jpg"), b"j" * 10)
        h = st.health()
        assert h["schema_version"] == 1 and h["service"] == "zenoh-store" and h["instance"] == "store"
        assert [s["name"] for s in h["status"]] == ["store: store", "store: disk"]
        for s in h["status"]:
            assert set(s) == {"level", "name", "message", "hardware_id", "values"} and 0 <= s["level"] <= 3
            assert all(v is None or isinstance(v, (int, float, str, bool)) for v in s["values"].values())
        v = h["status"][0]["values"]
        assert v["files"] == 1 and v["files.cam_front"] == 1 and v["run"] == RUN
        assert "disk.free_pct" in h["status"][1]["values"]
        json.dumps(h, allow_nan=False)


def test_rescan_reports_drift_and_new_files():
    with tempfile.TemporaryDirectory() as td:
        data = registry(pathlib.Path(td))
        st = make_store(data)
        seen = []
        st.add_observer(lambda run, e: seen.append(e.path))
        st.index(RUN)                                                   # the startup scan
        write(data / "runs" / RUN / "store" / "logger" / "a.csv", b"1,2\n")
        st.rescan()
        assert seen == ["logger/a.csv"]
        (data / "current").unlink()
        (data / "current").symlink_to(f"runs/{OLD}")
        st.rescan()
        assert st.run_drift == OLD and st.open_run == RUN and st.health()["level"] >= 1


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
