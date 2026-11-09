"""Entry point: `python3 -m zenoh_store [-c instance.yaml]`.

In the container the launcher hands everything over through the environment:
  ZENOH_STORE_CONFIG    the validated `store:` block as JSON (zenoh-store-up renders it)
  ZENOH_STORE_INSTANCE  the instance name (the <instance> key chunk)
  ZENOH_STORE_DATA      the data dir as mounted (/data); its `current` link names the open run
  VEHICLE_ID            set by rig; the hostname otherwise
Exit code 2 is a configuration error.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading

from .config import ConfigError, from_json, load_file

log = logging.getLogger("zenoh-store")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="zenoh-store", description="the vehicle's run store over Zenoh")
    ap.add_argument("-c", "--config", help="an instance YAML/JSON file (default: $ZENOH_STORE_CONFIG)")
    ap.add_argument("--data", default=os.environ.get("ZENOH_STORE_DATA") or os.environ.get("RIG_DATA_DIR") or "/data")
    ap.add_argument("--vehicle", default=os.environ.get("VEHICLE_ID") or socket.gethostname())
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        if args.config:
            instance, settings = load_file(args.config)
        else:
            settings = from_json(os.environ.get("ZENOH_STORE_CONFIG", ""))
            instance = os.environ.get("ZENOH_STORE_INSTANCE") or "zenoh-store"
    except (OSError, ConfigError, ValueError) as e:
        log.error("config error: %s", e)
        return 2

    from .store import Store
    from .zenoh_io import ZenohStore, connect_endpoints, endpoints
    store = Store(settings, args.data, args.vehicle, instance)
    log.info("zenoh-store: vehicle=%s instance=%s data=%s open_run=%s rules=%d", args.vehicle, instance,
             args.data, store.open_run or "-", len(store.policy.rules))
    store.start()
    adapter = ZenohStore(store, connect_endpoints(settings["zenoh_connect"]), endpoints(settings["zenoh_listen"]))
    adapter.start()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    log.info("zenoh-store: stopping")
    adapter.close()
    store.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
