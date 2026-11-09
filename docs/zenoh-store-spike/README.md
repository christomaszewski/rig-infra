# zenoh-store spike

Phase 0 measurements for [../zenoh-store-plan.md](../zenoh-store-plan.md): how a file transfer over a
slow link affects telemetry delay, whole versus chunked. Needs `eclipse-zenoh==1.9.0`.

```sh
python3 link_relay.py 17512 17511 125000 0.03 &      # 1 Mbit/s, 30 ms each way: ground -> :17512 -> :17511
python3 vehicle.py 17511 ./files &                   # serves ./files/<name>, publishes 20 Hz telemetry
head -c 671744 /dev/urandom > files/tiff656k
python3 ground.py 17512 whole   tiff656k 671744      # one reply
python3 ground.py 17512 chunked tiff656k 671744 32768
python3 ground.py 17512 push    tiff656k 671744      # the vehicle puts it to a ground subscriber
```

Both peers disable Zenoh shared memory: on one host it would carry large payloads outside TCP and
past the relay.
