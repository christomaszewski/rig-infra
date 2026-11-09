"""Zenoh keys <-> files in a run's store/ (docs/zenoh-store-plan.md "Contract").

    key:   fleet/<vid>/store/<run_id>/<producer>/<path...>/<name>.<ext>
    file:  <data_dir>/runs/<run_id>/store/<producer>/<path...>/<name>.<ext>

A store-relative path ("relpath") always has at least two chunks: the producer directory and a file
name. Every chunk must be usable as a Zenoh key chunk AND as a file name, so the rules are the union:
no empty chunks, nothing dot-prefixed (temp files and `..`), no wildcard / selector characters, no
verbatim (`@`) chunks, no control characters.
"""
from __future__ import annotations

import os
from typing import Optional

_FORBIDDEN = set("*$?#\\")


class KeyError_(ValueError):
    """A key or path outside the contract (the reply's `error` code is `bad_key`)."""


def bad_chunk(chunk: str) -> Optional[str]:
    """Why `chunk` cannot be one key chunk / file name, or None when it can."""
    if not chunk:
        return "empty chunk"
    if chunk.startswith("."):
        return "dot-prefixed chunk"
    if chunk.startswith("@"):
        return "verbatim (@) chunk"
    if any(c in _FORBIDDEN for c in chunk):
        return "wildcard or selector character"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in chunk):
        return "control character"
    try:
        chunk.encode("utf-8")
    except UnicodeEncodeError:
        return "not UTF-8"
    return None


def check_relpath(relpath: str) -> str:
    parts = relpath.split("/")
    if len(parts) < 2:
        raise KeyError_(f"{relpath!r}: expected <producer>/.../<file>, at least two chunks")
    for part in parts:
        why = bad_chunk(part)
        if why:
            raise KeyError_(f"{relpath!r}: {why}")
    return relpath


def check_run(run: str) -> str:
    why = bad_chunk(run) or ("contains '/'" if "/" in run else None)
    if why:
        raise KeyError_(f"run {run!r}: {why}")
    return run


def data_prefix(vehicle: str) -> str:
    return f"fleet/{vehicle}/store"


def data_key(vehicle: str, run: str, relpath: str) -> str:
    return f"{data_prefix(vehicle)}/{run}/{relpath}"


def is_wildcard(key: str) -> bool:
    return "*" in key or "$" in key


def parse_data_key(key: str, vehicle: str) -> tuple:
    """(run, relpath) for a concrete data key of this vehicle, or KeyError_."""
    prefix = data_prefix(vehicle) + "/"
    if not key.startswith(prefix):
        raise KeyError_(f"{key!r} is not under {prefix}")
    if is_wildcard(key):
        raise KeyError_(f"{key!r}: wildcard gets are not served; list files with the store's index")
    rest = key[len(prefix):]
    run, _, relpath = rest.partition("/")
    return check_run(run), check_relpath(relpath)


def relpath_of(root: str, path: str) -> Optional[str]:
    """The store-relative path of a file under `root`, or None when it falls outside the contract
    (outside the root, a dotfile or temp file, an unusable name)."""
    rel = os.path.relpath(path, root)
    if rel.startswith(".."):
        return None
    rel = rel.replace(os.sep, "/")
    try:
        check_relpath(rel)
    except KeyError_:
        return None
    return rel


def resolve(root: str, relpath: str) -> str:
    """The absolute file for a checked relpath, refusing anything that resolves outside `root`
    (a symlinked directory inside store/ must not expose the rest of the disk over the link)."""
    path = os.path.join(root, *relpath.split("/"))
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise KeyError_(f"{relpath!r} resolves outside the store")
    return path
