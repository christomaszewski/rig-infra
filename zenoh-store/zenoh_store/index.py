"""The in-memory index of one run's store/: which files exist, how big, and in what order they appeared.

Every file the index learns about (or sees change) gets the next sequence number. A fetcher's
position is a cursor `<epoch>.<seq>`; listing "after" a cursor returns exactly the files that are
new or changed since, in the order the store saw them. Sequence numbers come from the store, not
from file mtimes, so a vehicle clock that jumps (GPS or NTP sync after boot) cannot hide a file.
The epoch names this store process: after a restart old cursors are stale, and the listing starts
over with `reset: true` (the fetcher skips what it already holds).
"""
from __future__ import annotations

import bisect
import os
import stat
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from . import keys
from .keys import relpath_of


@dataclass(frozen=True)
class Entry:
    path: str          # store-relative: <producer>/.../<name>
    size: int
    mtime_ns: int
    seq: int


def walk(root: str, bound: Optional[str] = None, on_skip: Optional[Callable[[str, str], None]] = None):
    """(relpath, stat) of every file under root that fits the contract.

    Symlinks are followed only to targets inside `bound` (the run directory; default root): a
    linked file is listed with its target's size and mtime, a linked directory is walked like a
    real one. That is how a producer shares data the run already holds (laser scans under
    recordings/, say) without a second copy. Dot-prefixed entries (temp files, hidden dirs) are
    skipped silently; anything else unusable is reported as on_skip(relpath, reason)."""
    real_root = os.path.realpath(root)
    real_bound = os.path.realpath(bound or root)
    skip = on_skip or (lambda rel, why: None)
    stack, linked_dirs = [root], set()
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name.startswith("."):
                    continue
                shown = os.path.relpath(e.path, root).replace(os.sep, "/")
                try:
                    if e.is_symlink():
                        st, real, why = keys.follow(e.path, real_bound, real_root)
                        if why:
                            skip(shown, why)
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            if real in linked_dirs:
                                skip(shown, "directory linked twice")
                            else:
                                linked_dirs.add(real)
                                stack.append(e.path)
                            continue
                    elif e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                    else:
                        continue
                except OSError:
                    continue
                rel = relpath_of(root, e.path)
                if rel is None:
                    skip(shown, "not usable as a key")
                    continue
                yield rel, st


class RunIndex:
    def __init__(self, run: str, root: str, epoch: str, bound: Optional[str] = None):
        self.run = run
        self.root = root
        self.bound = bound or root          # links are followed while they stay inside this
        self.epoch = epoch
        self._lock = threading.Lock()
        self._entries: dict = {}
        self._order: list = []        # (seq, path), seq ascending; stale pairs skipped on read
        self._seq = 0
        self.skipped: dict = {}       # relpath -> why it is not served (reported in health)
        self.scanned_mono: Optional[float] = None
        self.bytes = 0

    # ---- writes ----------------------------------------------------------------
    def _assign(self, path: str, size: int, mtime_ns: int) -> Optional[Entry]:
        old = self._entries.get(path)
        if old is not None and old.size == size and old.mtime_ns == mtime_ns:
            return None
        self._seq += 1
        entry = Entry(path, size, mtime_ns, self._seq)
        self._entries[path] = entry
        self._order.append((entry.seq, path))
        self.bytes += size - (old.size if old else 0)
        if len(self._order) > 2 * len(self._entries) + 1024:
            self._compact()
        return entry

    def _compact(self) -> None:
        self._order = sorted((e.seq, p) for p, e in self._entries.items())

    def note(self, path: str) -> Optional[Entry]:
        """Stat one store-relative path (through links that stay inside the run). Returns the entry
        when it is new or changed, None when it is unchanged; a vanished file is forgotten."""
        full = os.path.join(self.root, *path.split("/"))
        try:
            real = os.path.realpath(full, strict=True)
            st = os.stat(real)
        except (OSError, RuntimeError):
            self.forget(path)
            return None
        if not keys.within(real, os.path.realpath(self.bound)):
            self.forget(path)
            with self._lock:
                self.skipped[path] = "link outside the run"
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        with self._lock:
            self.skipped.pop(path, None)
            return self._assign(path, st.st_size, st.st_mtime_ns)

    def forget(self, path: str) -> None:
        with self._lock:
            old = self._entries.pop(path, None)
            if old is not None:
                self.bytes -= old.size

    def forget_prefix(self, prefix: str) -> None:
        with self._lock:
            for p in [p for p in self._entries if p.startswith(prefix)]:
                self.bytes -= self._entries.pop(p).size

    def scan(self, monotonic: float) -> list:
        """Reconcile with the disk. New and changed files get sequence numbers in (mtime, path)
        order, so a first scan lists the run in the order it was written. Returns them."""
        found, skipped = {}, {}
        if os.path.isdir(self.root):
            for rel, st in walk(self.root, self.bound, skipped.__setitem__):
                found[rel] = (st.st_size, st.st_mtime_ns)
        changed = []
        with self._lock:
            for path in [p for p in self._entries if p not in found]:
                self.bytes -= self._entries.pop(path).size
            for path, (size, mtime) in sorted(found.items(), key=lambda kv: (kv[1][1], kv[0])):
                entry = self._assign(path, size, mtime)
                if entry is not None:
                    changed.append(entry)
            self.skipped = skipped
            self.scanned_mono = monotonic
        return changed

    # ---- reads -----------------------------------------------------------------
    def get(self, path: str) -> Optional[Entry]:
        with self._lock:
            return self._entries.get(path)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def last_seq(self) -> int:
        return self._seq

    def cursor(self, seq: Optional[int] = None) -> str:
        return f"{self.epoch}.{self._seq if seq is None else seq}"

    def parse_cursor(self, cursor: Optional[str]) -> tuple:
        """(seq to list after, reset). A missing cursor lists from the start; a cursor from another
        epoch (the store restarted) or a malformed one lists from the start with reset=True."""
        if not cursor:
            return 0, False
        epoch, _, seq = cursor.rpartition(".")
        if epoch != self.epoch or not seq.isdigit() or int(seq) > self._seq:
            return 0, True
        return int(seq), False

    def list(self, after_seq: int = 0, prefix: str = "", accept: Optional[Callable[[Entry], bool]] = None,
             limit: int = 500) -> tuple:
        """(entries after `after_seq` in sequence order, cursor to continue from, more?).
        The cursor also moves past entries the filters skip, so the next page never re-reads them."""
        out, last = [], after_seq
        with self._lock:
            i = bisect.bisect_right(self._order, after_seq, key=lambda pair: pair[0])
            for seq, path in self._order[i:]:
                entry = self._entries.get(path)
                if entry is None or entry.seq != seq:
                    continue                      # superseded or forgotten
                if (prefix and not path.startswith(prefix)) or (accept is not None and not accept(entry)):
                    last = seq
                    continue
                if len(out) >= limit:
                    return out, self.cursor(last), True
                out.append(entry)
                last = seq
            return out, self.cursor(last), False

    def producers(self) -> dict:
        """producer -> (files, newest mtime_ns)."""
        out: dict = {}
        with self._lock:
            for e in self._entries.values():
                prod = e.path.split("/", 1)[0]
                n, newest = out.get(prod, (0, 0))
                out[prod] = (n + 1, max(newest, e.mtime_ns))
        return out
