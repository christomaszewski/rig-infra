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
import stat
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


class OutsideRun(KeyError_):
    """A path whose links lead outside the directory it must stay in (served as not_found)."""


def within(real: str, real_bound: str) -> bool:
    return real == real_bound or real.startswith(real_bound + os.sep)


def follow(path: str, real_bound: str, real_store: str) -> tuple:
    """Where a symlink inside store/ leads: (stat of the target, real target, None), or
    (None, None, reason) when the store must not follow it. Links are followed only to a regular
    file or directory inside `real_bound` (the run directory), never to a directory that contains
    store/ itself (a cycle)."""
    try:
        real = os.path.realpath(path, strict=True)
        st = os.stat(real)
    except (OSError, RuntimeError):
        return None, None, "dangling link"
    if not within(real, real_bound):
        return None, None, "link outside the run"
    if stat.S_ISDIR(st.st_mode):
        if within(real_store, real):
            return None, None, "link to a directory containing store/"
    elif not stat.S_ISREG(st.st_mode):
        return None, None, "link to a special file"
    return st, real, None


def resolve(root: str, relpath: str, bound: Optional[str] = None) -> str:
    """The real file behind a checked relpath. Symlinks are followed only while they stay inside
    `bound` (the run directory, for gets) or `root` itself (the default, for puts): a stray link
    must not expose the rest of the disk over the radio, and a put never writes outside store/."""
    real = os.path.realpath(os.path.join(root, *relpath.split("/")))
    if not within(real, os.path.realpath(bound or root)):
        raise OutsideRun(f"{relpath!r} resolves outside the {'run' if bound else 'store'}")
    return real
