#!/usr/bin/env python3
"""Builder-stage half of the fleet-ros-msgs overlay (runs INSIDE msgs/Dockerfile, not on the host):
validate the union manifest, clone each `source:` repo at its pin, and colcon-build the declared
interface packages into one merged install prefix (/opt/fleet-msgs).

Validation lives here — one place, container-side — so a hand-run `docker build` and a future
rig-driven build refuse the same manifests the same way:
  - an EMPTY manifest is refused: an overlay identical to the base is a pointless image, and under
    the future rig integration an empty union means "don't build the overlay at all";
  - `source:` entries need `repo` + `ref` + `packages` — the ref pin is MANDATORY because the overlay
    must publish wire-identical definitions to what the declaring service itself builds against;
  - the same repo declared at two different refs is a REFUSAL, not a manifest-order guess (two
    services disagreeing on a pin is the version skew this image exists to prevent — align the
    riggings); the same repo at the SAME ref from several services merges (packages union).

Local checkouts (the FLEET_MSGS_LOCAL dev loop, see build-msgs.sh): the optional third argument is
the `local-sources` mount — host-side snapshots of working trees plus sources.tsv describing them.
Each snapshot REPLACES the clone of the one `source:` repo its key names (a declared package, the
repo name, or the repo URL); keys that match nothing, match several repos, name an `apt:` package,
or collide on one repo are refused here, with the manifest in hand. An empty mount (the default)
changes nothing.

Provenance: alongside the install prefix this writes <prefix>/provenance.yaml — one entry per
source repo recording the declared ref AND the commit SHA it resolved to at build time
(`git rev-parse HEAD` on the very clone that was built, so the record cannot drift from the build).
That is the overlay's half of the fleet-msgs provenance convention (schema:
provenance.example.yaml); the symbolic ref alone is not content identity — a moved tag or a
re-built branch gives a different tree under the same name, and the SHA is what lets `rig image
audit` catch that against the declaring service's own provenance. Always written, even with no
`source:` entries (`source: []`) — an overlay image without the file is a pre-provenance build,
not a source-less one. A local checkout records what the host saw in its working tree instead
(ref, rev, and `local:` its path) — never the declared pin it replaced.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class Local:
    """One sources.tsv row, written by build-msgs.sh on the host."""
    dir: str   # snapshot directory, relative to the local-sources root
    key: str   # the FLEET_MSGS_LOCAL key, resolved against the manifest below
    path: str  # the checkout on the host — recorded as provenance `local:`
    ref: str   # exact tag at HEAD, else the SHA; `-dirty` when the snapshot differs from HEAD
    rev: str   # HEAD, or `unknown` (dirty, or no git identity)


def die(msg: str) -> None:
    print(f"build_msgs: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    manifest_path, prefix = sys.argv[1], sys.argv[2]
    build(manifest_path, prefix, sys.argv[3] if len(sys.argv) > 3 else None)


def build(manifest_path: str, prefix: str, local_root: str | None = None,
          ws: str = "/tmp/msgs_ws") -> None:
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}
    if not isinstance(manifest, dict):
        die(f"{manifest_path}: manifest must be a mapping with `apt:` and/or `source:` lists")
    apt = manifest.get("apt") or []
    source = manifest.get("source") or []
    if not isinstance(apt, list) or not isinstance(source, list):
        die(f"{manifest_path}: `apt:` and `source:` must be lists")
    if not apt and not source:
        die("empty manifest — no interface packages declared; refusing to build an overlay "
            "identical to the base image (with rig, an empty union should skip this build entirely)")

    pins: dict[str, str] = {}       # repo -> ref (conflict refusal)
    packages: dict[str, list] = {}  # repo -> union of declared packages
    for entry in source:
        if not isinstance(entry, dict):
            die(f"source entry must be a mapping: {entry!r}")
        repo, ref, pkgs = entry.get("repo"), entry.get("ref"), entry.get("packages")
        if not repo or not ref or not pkgs:
            die(f"source entry needs `repo`, `ref` (the pin is mandatory) and `packages`: {entry!r}")
        if repo in pins and pins[repo] != str(ref):
            die(f"{repo} declared at two different refs ('{pins[repo]}' vs '{ref}') — two services "
                f"disagree on the pin; align their `msgs:` declarations")
        pins[repo] = str(ref)
        packages.setdefault(repo, [])
        packages[repo] += [p for p in pkgs if p not in packages[repo]]
    # Resolved before anything is fetched or built: a typo'd key must not cost a clone first.
    local = resolve_local(read_local(local_root), packages, apt)

    os.makedirs(prefix, exist_ok=True)  # the final stage COPYs this even when source: is empty
    if not source:
        write_provenance(prefix, [])
        return

    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    provenance = []
    for i, (repo, ref) in enumerate(pins.items()):
        dest = os.path.join(src, f"repo{i}")
        if repo in local:
            loc = local[repo]
            print(f"build_msgs: {repo}: building the LOCAL checkout {loc.path} ({loc.ref}) "
                  f"instead of cloning the declared pin '{ref}'", file=sys.stderr)
            shutil.copytree(os.path.join(local_root, loc.dir), dest, symlinks=True)
            provenance.append({"repo": repo, "ref": loc.ref, "rev": loc.rev,
                               "packages": packages[repo], "local": loc.path})
            continue
        if local and _is_ssh(repo) and not os.path.exists(os.environ.get("SSH_AUTH_SOCK", "")):
            # build-msgs.sh skips agent forwarding when FLEET_MSGS_LOCAL is set and no agent is
            # loaded, betting the local checkouts cover every ssh-form repo — this one they don't.
            die(f"{repo} is an ssh-form repo FLEET_MSGS_LOCAL does not cover, and no ssh-agent was "
                f"forwarded — load one on the build machine (ssh-add) or add the repo to "
                f"FLEET_MSGS_LOCAL")
        # full clone (no --depth): `ref` may be a tag, branch, or bare SHA — all must resolve
        subprocess.run(["git", "clone", repo, dest], check=True)
        # Resolve BEFORE checkout: a NON-DEFAULT branch exists only as origin/<ref> in a fresh
        # clone, and `checkout --detach <ref>` disables the DWIM (modern git dies 128 with
        # "'--detach' cannot be used with '-b'"). Tags/SHAs/the default branch hit the first
        # candidate; branches the second; neither = a pointed error naming what IS there.
        sha = None
        for cand in (ref, f"origin/{ref}"):
            probe = subprocess.run(["git", "-C", dest, "rev-parse", "--verify", "--quiet",
                                    cand + "^{commit}"], capture_output=True, text=True)
            if probe.returncode == 0:
                sha = probe.stdout.strip()
                break
        if sha is None:
            tags = subprocess.run(["git", "-C", dest, "tag", "-l"], capture_output=True,
                                  text=True).stdout.split()
            branches = [b.strip() for b in subprocess.run(
                ["git", "-C", dest, "branch", "-r"], capture_output=True, text=True
            ).stdout.splitlines() if "->" not in b]
            raise SystemExit(
                f"build_msgs: ref '{ref}' not found in {repo} — not a tag, branch, or commit "
                f"there. The rigging's msgs.source ref must be the pin the service builds "
                f"against.\n  branches: {', '.join(branches) or 'none'}"
                f"\n  tags: {', '.join(tags[:20]) or 'none'}{' …' if len(tags) > 20 else ''}")
        subprocess.run(["git", "-C", dest, "checkout", "--detach", sha], check=True)
        # rev-parse the clone that will be BUILT — the truth, not a re-echo of the declaration
        rev = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        provenance.append({"repo": repo, "ref": ref, "rev": rev, "packages": packages[repo]})

    wanted = sorted({p for pkgs in packages.values() for p in pkgs})
    # --packages-up-to: an interface package may depend on sibling interface packages in the same
    # repo; pull those in too. Anything heavier than message generation does not belong here —
    # the overlay is interface packages ONLY (doctrine, enforced by review not tooling).
    subprocess.run(
        ["colcon", "build", "--merge-install", "--install-base", prefix,
         "--packages-up-to", *wanted, "--cmake-args", "-DBUILD_TESTING=OFF"],
        cwd=ws, check=True)
    missing = [p for p in wanted if not os.path.isdir(os.path.join(prefix, "share", p))]
    if missing:
        die(f"declared packages not present after build: {missing} — wrong `packages:` names?")
    write_provenance(prefix, provenance)  # after the build: the file describes a COMPLETE prefix


def read_local(root: str | None) -> list[Local]:
    """The sources.tsv rows of the local-sources mount; [] when there is none (the empty default
    stage, or a build run without the mount at all)."""
    index = os.path.join(root, "sources.tsv") if root else None
    if not index or not os.path.isfile(index):
        return []
    rows = []
    with open(index) as f:
        for n, line in enumerate(f, 1):
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 5 or not all(fields):
                die(f"{index}:{n}: malformed local-sources row {line!r} (want dir, key, path, ref, "
                    f"rev — written by build-msgs.sh)")
            rows.append(Local(*fields))
    return rows


def resolve_local(rows: list[Local], packages: dict[str, list], apt: list) -> dict[str, Local]:
    """repo -> the local checkout that replaces its clone. A key names ONE `source:` repo: by one of
    its declared packages, the repo name (last path segment), or the repo URL (normalized like the
    provenance join — https/ssh spellings match)."""
    def names(repo):
        norm = norm_repo(repo)
        return {norm, norm.rsplit("/", 1)[-1], *packages[repo]}
    resolved: dict[str, Local] = {}
    for row in rows:
        hits = [repo for repo in packages
                if row.key in names(repo) or norm_repo(row.key) == norm_repo(repo)]
        if not hits:
            if row.key in apt:
                die(f"FLEET_MSGS_LOCAL: '{row.key}' is an `apt:` interface package — local "
                    f"checkouts replace `source:` repos only; declare it under msgs.source (repo, "
                    f"ref, packages) to build it from a checkout")
            valid = sorted({n for repo in packages for n in names(repo)})
            die(f"FLEET_MSGS_LOCAL: '{row.key}' matches no `source:` entry in the manifest — name "
                f"one of its packages, the repo name, or the repo URL: {', '.join(valid) or 'none'}")
        if len(hits) > 1:
            die(f"FLEET_MSGS_LOCAL: '{row.key}' is ambiguous — it matches {', '.join(hits)}; use "
                f"the repo URL instead")
        repo = hits[0]
        if repo in resolved:
            die(f"FLEET_MSGS_LOCAL: '{resolved[repo].key}' and '{row.key}' both name {repo} — "
                f"one checkout per repo")
        resolved[repo] = row
    return resolved


def norm_repo(url: str) -> str:
    """The provenance join normalization (provenance.example.yaml; rig's audit applies the same):
    drop the scheme and any userinfo, rewrite scp-form `git@host:path` -> `host/path`, drop ONE
    trailing `.git`, lowercase the host."""
    u = url.strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(.*)$", u)
    if m:
        u = re.sub(r"^[^/@]+@", "", m.group(1))
    else:
        m = re.match(r"^[^/@]+@([^:/]+):(.*)$", u)
        if m:
            u = f"{m.group(1)}/{m.group(2)}"
    if u.endswith(".git"):
        u = u[:-4]
    host, sep, rest = u.partition("/")
    return host.lower() + sep + rest


def _is_ssh(repo: str) -> bool:
    return repo.startswith(("git@", "ssh://"))


def write_provenance(prefix: str, source: list) -> None:
    with open(os.path.join(prefix, "provenance.yaml"), "w") as f:
        yaml.safe_dump({"version": 1, "source": source}, f, sort_keys=False)


if __name__ == "__main__":
    main()
