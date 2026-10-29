#!/bin/sh
# fleet-ros-msgs overlay build — the rig build contract shape: build-msgs.sh <registry> [tag].
#
# Builds <registry>/fleet-ros-msgs:<tag> = the deployment's base image + the UNION of the interface
# packages the deployment's services declare in their riggings' `msgs:` blocks (see
# msgs-manifest.example.yaml for both the per-service declaration and the union manifest — same
# schema). The ros2 bag logger's compose prefers this image over the bare base
# (BAG_LOGGER_IMAGE -> RIG_MSGS_IMAGE -> RIG_BASE_IMAGE -> composed fleet-ros ref), so building and
# exporting it is all it takes for the logger to start recording the fleet's custom types.
#
# Driven by `rig build` (rig >= v0.2.28): the zenoh-router + ros2-bag-logger riggings declare this
# script as `build.msgs_overlay` — rig unions the fleet's `msgs:` blocks, renders the union to a
# temp file (RIG_MSGS_MANIFEST), and runs this right after the base stage; `rig up` then exports
# RIG_MSGS_IMAGE and the logger records the fleet's custom types. Standalone use (no rig, or an
# older rig) stays supported — author the union by hand and point the logger at the result:
#   FLEET_MSGS_MANIFEST=./msgs.yaml ./msgs/build-msgs.sh <registry> <tag>
#   # then export BAG_LOGGER_IMAGE=<registry>/fleet-ros-msgs:<tag> (or RIG_MSGS_IMAGE) for `rig up`
#
# Env (the argv contract stays `<cmd> <registry> [tag]`; the rest arrives through the environment):
#   RIG_MSGS_MANIFEST    path to the union manifest — the name rig will own once it renders the
#                        union itself (rig-owned, set-or-popped, like RIG_ROS_RMW).
#   FLEET_MSGS_MANIFEST  the same for builds OUTSIDE rig (standalone), where nothing sets
#                        RIG_MSGS_MANIFEST; rig's var wins when both are set (the FLEET_ROS_RMW
#                        pattern from base/build.sh).
#   RIG_BASE_IMAGE       the deployment's base image — this overlay builds FROM it. Fallback:
#                        <registry>/fleet-ros:<tag>, the same composed ref the pull side uses.
#   ROS_DISTRO           vehicle.yaml `ros.distro`; maps `apt:` names to ros-<distro>-* packages.
#   RIG_BUILD_NO_CACHE   `rig build --no-cache`: full rebuild. Deliberately NO --pull here (unlike
#                        base/build.sh): the parent is the deployment's own base image, whose
#                        version authority is the stage-0 build on THIS box, not the registry.
#   FLEET_MSGS_LOCAL     opt-in dev loop: build `source:` repos from LOCAL checkouts instead of
#                        cloning their pins (e.g. unpushed message changes). Whitespace-separated
#                        KEY=PATH entries — KEY a declared package, the repo name, or the repo URL;
#                        PATH absolute or ~/ (no whitespace). See "Local checkouts" below. Not
#                        rig-owned: `rig build` passes the caller's env through, so
#                          FLEET_MSGS_LOCAL="sbg_msgs=$HOME/ws/sbg_driver" rig build
#                        works on any rig. Unset = the pinned clones, unchanged.
set -eu
registry="${1:?usage: build-msgs.sh <registry> [tag]}"
tag="${2:-latest}"
msgs_dir="$(cd "$(dirname "$0")" && pwd)"
manifest="${RIG_MSGS_MANIFEST:-${FLEET_MSGS_MANIFEST:-}}"
if [ -z "$manifest" ] || [ ! -f "$manifest" ]; then
    echo "build-msgs.sh: set RIG_MSGS_MANIFEST (or FLEET_MSGS_MANIFEST) to the union manifest path" >&2
    echo "  (schema: msgs/msgs-manifest.example.yaml; empty manifests are refused by the build)" >&2
    exit 1
fi
distro="${ROS_DISTRO:-lyrical}"
base="${RIG_BASE_IMAGE:-${registry}/fleet-ros:${tag}}"
ref="${registry}/fleet-ros-msgs:${tag}"

