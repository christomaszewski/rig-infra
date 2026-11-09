"""Rate-limited, delayed TCP relay standing in for the radio (usage: link_relay.py LISTEN TARGET BYTES_PER_S DELAY_S): listen on B, forward to A, shape both
directions to RATE bytes/s with a fixed one-way delay. It reads one small chunk at a time, so a
backlog stays in the SENDER's socket buffer (as it would on the vehicle), not in the relay."""
import asyncio, sys, time
LISTEN, TARGET, RATE, DELAY = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
CHUNK = 1460

STATS = {"conns": 0}
async def pipe(reader, writer, tag="?"):
    q = asyncio.Queue()
    STATS[tag] = 0
    async def shaper():
        next_t = time.monotonic()
        while True:
            data = await reader.read(CHUNK)
            if not data:
                await q.put(None); return
            STATS[tag] += len(data)
            next_t = max(next_t, time.monotonic()) + len(data) / RATE
            await asyncio.sleep(max(0, next_t - time.monotonic()))
            await q.put((time.monotonic() + DELAY, data))
    async def deliver():
        while True:
            item = await q.get()
            if item is None:
                writer.close(); return
            due, data = item
            await asyncio.sleep(max(0, due - time.monotonic()))
            writer.write(data); await writer.drain()
    await asyncio.gather(shaper(), deliver())

async def handle(cr, cw):
    STATS["conns"] += 1; n = STATS["conns"]
    ur, uw = await asyncio.open_connection("127.0.0.1", TARGET)
    await asyncio.gather(pipe(cr, uw, f"up{n}"), pipe(ur, cw, f"down{n}"), return_exceptions=True)
    print(f"relay: connection {n} closed, bytes {dict((k, v) for k, v in STATS.items() if k.endswith(str(n)))}",
          file=sys.stderr, flush=True)

async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", LISTEN)
    async with srv:
        await srv.serve_forever()
asyncio.run(main())
