"""
Start the live paper-trading bot:      python run.py
Stop it any time with Ctrl+C. Restarting picks up where it left off
(cash and open positions are rebuilt from data/paper.db).
"""
import logging
import os
from logging.handlers import RotatingFileHandler
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)   # so data/ always lands next to this file

import config
from core.engine import Engine
from core.polymarket import PolymarketFeed
from core.polymarket_us import PolymarketUSFeed
from core.storage import Storage


def single_instance_lock(port: int = 48217):
    """Two bots on one database would double every trade. Holding a local port stops that."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sys.exit("The bot is already running (another run.py holds the lock). Close it first.")
    return sock   # keep a reference so the lock lasts as long as the process


def keep_pc_awake():
    """Windows: stop the PC from sleeping while the bot runs (a sleeping PC = missed windows)."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def stop_cleanly_on_sigterm():
    """Linux services (systemd) stop the bot with SIGTERM: treat it like Ctrl+C so it saves and exits."""
    import signal

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)


def main():
    lock = single_instance_lock()
    stop_cleanly_on_sigterm()
    keep_pc_awake()
    os.makedirs("data", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(),
                  # rotate at 5 MB, keep 5 old files (bot.log.1 ... bot.log.5) so the log can't fill the disk
                  RotatingFileHandler("data/bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")],
    )
    if getattr(config, "EXCHANGE", "polymarket_intl") == "polymarket_us":
        feed = PolymarketUSFeed(window_seconds=config.WINDOW_SECONDS)
        logging.info("Exchange: Polymarket US (gateway.polymarket.us) -> %s", config.DB_PATH)
    else:
        feed = PolymarketFeed(window_seconds=config.WINDOW_SECONDS)
        logging.info("Exchange: polymarket.com -> %s", config.DB_PATH)
    engine = Engine(config, config.STRATEGIES, feed, Storage(config.DB_PATH))
    try:
        engine.run()
    except KeyboardInterrupt:
        logging.info("Stopped. Data saved to %s", config.DB_PATH)
        engine.record_equity(engine.clock())
        engine.store.commit()


if __name__ == "__main__":
    main()
