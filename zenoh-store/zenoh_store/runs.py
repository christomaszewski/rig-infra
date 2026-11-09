"""The rig run registry as the store sees it (read-only; rig owns it).

    <data_dir>/runs/<run_id>/manifest.yaml   `ended:` present <=> sealed
    <data_dir>/current -> runs/<run_id>      the (at most one) OPEN run, a RELATIVE link

The open run is pinned once, at startup: rig refuses to rotate while any row is up, so the store
restarts at every run boundary like every other recorder. Without a registry (no `runs/`, standalone
use) the data dir's own `store/` is served as the run `flat`, which is then also the open run.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

FLAT_RUN = "flat"
_ENDED = re.compile(r"^ended:[ \t]*(.*?)[ \t]*$", re.M)


@dataclass(frozen=True)
class RunInfo:
    run: str
    state: str           # open | sealed | interrupted
    has_store: bool


def manifest_sealed(text: str) -> bool:
    m = _ENDED.search(text)
    return bool(m) and m.group(1) not in ("", "~", "null", "''", '""')


class Registry:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.runs_dir = os.path.join(data_dir, "runs")

    @property
    def flat(self) -> bool:
        return not os.path.isdir(self.runs_dir)

    def current(self) -> Optional[str]:
        """The open run id from the `current` link, or None. A link pointing anywhere but a direct
        child of runs/ is treated as no open run (rig itself refuses such a registry)."""
        if self.flat:
            return FLAT_RUN if os.path.isdir(self.data_dir) else None
        link = os.path.join(self.data_dir, "current")
        try:
            target = os.readlink(link)
        except OSError:
            return None
        resolved = os.path.normpath(os.path.join(self.data_dir, target))
        if os.path.dirname(resolved) != os.path.normpath(self.runs_dir) or not os.path.isdir(resolved):
            return None
        return os.path.basename(resolved)

    def store_dir(self, run: str) -> str:
        if run == FLAT_RUN and self.flat:
            return os.path.join(self.data_dir, "store")
        return os.path.join(self.runs_dir, run, "store")

    def exists(self, run: str) -> bool:
        if run == FLAT_RUN:
            return self.flat and os.path.isdir(self.data_dir)
        return not self.flat and os.path.isdir(os.path.join(self.runs_dir, run))

    def state(self, run: str, open_run: Optional[str]) -> str:
        if run == open_run:
            return "open"
        try:
            with open(os.path.join(self.runs_dir, run, "manifest.yaml")) as f:
                return "sealed" if manifest_sealed(f.read()) else "interrupted"
        except OSError:
            return "interrupted"

    def runs(self, open_run: Optional[str]) -> list:
        if self.flat:
            return [RunInfo(FLAT_RUN, "open", os.path.isdir(self.store_dir(FLAT_RUN)))] \
                if os.path.isdir(self.data_dir) else []
        out = []
        try:
            names = sorted(os.listdir(self.runs_dir))
        except OSError:
            return []
        for name in names:
            if name.startswith(".") or not os.path.isdir(os.path.join(self.runs_dir, name)):
                continue
            out.append(RunInfo(name, self.state(name, open_run), os.path.isdir(self.store_dir(name))))
        return out