# Stage a throwaway context: the manifest lives wherever the caller keeps it (rig will render a temp
# file; hand use points at a deployment-owned yaml) — never copied into msgs/ itself, which must stay
# a clean, self-contained build unit.
work="$(mktemp -d "${TMPDIR:-/tmp}/rig-msgs.XXXXXX")"
trap 'rm -rf "$work"' EXIT
ctx="$work/context"
mkdir "$ctx"
cp "$msgs_dir/Dockerfile" "$msgs_dir/build_msgs.py" "$ctx/"
cp "$manifest" "$ctx/msgs-manifest.yaml"

# Local checkouts (FLEET_MSGS_LOCAL). Each checkout is SNAPSHOTTED the way git sees it — tracked
# files with their uncommitted edits plus untracked, non-ignored files; never .git, never ignored
# build/ install/ log/ trees (sbg_driver: 137 MB on disk, 1.2 MB this way) — from git's own file
# lists, READ-ONLY: nothing is written to the checkout (no index refresh, no objects), so a
# read-only checkout works and a stray un-ignored file costs a copy, not a blob in .git. Submodule
# and nested-repo CONTENTS are not snapshotted (an empty directory stands in) — the pinned clone
# in build_msgs.py does not fetch submodules either. The snapshots reach BuildKit as one named
# build context, `local-sources`, which replaces msgs/Dockerfile's empty stage of that name; a
# named context transfers from this client exactly like the main one, so rig-build.sh's
# --platform/--push/--load paths are unchanged. KEYs are resolved against the manifest
# container-side (build_msgs.py, which has it parsed): unknown, ambiguous, or apt-only keys are
# refused there. sources.tsv carries the identity the host saw — sbg_driver tools/build_image.sh's
# convention: ref = the exact tag at HEAD, else the SHA, `-dirty` appended when the working tree
# differs from HEAD; rev = HEAD, `unknown` when dirty (HEAD does not describe the built content).
# A repo with no commits yet, and a directory with no .git of its own (an export, copied whole),
# are recorded unknown/unknown. The image is LABELLED io.rig.build.local=<keys>, so `rig image
# audit` warns about it and `rig bake` refuses it without --allow-local. PATH must be absolute:
# rig runs this script from the service directory, so a relative path would silently mean
# something else.
lsrc=""
lkeys=""
if [ -n "${FLEET_MSGS_LOCAL:-}" ]; then
    command -v git >/dev/null 2>&1 || {
        echo "build-msgs.sh: FLEET_MSGS_LOCAL needs git on this machine to snapshot the checkouts" >&2
        exit 1
    }
    lsrc="$work/local-sources"
    mkdir "$lsrc"
    n=0
    set -f  # split the entries on whitespace, but never glob them
    for entry in $FLEET_MSGS_LOCAL; do
        key="${entry%%=*}"
        path="${entry#*=}"
        if [ "$key" = "$entry" ] || [ -z "$key" ] || [ -z "$path" ]; then
            echo "build-msgs.sh: FLEET_MSGS_LOCAL entry '$entry' is not KEY=PATH" >&2
            exit 1
        fi
        # shellcheck disable=SC2088  # matching a LITERAL ~ (quoted in the env), expanding it here
        case "$path" in
            "~") path="$HOME" ;;
            "~/"*) path="$HOME/${path#"~/"}" ;;
        esac
        case "$path" in
            /*) ;;
            *) echo "build-msgs.sh: FLEET_MSGS_LOCAL '$entry': PATH must be absolute (or ~/...) —" \
                    "rig runs this script from the service directory, not from your shell's" >&2
               exit 1 ;;
        esac
        if [ ! -d "$path" ]; then
            echo "build-msgs.sh: FLEET_MSGS_LOCAL '$entry': $path is not a directory" >&2
            exit 1
        fi
        path="$(cd "$path" && pwd -P)"
        dest="$lsrc/$n"
        mkdir "$dest"
        # This root's OWN .git (directory, or a worktree's file): a plain directory inside some
        # other checkout must not borrow that checkout's identity.
        if [ -e "$path/.git" ]; then
            # The index's paths plus untracked, non-ignored files, minus tracked files deleted in
            # the working tree. Each step lands in a file so `set -e` sees a failing git or tar
            # (a pipeline would report only its last command). --no-recursion: a submodule or
            # nested repo is listed as a directory and must stay one, not pull in its contents.
            list="$work/files.$n"
            git -C "$path" ls-files -z --cached --others --exclude-standard > "$list"
            # shellcheck disable=SC2016  # $f expands in the inner sh, not here
            (cd "$path" && xargs -0 sh -c \
                'for f do if [ -e "$f" ] || [ -L "$f" ]; then printf "%s\0" "$f"; fi; done' sh \
                < "$list" > "$list.present")
            (cd "$path" && COPYFILE_DISABLE=1 tar -cf "$work/snapshot.$n.tar" \
                --no-recursion --null -T "$list.present")
            tar -xf "$work/snapshot.$n.tar" -C "$dest"
            if rev="$(git -C "$path" rev-parse --verify -q HEAD)"; then
                lref="$(git -C "$path" describe --tags --exact-match HEAD 2>/dev/null || echo "$rev")"
                # --no-optional-locks: status must not refresh (write) the checkout's index.
                # Submodules are ignored because their contents are not in the snapshot.
                if [ -n "$(git --no-optional-locks -C "$path" status --porcelain \
                        --ignore-submodules=all)" ]; then
                    lref="${lref}-dirty"
                    rev=unknown
                fi
            else
                lref=unknown  # no commits yet: nothing names this content
                rev=unknown
            fi
        else
            (cd "$path" && tar -cf - .) | tar -xf - -C "$dest"
            lref=unknown
            rev=unknown
        fi
        printf '%s\t%s\t%s\t%s\t%s\n' "$n" "$key" "$path" "$lref" "$rev" >> "$lsrc/sources.tsv"
        echo "fleet-ros-msgs: ${key} <- LOCAL ${path} (${lref})" >&2
        lkeys="${lkeys:+${lkeys},}${key}"
        n=$((n + 1))
    done
    set +f
    if [ "$n" -eq 0 ]; then lsrc=""; fi
fi

# ssh-form source repos clone inside the builder via BuildKit agent forwarding — the invoking
# machine's keys, never baked into a layer (msgs/Dockerfile's --mount=type=ssh RUN). The flag is
# passed only when the manifest needs it, so https-only fleets and agent-less CI are untouched.
# An EMPTY agent forwards fine and then fails the clone confusingly (macOS git uses key files
# directly, but BuildKit forwards the AGENT) — hence the ssh-add -l identity check, not just
# a socket check.
# With local checkouts and no agent, the build proceeds WITHOUT forwarding: the checkouts may cover
# every ssh-form repo, and build_msgs.py refuses, by name, any one they leave to a clone.
ssh_flag=""
if grep -Eq '^[[:space:]]*-?[[:space:]]*repo:[[:space:]]*["'"'"']?(git@|ssh://)' "$ctx/msgs-manifest.yaml"; then
    if [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1; then
        ssh_flag="--ssh default"
    elif [ -n "$lsrc" ]; then
        echo "build-msgs.sh: no loaded ssh-agent — building without agent forwarding; ssh-form" >&2
        echo "  repos FLEET_MSGS_LOCAL does not cover will be refused by name" >&2
    else
        echo "build-msgs.sh: the manifest declares ssh-form source repos, but no ssh-agent with" >&2
        echo "  loaded identities is available for BuildKit to forward. Fix on the build machine:" >&2
        echo "    eval \"\$(ssh-agent)\"   # if no agent is running" >&2
        echo "    ssh-add               # load the key that can clone those repos" >&2
        echo "  (keys are FORWARDED into the builder stage only — never baked into the image)" >&2
        exit 1
    fi
fi

echo "fleet-ros-msgs: building ${ref} FROM ${base} (manifest=${manifest})${RIG_BUILD_NO_CACHE:+ --no-cache}${ssh_flag:+ ${ssh_flag}}${lsrc:+ +local-sources}" >&2
# Unquoted on purpose: ${VAR:+word} expands to exactly one word or none (safe under `set -u`).
# shellcheck source=msgs/rig-build.sh
. "$msgs_dir/rig-build.sh"
export RIG_BUILD_PULL=0
# shellcheck disable=SC2086  # optional two-word --ssh default
rig_build "$ref" ${ssh_flag} ${lsrc:+--build-context} ${lsrc:+"local-sources=$lsrc"} \
    ${lsrc:+--label} ${lsrc:+"io.rig.build.local=$lkeys"} \
    --build-arg "BASE_IMAGE=${base}" \
    --build-arg "ROS_DISTRO=${distro}" \
    "$ctx"
