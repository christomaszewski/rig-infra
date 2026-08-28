"""The graph-snapshotter's pure core (tools/graph_snapshot.py) and the `graph:` config gate
(tools/bag_cmd.py). The epoch contract these pin down (rig-graph-capture-handoff §1.2): identity
hash over the canonical `nodes:` body only; unchanged graph -> the SAME file with `last:` bumped;
changed graph -> a NEW file, the old one never touched again; canonical ordering makes the hash
input-order-independent; the snapshotter excludes its OWN node and nothing else; same-second
filename collisions suffix `_<n>`. rig v0.2.32's reader is built against fixtures of exactly this
schema — a change that breaks these tests is a contract renegotiation, not a refactor.
Run: `python3 tests/test_graph_snapshot.py` (no ROS needed — rclpy imports lazily)."""
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import yaml

import bag_cmd
import graph_snapshot as gs

TALKER = {"pubs": [("/chatter", ["std_msgs/msg/String"]), ("/rosout", ["rcl_interfaces/msg/Log"])],
          "subs": [], "provides": [("/talker/reset", ["std_srvs/srv/Trigger"])], "requires": []}
LISTENER = {"pubs": [("/rosout", ["rcl_interfaces/msg/Log"])],
            "subs": [("/chatter", ["std_msgs/msg/String"])],
            "provides": [], "requires": [("/talker/reset", ["std_srvs/srv/Trigger"])]}
OBS = {"/talker": TALKER, "/listener": LISTENER}


def _shuffled(obs: dict) -> dict:
    """Same observation, different insertion/list order everywhere."""
    keys = list(obs)
    random.shuffle(keys)
    out = {}
    for k in keys:
        entry = {}
        for kind in ("requires", "provides", "subs", "pubs"):
            edges = list(obs[k].get(kind) or [])
            random.shuffle(edges)
            entry[kind] = edges
        out[k] = entry
    return out


# --- canonical form + identity hash --------------------------------------------------------------

def test_canonical_ordering_is_input_order_independent():
    want = gs.identity_hash(gs.canonicalize(OBS))
    for seed in range(5):
        random.seed(seed)
        assert gs.identity_hash(gs.canonicalize(_shuffled(OBS))) == want


def test_hash_covers_nodes_body_only():
    nodes = gs.canonicalize(OBS)
    h = gs.identity_hash(nodes)
    a = gs.render_epoch(nodes, "2026-08-27T10:00:00Z", "2026-08-27T10:00:00Z", "rmw_zenoh_cpp", 7)
    b = gs.render_epoch(nodes, "2026-08-27T11:00:00Z", "2026-08-27T12:34:56Z", "rmw_other", 0)
    # different stamps/rmw/domain, same body: the parsed nodes re-hash identically (the audit path)
    assert gs.identity_hash(yaml.safe_load(a)["nodes"]) == h
    assert gs.identity_hash(yaml.safe_load(b)["nodes"]) == h


def test_self_exclusion_only():
    me = "/graph_snapshotter_bag_logger"
    obs = dict(OBS)
    obs[me] = {"pubs": [("/rosout", ["rcl_interfaces/msg/Log"])], "subs": [], "provides": [],
               "requires": []}
    obs["/_hidden_recorder"] = {"pubs": [("/parameter_events", ["rcl_interfaces/msg/ParameterEvent"])],
                                "subs": [], "provides": [], "requires": []}
    nodes = gs.canonicalize(obs, self_fqn=me)
    assert me not in nodes                      # the one measurement artifact is dropped...
    assert "/_hidden_recorder" in nodes         # ...and NOTHING else — hidden nodes recorded raw
    # a graph differing only in the snapshotter's own node hashes identically
    with_me = dict(OBS, **{me: obs[me]})
    assert gs.identity_hash(gs.canonicalize(with_me, self_fqn=me)) \
        == gs.identity_hash(gs.canonicalize(OBS))


def test_multiple_types_on_one_name_become_one_entry_per_type():
    obs = {"/n": {"pubs": [("/t", ["pkg/msg/A", "pkg/msg/B"])], "subs": [], "provides": [],
                  "requires": []}}
    assert gs.canonicalize(obs)["/n"]["pubs"] == [{"topic": "/t", "type": "pkg/msg/A"},
                                                  {"topic": "/t", "type": "pkg/msg/B"}]


def test_fqn_root_and_nested_namespaces():
    assert gs.fqn("talker", "/") == "/talker"
    assert gs.fqn("novatel_node", "/gnss_primary") == "/gnss_primary/novatel_node"
    assert gs.fqn("n", "/a/b/") == "/a/b/n"


# --- epoch decision: same/changed/new-file, `last:` bump ------------------------------------------

def _tick(tr, obs, now, stamp, taken=()):
    return tr.tick(gs.canonicalize(obs), now, stamp, lambda f: f in taken)


