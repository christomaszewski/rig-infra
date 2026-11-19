#!/usr/bin/env python3
"""Refresh the bag logger's temporary observer compatibility runtime.

observer/tools is the only implementation source. Rig vendors each service's
launch surface independently, so the legacy bag-logger service carries ordinary,
byte-identical copies instead of reaching into a sibling service at runtime.

Run with --write after changing the canonical workers. CI uses --check.
Unexpected destination entries are reported, never silently deleted.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

FILES = (
    "graph_snapshot.py",
    "endpoint_snapshot.py",
    "run_observer.py",
    "system_snapshot.py",
    "host_probe.py",
)


def differences(source: Path, destination: Path) -> list[str]:
    problems = []
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        return ["destination must be a normal directory"]
    if destination.exists():
        for path in sorted(destination.iterdir()):
            if path.name == "__pycache__" and path.is_dir() and not path.is_symlink():
                continue  # normal local Python imports; never part of the runtime surface
            if path.name not in FILES:
                problems.append(f"extra: {path.name}")
    for name in FILES:
        canonical, mirrored = source / name, destination / name
        if canonical.is_symlink() or not canonical.is_file():
            problems.append(f"canonical source is not a normal file: {name}")
        elif mirrored.is_symlink():
            problems.append(f"symlink instead of normal copy: {name}")
        elif not mirrored.is_file():
            problems.append(f"missing: {name}")
        elif canonical.read_bytes() != mirrored.read_bytes():
            problems.append(f"stale: {name}")
    return problems


def synchronize(source: Path, destination: Path) -> None:
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("destination must be a normal directory")
    # Validate the complete fixed source set before changing any generated copy.
    for name in FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"canonical source is not a normal file: {name}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        canonical, mirrored = source / name, destination / name
        temporary = destination / f".{name}.tmp.{os.getpid()}"
        try:
            shutil.copy2(canonical, temporary)
            os.replace(temporary, mirrored)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate copies without modifying them")
    mode.add_argument("--write", action="store_true", help="refresh compatibility copies (also the default)")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    source = root / "observer/tools"
    destination = root / "ros2-bag-logger/tools/observer"
    try:
        if not args.check:
            synchronize(source, destination)
        problems = differences(source, destination)
    except (OSError, ValueError) as exc:
        problems = [str(exc)]
    if problems:
        print("observer compatibility runtime is not synchronized:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("Run python3 tools/sync_observer_compat.py --write after editing observer/tools.", file=sys.stderr)
        return 1
    print(f"observer compatibility runtime: {len(FILES)} files {'verified' if args.check else 'synchronized'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
