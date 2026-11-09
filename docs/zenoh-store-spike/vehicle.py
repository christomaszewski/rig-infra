"""Vehicle side (usage: vehicle.py PORT FILES_DIR): a store prototype (files served whole or by offset/length) plus a
20 Hz telemetry publisher (default priority) whose payload carries its send time."""
import json, os, sys, time, zenoh
port, files_dir = sys.argv[1], sys.argv[2]
c = zenoh.Config(); c.insert_json5("mode", '"peer"'); c.insert_json5("scouting/multicast/enabled", "false")
c.insert_json5("scouting/gossip/enabled", "false"); c.insert_json5("transport/shared_memory/enabled", "false"); c.insert_json5("listen/endpoints", json.dumps([f"tcp/127.0.0.1:{port}"]))
s = zenoh.open(c)
PREFIX = "fleet/7/store/R/"
def params(q):
    out = {}
    for part in str(q.parameters).split(";"):
        if "=" in part:
            k, v = part.split("=", 1); out[k] = v
    return out
def on_query(q):
    name = str(q.key_expr)[len(PREFIX):]
    path = os.path.join(files_dir, name)
    p = params(q)
    with open(path, "rb") as f:
        if "offset" in p:
            f.seek(int(p["offset"])); data = f.read(int(p["length"]))
        else:
            data = f.read()
    q.reply(q.key_expr, data)
qa = s.declare_queryable(PREFIX + "**", on_query)
pub = s.declare_publisher("fleet/7/telemetry")
def push(name):
    with open(os.path.join(files_dir, name), "rb") as f:
        s.put(PREFIX + name, f.read(), priority=zenoh.Priority.DATA_LOW, congestion_control=zenoh.CongestionControl.BLOCK)
s.declare_subscriber("fleet/7/push_cmd", lambda smp: push(smp.payload.to_bytes().decode()))
seq = 0
while True:
    pub.put(json.dumps({"t": time.time_ns(), "seq": seq}).encode() + b" " * 60); seq += 1
    time.sleep(0.05)
