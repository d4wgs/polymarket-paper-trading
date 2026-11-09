"""
Websocket recorder entry point (its own process; see core/recorder.py).

    python tools/ws_recorder.py                          # record into data/ws_events.db (systemd service)
    python tools/ws_recorder.py --probe 1800             # 30 min of RAW messages -> data/ws_probe.jsonl (no DB)
    python tools/ws_recorder.py --seconds 300            # record for 5 minutes, then stop

Needs PM_KEY_ID and PM_SECRET_KEY (environment or gitignored .env). Never prints them.
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config
from core.credentials import pm_credentials
from core.recorder import JsonlSink, Recorder, disk_report


async def amain(args):
    key, secret = pm_credentials()
    if not key or not secret:
        sys.exit("PM_KEY_ID / PM_SECRET_KEY are not set (environment or .env). See .env.example.")
    cfg = config.RECORDER
    if args.probe:
        sink = JsonlSink(args.out)
        logging.info("PROBE mode: raw messages -> %s for %ds", args.out, args.probe)
    else:
        from core.ws_store import WsStore
        sink = WsStore(args.db or cfg.get("db_path", "data/ws_events.db"), cfg)
    rec = Recorder(sink, config.MARKETS, cfg, (key, secret))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, rec.request_stop)
        except NotImplementedError:          # Windows
            pass
    limit = args.probe or args.seconds
    task = asyncio.create_task(rec.run())

    async def reporter():
        every = 60 if limit else float(cfg.get("disk_log_seconds", 600))
        while True:
            await asyncio.sleep(every)
            extra = "" if args.probe else " | " + disk_report(sink.path)
            logging.info("counts %s%s", rec.counts, extra)
    rep = asyncio.create_task(reporter())
    try:
        # stopped by a signal -> rec.run() returns; stopped by --seconds/--probe -> timeout
        await asyncio.wait_for(asyncio.shield(task), timeout=limit or None)
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
    finally:
        rec.request_stop()
        task.cancel()
        rep.cancel()
        sink.gap(time.time(), "stop")
        sink.close()
        logging.info("Stopped. counts %s", rec.counts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", type=float, default=0, help="seconds of raw messages to log to --out (JSONL)")
    ap.add_argument("--out", default="data/ws_probe.jsonl")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many seconds (default: run forever)")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