def test_unchanged_graph_bumps_last_in_the_same_file():
    tr = gs.EpochTracker("rmw_zenoh_cpp", 7)
    a1, f1, t1 = _tick(tr, OBS, "2026-08-27T10:00:00Z", "20260827T100000Z")
    a2, f2, t2 = _tick(tr, _shuffled(OBS), "2026-08-27T10:01:00Z", "20260827T100100Z")
    assert (a1, a2) == ("open", "bump") and f1 == f2 == "epoch_20260827T100000Z.yaml"
    d1, d2 = yaml.safe_load(t1), yaml.safe_load(t2)
    assert d1["first"] == d1["last"]                      # a fresh epoch opens with first == last
    assert d2["first"] == d1["first"]                     # first: pinned to the epoch open...
    assert str(d2["last"]).startswith("2026-08-27 10:01:00")   # ...last: bumped (liveness)
    assert d2["nodes"] == d1["nodes"]


def test_changed_graph_opens_a_new_file():
    tr = gs.EpochTracker("rmw_zenoh_cpp", 7)
    _tick(tr, OBS, "2026-08-27T10:00:00Z", "20260827T100000Z")
    gone = {"/talker": TALKER}                            # listener stopped
    a, f, text = _tick(tr, gone, "2026-08-27T10:02:00Z", "20260827T100200Z")
    doc = yaml.safe_load(text)
    assert a == "open" and f == "epoch_20260827T100200Z.yaml"
    assert doc["first"] == doc["last"] and "/listener" not in doc["nodes"]
    # flap back: the original topology is a THIRD file, never a rewrite of the first
    a, f, _ = _tick(tr, OBS, "2026-08-27T10:03:00Z", "20260827T100300Z")
    assert a == "open" and f == "epoch_20260827T100300Z.yaml"


def test_filename_collision_suffixes():
    assert gs.epoch_filename("S", lambda f: False) == "epoch_S.yaml"
    taken = {"epoch_S.yaml", "epoch_S_1.yaml"}
    assert gs.epoch_filename("S", lambda f: f in taken) == "epoch_S_2.yaml"
    tr = gs.EpochTracker("rmw_zenoh_cpp", 0)              # same-second restart, pathological
    _, f, _ = _tick(tr, OBS, "2026-08-27T10:00:00Z", "20260827T100000Z",
                    taken={"epoch_20260827T100000Z.yaml"})
    assert f == "epoch_20260827T100000Z_1.yaml"


# --- the epoch file itself ------------------------------------------------------------------------

def test_epoch_file_matches_the_contract_schema():
    text = gs.render_epoch(gs.canonicalize(OBS), "2026-08-27T10:15:03Z", "2026-08-27T11:42:03Z",
                           "rmw_zenoh_cpp", 7)
    doc = yaml.safe_load(text)
    assert set(doc) == {"schema", "first", "last", "rmw", "domain", "nodes"}
    assert doc["schema"] == 1 and doc["rmw"] == "rmw_zenoh_cpp" and doc["domain"] == 7
    assert not isinstance(doc["first"], str)              # unquoted ISO8601 = a YAML timestamp,
    assert not isinstance(doc["last"], str)               # as the contract example shows
    assert set(doc["nodes"]) == {"/talker", "/listener"}
    assert set(doc["nodes"]["/talker"]) == {"pubs", "subs", "provides", "requires"}
    assert doc["nodes"]["/listener"]["subs"] == [{"topic": "/chatter", "type": "std_msgs/msg/String"}]
    assert doc["nodes"]["/listener"]["requires"] == [{"service": "/talker/reset",
                                                      "type": "std_srvs/srv/Trigger"}]
    assert doc["nodes"]["/talker"]["provides"] == [{"service": "/talker/reset",
                                                    "type": "std_srvs/srv/Trigger"}]
    empty = yaml.safe_load(gs.render_epoch({}, "2026-08-27T10:15:03Z", "2026-08-27T10:15:03Z",
                                           "rmw_zenoh_cpp", 0))
    assert empty["nodes"] == {}


# --- the `graph:` config gate (bag_cmd) -----------------------------------------------------------

def _expect_exit(cfg, needle):
    try:
        bag_cmd.graph_params(cfg)
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")
    except SystemExit as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


def test_graph_params_absent_block_is_off_and_inert():
    assert bag_cmd.graph_params({}) == (False, "", "")
    assert bag_cmd.graph_params({"name": "x", "record": {}}) == (False, "", "")


def test_graph_params_reads_the_block():
    assert bag_cmd.graph_params({"graph": {"enabled": True}}) == (True, "", "")
    assert bag_cmd.graph_params({"graph": {"enabled": True, "interval_s": 60, "settle_s": 5}}) \
        == (True, "60", "5")
    assert bag_cmd.graph_params({"graph": {"enabled": False, "interval_s": 30}}) == (False, "30", "")
    assert bag_cmd.graph_params({"graph": {"enabled": True, "settle_s": 2.5}}) == (True, "", "2.5")


def test_graph_params_is_strict():
    _expect_exit({"graph": []}, "must be a mapping")
    _expect_exit({"graph": {"enable": True}}, "unknown graph key")      # the typo that would
    _expect_exit({"graph": {"interval": 30}}, "unknown graph key")      # silently run defaults
    _expect_exit({"graph": {"enabled": True, "interval_s": 0}}, "interval_s")
    _expect_exit({"graph": {"enabled": True, "interval_s": "fast"}}, "interval_s")
    _expect_exit({"graph": {"enabled": True, "settle_s": -1}}, "settle_s")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
