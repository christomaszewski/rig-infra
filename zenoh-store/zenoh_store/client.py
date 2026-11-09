"""A small client for a vehicle's store: list, fetch on request (chunked, resumable), describe.

    python3 -m zenoh_store.client --vehicle v1 [--connect tcp/<router>:7447] describe
    python3 -m zenoh_store.client --vehicle v1 ls [--run <id>] [--fetch auto,latest] [--prefix cam_front/]
    python3 -m zenoh_store.client --vehicle v1 get <run> <path> [-o out] [--chunk-kb 32]

Queries go out at DATA_LOW priority, so the vehicle's replies queue behind telemetry on the link,
and a fetch has one chunk in flight at a time: the added delay on a shared link stays near
chunk size / link rate (docs/zenoh-store-plan.md, Phase 0). A fetch writes `<out>.part` and
resumes from it; the file is renamed into place only once its size matches the store's.
This is the seed of the ground mirror (Phase 2), which adds following events and fetch rules.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional


class StoreError(RuntimeError):
    def __init__(self, obj):
        self.code = obj.get("error", "error") if isinstance(obj, dict) else "error"
        self.obj = obj
        super().__init__(f"{self.code}: {obj.get('detail', '') if isinstance(obj, dict) else obj}")


def _decode(zbytes) -> bytes:
    if zbytes is None:
        return b""
    return zbytes.to_bytes() if hasattr(zbytes, "to_bytes") else bytes(zbytes)


class StoreClient:
    def __init__(self, session, vehicle: str, instance: str = "zenoh-store", timeout_s: float = 30.0,
                 low_priority: bool = True):
        import zenoh
        self.z = zenoh
        self.session = session
        self.vehicle = vehicle
        self.base = f"fleet/{vehicle}/svc/{instance}/store"
        self.timeout_s = timeout_s
        self.priority = zenoh.Priority.DATA_LOW if low_priority else zenoh.Priority.DEFAULT

    def _one(self, selector: str):
        """(payload bytes, attachment dict) of the first reply, or StoreError."""
        replies = list(self.session.get(selector, timeout=self.timeout_s, priority=self.priority))
        if not replies:
            raise StoreError({"error": "no_reply", "detail": f"nothing answered {selector} "
                                                             f"(is the store up and reachable?)"})
        r = replies[0]
        if r.ok is None:
            try:
                raise StoreError(json.loads(_decode(r.err.payload)))
            except ValueError:
                raise StoreError({"error": "error", "detail": _decode(r.err.payload).decode(errors="replace")})
        att = r.ok.attachment
        return _decode(r.ok.payload), (json.loads(_decode(att)) if att is not None else {})

    def describe(self) -> dict:
        return json.loads(self._one(self.base)[0])

    def index(self, run: str = "open", after: Optional[str] = None, prefix: str = "",
              fetch: str = "", limit: int = 500) -> dict:
        params = [f"run={run}", f"limit={limit}"]
        if after:
            params.append(f"after={after}")
        if prefix:
            params.append(f"prefix={prefix}")
        if fetch:
            params.append(f"fetch={fetch}")
        return json.loads(self._one(f"{self.base}/index?{';'.join(params)}")[0])

    def list_all(self, run: str = "open", **kw):
        after = None
        while True:
            page = self.index(run, after=after, **kw)
            yield from ({**f, "run": page["run"]} for f in page["files"])
            after = page["next"]
            if not page["more"]:
                return

    def key(self, run: str, path: str) -> str:
        return f"fleet/{self.vehicle}/store/{run}/{path}"

    def get(self, run: str, path: str, offset: int = 0, length: Optional[int] = None, meta: bool = False):
        params = []
        if meta:
            params.append("meta")
        if offset:
            params.append(f"offset={offset}")
        if length is not None:
            params.append(f"length={length}")
        sel = self.key(run, path) + (f"?{';'.join(params)}" if params else "")
        return self._one(sel)

    def fetch(self, run: str, path: str, dest: str, chunk: int = 32 * 1024, progress=None) -> int:
        """Fetch one file into `dest`, resuming `dest.part`. Returns the file size."""
        _, att = self.get(run, path, meta=True)
        size = att["size"]
        part = dest + ".part"
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size:
            have = 0
        with open(part, "r+b" if have else "wb") as f:
            f.seek(have)
            f.truncate()
            while have < size:
                data, att = self.get(run, path, offset=have, length=min(chunk, size - have))
                if not data:
                    raise StoreError({"error": "short_read", "detail": f"no bytes at offset {have}"})
                f.write(data)
                have += len(data)
                if progress:
                    progress(have, size)
        if os.path.getsize(part) != size:
            raise StoreError({"error": "size_mismatch", "detail": f"{part}: {os.path.getsize(part)} != {size}"})
        os.replace(part, dest)
        return size


def _session(connect: list):
    import zenoh
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"' if connect else '"peer"')
    if connect:
        conf.insert_json5("connect/endpoints", json.dumps(connect))
    return zenoh.open(conf)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="zenoh_store.client")
    ap.add_argument("--vehicle", required=True)
    ap.add_argument("--instance", default="zenoh-store")
    ap.add_argument("--connect", action="append", default=[], help="router endpoint (repeatable)")
    ap.add_argument("--timeout", type=float, default=30.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("describe")
    ls = sub.add_parser("ls")
    ls.add_argument("--run", default="open")
    ls.add_argument("--fetch", default="", help="comma list of auto,latest,on_request")
    ls.add_argument("--prefix", default="")
    g = sub.add_parser("get")
    g.add_argument("run")
    g.add_argument("path")
    g.add_argument("-o", "--output")
    g.add_argument("--chunk-kb", type=int, default=32)
    args = ap.parse_args(argv)
    session = _session(args.connect)
    try:
        c = StoreClient(session, args.vehicle, args.instance, args.timeout)
        if args.cmd == "describe":
            print(json.dumps(c.describe(), indent=2))
        elif args.cmd == "ls":
            for f in c.list_all(args.run, prefix=args.prefix, fetch=args.fetch):
                print(f"{f['size']:>12}  {f['fetch']:<10}  {f['run']}/{f['path']}")
        else:
            out = args.output or os.path.basename(args.path)
            size = c.fetch(args.run, args.path, out, args.chunk_kb * 1024,
                           progress=lambda have, total: print(f"\r{have}/{total}", end="", file=sys.stderr))
            print(f"\n{out}: {size} bytes", file=sys.stderr)
    except StoreError as e:
        print(f"zenoh_store.client: {e}", file=sys.stderr)
        return 1
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
