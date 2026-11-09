"""The `store:` block of a zenoh-store instance config: defaults, validation, rendering.

Stdlib only, and importable as a plain script: the launcher runs `python3 zenoh_store/config.py
render <instance.yaml>` on the host (that path needs PyYAML, like every rig launcher) to validate the
config before compose ever runs and to hand the container one canonical JSON string
(ZENOH_STORE_CONFIG). Inside the image the service parses that JSON with `from_json`.
"""
from __future__ import annotations

import json
import sys

FETCH_MODES = ("auto", "latest", "on_request")

DEFAULTS = {
    "fetch": [],                 # rules, first match wins; unmatched files are on_request
    "default_priority": 100,     # priority of a file no rule matches (lower is fetched first)
    "mime": {},                  # extension -> encoding, on top of the built-in table
    "max_put_mb": 32,            # largest payload accepted on a put to a data key
    "max_reply_mb": 16,          # largest single reply (a whole file, or one range)
    "chunk_kb": 32,              # the chunk size the store suggests to fetchers over a slow link
    "settle_ms": 0,              # >0: announce an in-place-written file only once it stops changing
    "rescan_s": 300,             # safety-net rescan of the open run (inotify is the primary source)
    "sealed_ttl_s": 300,         # how long a sealed run's index is reused before a rescan
    "workers": 2,                # threads answering data gets
    "queue": 32,                 # data gets waiting for a worker before `busy` is replied
    "health_interval_s": 5,      # /health/state publish period
    "zenoh_connect": None,       # None: $ZENOH_CONNECT, else tcp/localhost:7447. "" = scout only
    # Where the store's peer session listens. Loopback: onboard sessions (host networking) reach it
    # directly, nothing off the vehicle does, and it works where IPv6 is disabled (Zenoh's own
    # default, tcp/[::]:0, fails to open there). "" = Zenoh's default.
    "zenoh_listen": "tcp/127.0.0.1:0",
}

_RULE_KEYS = {"match", "fetch", "priority", "max_mb"}


class ConfigError(ValueError):
    pass


def _number(block: dict, key: str, minimum: float, integer: bool = False, inclusive: bool = False):
    """block[key] when it is a number above `minimum` (or equal to it with inclusive=True)."""
    value = block[key]
    ok_type = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not ok_type or value < minimum or (value == minimum and not inclusive):
        kind = "an integer" if integer else "a number"
        raise ConfigError(f"store.{key}: expected {kind} {'>=' if inclusive else '>'} {minimum}, got {value!r}")
    return value


def _rule(i: int, raw) -> dict:
    where = f"store.fetch[{i}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping like {{match: '*/keyframes/*.jpg', fetch: auto}}")
    unknown = sorted(set(raw) - _RULE_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown} (allowed: {sorted(_RULE_KEYS)})")
    match = raw.get("match")
    if not isinstance(match, str) or not match.strip():
        raise ConfigError(f"{where}.match: expected a path glob relative to the run's store/")
    match = match.strip()
    if match.startswith("/") or any(part in ("..", ".") for part in match.split("/")):
        raise ConfigError(f"{where}.match: {match!r} must be relative to store/ (no leading '/', '.' or '..')")
    if match.count("{") != match.count("}") or match.count("[") != match.count("]"):
        raise ConfigError(f"{where}.match: {match!r} has an unclosed '{{' or '['")
    fetch = raw.get("fetch", "auto")
    if fetch not in FETCH_MODES:
        raise ConfigError(f"{where}.fetch: expected one of {list(FETCH_MODES)}, got {fetch!r}")
    rule = {"match": match, "fetch": fetch}
    if "priority" in raw:
        p = raw["priority"]
        if isinstance(p, bool) or not isinstance(p, int) or p < 0:
            raise ConfigError(f"{where}.priority: expected an integer >= 0, got {p!r}")
        rule["priority"] = p
    if "max_mb" in raw:
        m = raw["max_mb"]
        if isinstance(m, bool) or not isinstance(m, (int, float)) or m <= 0:
            raise ConfigError(f"{where}.max_mb: expected a number > 0, got {m!r}")
        rule["max_mb"] = m
    return rule


def validate(store) -> dict:
    """The effective store settings (defaults filled in) or ConfigError."""
    if store is None:
        store = {}
    if not isinstance(store, dict):
        raise ConfigError("store: expected a mapping")
    unknown = sorted(set(store) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"store: unknown key(s) {unknown} (allowed: {sorted(DEFAULTS)})")
    out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
           for k, v in DEFAULTS.items()}
    out.update(store)
    if not isinstance(out["fetch"], list):
        raise ConfigError("store.fetch: expected a list of rules")
    out["fetch"] = [_rule(i, r) for i, r in enumerate(out["fetch"])]
    dp = out["default_priority"]
    if isinstance(dp, bool) or not isinstance(dp, int) or dp < 0:
        raise ConfigError(f"store.default_priority: expected an integer >= 0, got {dp!r}")
    mime = out["mime"]
    if not isinstance(mime, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and "/" in v
                                             for k, v in mime.items()):
        raise ConfigError("store.mime: expected a mapping of extension -> 'type/subtype'")
    out["mime"] = {k.lower().lstrip("."): v for k, v in sorted(mime.items())}
    for key in ("max_put_mb", "max_reply_mb", "chunk_kb", "rescan_s", "sealed_ttl_s", "health_interval_s"):
        _number(out, key, 0)
    _number(out, "settle_ms", 0, inclusive=True)
    _number(out, "workers", 1, integer=True, inclusive=True)
    _number(out, "queue", 1, integer=True, inclusive=True)
    for key in ("zenoh_connect", "zenoh_listen"):
        value = out[key]
        if value is None and key == "zenoh_connect":
            continue
        if not isinstance(value, (str, list)) or (isinstance(value, list)
                                                   and not all(isinstance(e, str) for e in value)):
            raise ConfigError(f"store.{key}: expected a comma-separated string or a list of endpoints")
        if isinstance(value, list):
            out[key] = ",".join(e.strip() for e in value if e.strip())
    return out


def from_json(text: str) -> dict:
    try:
        raw = json.loads(text) if text and text.strip() else {}
    except ValueError as e:
        raise ConfigError(f"ZENOH_STORE_CONFIG is not JSON: {e}") from e
    return validate(raw)


def load_file(path: str) -> tuple:
    """(instance name, effective store settings) from an instance YAML (or JSON) file."""
    with open(path) as f:
        text = f.read()
    try:
        import yaml
        doc = yaml.safe_load(text)
    except ImportError:
        doc = json.loads(text)
    doc = doc or {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: expected a mapping")
    name = str(doc.get("name") or "zenoh-store")
    if not name or any(c in name for c in "/*$?#@ ") or name.startswith("."):
        raise ConfigError(f"name: {name!r} must be one Zenoh key chunk (no '/', '*', '$', '?', '#', '@', spaces)")
    return name, validate(doc.get("store"))


def render(settings: dict) -> str:
    return json.dumps(settings, sort_keys=True, separators=(",", ":"))


def main(argv) -> int:
    if len(argv) != 3 or argv[1] != "render":
        sys.stderr.write("usage: config.py render <instance.yaml>\n")
        return 2
    try:
        name, settings = load_file(argv[2])
    except (OSError, ConfigError, ValueError) as e:
        sys.stderr.write(f"zenoh-store: config error: {e}\n")
        return 2
    print(name + "\t" + render(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
