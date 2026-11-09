"""Ground side (usage: ground.py RELAY_PORT whole|chunked|push FILE SIZE [CHUNK]): measures telemetry delay (now - send time) while a transfer runs, for each mode:
whole-file reply, paced 64 KB chunks (one in flight), and a push the vehicle sends to a subscriber."""
import json, statistics, sys, threading, time, zenoh
relay_port, mode, name, size = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
c = zenoh.Config(); c.insert_json5("mode", '"peer"'); c.insert_json5("scouting/multicast/enabled", "false")
c.insert_json5("scouting/gossip/enabled", "false"); c.insert_json5("transport/shared_memory/enabled", "false"); c.insert_json5("listen/endpoints", '[]')
c.insert_json5("connect/endpoints", json.dumps([f"tcp/127.0.0.1:{relay_port}"]))
s = zenoh.open(c)
lat, lock, window = [], threading.Lock(), {"on": False}
def on_tel(smp):
    d = json.loads(smp.payload.to_bytes().decode().strip()); ms = (time.time_ns() - d["t"]) / 1e6
    with lock:
        if window["on"]: lat.append(ms)
s.declare_subscriber("fleet/7/telemetry", on_tel)
got = threading.Event()
s.declare_subscriber("fleet/7/store/R/**", lambda smp: got.set())
time.sleep(3)                                   # link up, telemetry flowing
with lock: lat.clear()
window["on"] = True; base = []
time.sleep(2)
with lock: base, lat[:] = list(lat), []
t0 = time.monotonic()
key = "fleet/7/store/R/" + name
if mode == "whole":
    r = list(s.get(key, timeout=120, priority=zenoh.Priority.DATA_LOW)); assert len(r[0].ok.payload.to_bytes()) == size
elif mode == "chunked":
    off, CH = 0, int(sys.argv[5]) if len(sys.argv) > 5 else 65536
    while off < size:
        r = list(s.get(f"{key}?offset={off};length={CH}", timeout=60, priority=zenoh.Priority.DATA_LOW)); off += len(r[0].ok.payload.to_bytes())
elif mode == "push":
    s.put("fleet/7/push_cmd", name.encode()); assert got.wait(120)
dt = time.monotonic() - t0
time.sleep(0.5)
window["on"] = False
def stats(v):
    v = sorted(v); return f"p50 {statistics.median(v):6.0f} ms  p95 {v[int(len(v) * .95)]:6.0f} ms  max {v[-1]:6.0f} ms" if v else "none"
print(f"{mode:8s} {name:10s} chunk={sys.argv[5] if len(sys.argv) > 5 else 65536:>6} transfer {dt:5.1f} s  telemetry idle: {stats(base)}  |  during: {stats(lat)}")
s.close()
