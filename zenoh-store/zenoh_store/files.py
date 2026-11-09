"""Reading byte ranges and writing files the way the store contract asks producers to."""
from __future__ import annotations

import hashlib
import os
import stat
import threading


class RangeError(ValueError):
    pass


class TooLarge(RangeError):
    """The range is valid but bigger than one reply may carry."""


def read_range(path: str, offset: int, length: int | None, max_bytes: int) -> tuple:
    """(bytes, file size, mtime_ns), from one open file. `length` None reads to the end. A range
    past the end is a RangeError; one bigger than max_bytes is TooLarge."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IsADirectoryError(path)       # directories and devices are not files to serve
        size = st.st_size
        if offset < 0 or offset > size:
            raise RangeError(f"offset {offset} is outside the file (size {size})")
        want = size - offset if length is None else min(length, size - offset)
        if want > max_bytes:
            raise TooLarge(f"{want} bytes requested; replies are capped at {max_bytes} "
                             f"(ask for ?offset=<n>;length=<n> ranges)")
        chunks, got = [], 0
        while got < want:
            b = os.pread(fd, want - got, offset + got)
            if not b:
                break
            chunks.append(b)
            got += len(b)
        return b"".join(chunks), size, st.st_mtime_ns
    finally:
        os.close(fd)


def write_atomic(path: str, data: bytes) -> None:
    """Dot-prefixed temp in the same directory, fsync, rename: the file appears complete or not at
    all, and the store's own watcher reports it on the rename. Refuses to replace an existing file."""
    d, name = os.path.split(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)          # fails with FileExistsError instead of silently replacing
    except FileExistsError:
        os.unlink(tmp)
        raise
    except OSError:
        os.replace(tmp, path)       # filesystems without hard links: plain rename
        return
    os.unlink(tmp)


class DigestCache:
    """sha256 per (path, size, mtime), computed on request only."""

    def __init__(self, limit: int = 1024):
        self._cache: dict = {}
        self._limit = limit
        self._lock = threading.Lock()

    def sha256(self, path: str, size: int, mtime_ns: int) -> str:
        key = (path, size, mtime_ns)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        digest = h.hexdigest()
        with self._lock:
            if len(self._cache) >= self._limit:
                self._cache.clear()
            self._cache[key] = digest
        return digest
