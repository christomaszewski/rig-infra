"""Watching the open run's store/ for completed files: inotify through ctypes (no extra packages).

A file counts as complete when it is renamed into place (IN_MOVED_TO: the temp-then-rename producer
rule) or closed after writing (IN_CLOSE_WRITE: in-place writers). With `settle_ms` > 0, a closed or
modified file is only reported once it has been quiet that long (writers that close and reopen).
New directories are watched as they appear and scanned straight away, so files written before the
watch landed are not missed. A queue overflow asks for a full rescan.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import select
import struct
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
_EVENT = struct.Struct("iIII")

BASE_MASK = (IN_CLOSE_WRITE | IN_MOVED_TO | IN_MOVED_FROM | IN_CREATE | IN_DELETE
             | IN_DELETE_SELF | IN_MOVE_SELF)


class InotifyUnavailable(OSError):
    pass


class Inotify:
    def __init__(self):
        try:
            self._libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
            init = self._libc.inotify_init1
        except (OSError, AttributeError) as e:
            raise InotifyUnavailable(f"inotify is not available: {e}") from e
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        fd = init(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise InotifyUnavailable(err, f"inotify_init1: {os.strerror(err)}")
        self.fd = fd

    def add(self, path: str, mask: int) -> int:
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(path), mask)
        if wd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_add_watch {path}: {os.strerror(err)}")
        return wd

    def remove(self, wd: int) -> None:
        self._libc.inotify_rm_watch(self.fd, wd)

    def read(self, timeout_s: float) -> list:
        """[(wd, mask, cookie, name)] or [] after timeout_s."""
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout_s))
        if not ready:
            return []
        try:
            buf = os.read(self.fd, 256 * 1024)
        except BlockingIOError:
            return []
        out, i = [], 0
        while i + _EVENT.size <= len(buf):
            wd, mask, cookie, length = _EVENT.unpack_from(buf, i)
            i += _EVENT.size
            name = buf[i:i + length].rstrip(b"\0")
            i += length
            out.append((wd, mask, cookie, os.fsdecode(name)))
        return out

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class TreeWatcher:
    """Watches `root` recursively and reports store-relative paths:

    on_file(relpath)   a file completed (or, with settle, went quiet)
    on_gone(relpath)   a file or directory went away (producers should not do this)
    on_rescan()        the kernel queue overflowed, or a watch could not be added
    """

    def __init__(self, root: str, on_file: Callable[[str], None], on_gone: Callable[[str], None],
                 on_rescan: Callable[[], None], settle_ms: float = 0,
                 clock: Callable[[], float] = time.monotonic):
        self.root = root
        self.on_file, self.on_gone, self.on_rescan = on_file, on_gone, on_rescan
        self.settle_s = settle_ms / 1000.0
        self._clock = clock
        self._ino: Optional[Inotify] = None
        self._dirs: dict = {}          # wd -> store-relative dir ("" for root)
        self._wds: dict = {}           # store-relative dir -> wd
        self._pending: dict = {}       # relpath -> due (settle mode)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.overflows = 0
        self.watch_errors = 0
        self.mask = BASE_MASK | (IN_MODIFY if self.settle_s > 0 else 0)

    @property
    def watching(self) -> int:
        return len(self._wds)

    def start(self) -> None:
        """Raises InotifyUnavailable when the kernel or libc offers no inotify."""
        os.makedirs(self.root, exist_ok=True)
        self._ino = Inotify()
        self._add_tree("")
        self._thread = threading.Thread(target=self._run, name="store-inotify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        if self._ino is not None:
            self._ino.close()

    # ---- watches -----------------------------------------------------------------
    def _abs(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def _add_tree(self, rel: str) -> list:
        """Watch `rel` and every directory below it; returns the files already inside (the ones
        written before the watch existed)."""
        files, stack = [], [rel]
        while stack:
            d = stack.pop()
            try:
                wd = self._ino.add(self._abs(d), self.mask)
            except OSError as e:
                self.watch_errors += 1
                if e.errno == errno.ENOSPC:
                    log.warning("inotify: out of watches (fs.inotify.max_user_watches) at %s; "
                                "relying on rescans", d or ".")
                    self.on_rescan()
                continue
            self._dirs[wd] = d
            self._wds[d] = wd
            try:
                with os.scandir(self._abs(d)) as it:
                    for e in it:
                        if e.name.startswith("."):
                            continue
                        child = f"{d}/{e.name}" if d else e.name
                        if e.is_dir(follow_symlinks=False):
                            stack.append(child)
                        elif e.is_file(follow_symlinks=False):
                            files.append(child)
            except OSError:
                pass
        return files

    def _drop_tree(self, rel: str) -> None:
        for d in [d for d in self._wds if d == rel or d.startswith(rel + "/")]:
            wd = self._wds.pop(d)
            self._dirs.pop(wd, None)

    # ---- event loop --------------------------------------------------------------
    def _due(self) -> float:
        if not self._pending:
            return 0.5
        return max(0.0, min(self._pending.values()) - self._clock())

    def _flush(self) -> None:
        now = self._clock()
        for rel in [r for r, due in self._pending.items() if due <= now]:
            del self._pending[rel]
            self.on_file(rel)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                events = self._ino.read(min(0.5, self._due()))
            except OSError as e:
                log.warning("inotify: read failed (%s); relying on rescans", e)
                self.on_rescan()
                return
            for wd, mask, _cookie, name in events:
                try:
                    self.handle(wd, mask, name)
                except Exception as e:   # noqa: BLE001 -- one bad event never stops the watcher
                    log.warning("inotify: event %s/%s failed: %s", wd, name, e)
            self._flush()

    def handle(self, wd: int, mask: int, name: str) -> None:
        if mask & IN_Q_OVERFLOW:
            self.overflows += 1
            log.warning("inotify: event queue overflowed; rescanning")
            self.on_rescan()
            return
        d = self._dirs.get(wd)
        if d is None:
            return
        if mask & IN_IGNORED or mask & (IN_DELETE_SELF | IN_MOVE_SELF):
            if mask & (IN_DELETE_SELF | IN_MOVE_SELF) and d == "":
                log.warning("inotify: store/ itself went away")
            self._dirs.pop(wd, None)
            self._wds.pop(d, None)
            return
        if not name or name.startswith("."):
            return
        rel = f"{d}/{name}" if d else name
        if mask & IN_ISDIR:
            if mask & (IN_CREATE | IN_MOVED_TO):
                for f in self._add_tree(rel):
                    self._report(f, settled=True)
            elif mask & (IN_DELETE | IN_MOVED_FROM):
                self._drop_tree(rel)
                self.on_gone(rel + "/")
            return
        if mask & IN_MOVED_TO:
            self._pending.pop(rel, None)
            self._report(rel, settled=True)
        elif mask & IN_CLOSE_WRITE:
            self._report(rel, settled=False)
        elif mask & IN_MODIFY and rel in self._pending:
            self._pending[rel] = self._clock() + self.settle_s
        elif mask & (IN_DELETE | IN_MOVED_FROM):
            self._pending.pop(rel, None)
            self.on_gone(rel)

    def _report(self, rel: str, settled: bool) -> None:
        if settled or self.settle_s <= 0:
            self.on_file(rel)
        else:
            self._pending[rel] = self._clock() + self.settle_s
