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
from core.fairvalue import MarketData
from core.fees import fee_model_name
from core.spot import SpotFeed
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


def wait_for_clock_sync(max_wait: float = 600):
    """
    Linux (Raspberry Pi): the Pi has no clock battery, so after a power cut it boots with the time it
    was last shut down until NTP corrects it. Every round, trade and equity row is timed off this
    clock, so wait (up to `max_wait` seconds) until the system says the clock is synchronized.
    """
    import shutil
    import subprocess
    import time
    if not sys.platform.startswith("linux") or not shutil.which("timedatectl"):
        return
    start, warned = time.time(), False
    while True:
        try:
            out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return                          # can't tell: don't block the bot
        if out == "yes":
            if warned:
                logging.info("System clock synchronized; starting")
            return
        if time.time() - start > max_wait:
            logging.warning("System clock still not synchronized after %.0fs; starting anyway", max_wait)
            return
        if not warned:
            logging.info("Waiting for the system clock to synchronize (NTP) before trading...")
            warned = True
        time.sleep(5)


def build_market_data() -> MarketData:
    sp = config.SPOT
    spot = SpotFeed(source=sp.get("source", "coinbase"), product=sp["product"], symbol=sp["symbol"],
                    half_life=config.MODEL.get("vol_half_life_seconds", 300),
                    stale_seconds=sp.get("stale_seconds", 5),
                    bootstrap=sp.get("bootstrap_vol_from_candles", True),
                    min_samples=config.MODEL.get("vol_min_samples", 30))
    spot.start()
    return MarketData(spot, config.MODEL)


def log_startup():
    fm = fee_model_name(config)
    if fm == "placeholder":
        logging.warning("WARNING: fee model is the PLACEHOLDER (fee_peak %.4f x 4 x p x (1-p) per share), not "
                        "the exchange's published schedule. PnL after fees is only approximate.", config.FEE_PEAK)
    else:
        logging.info("Fee model: %s (published schedule)", fm)
    if config.EXCHANGE != "polymarket_us" and config.RISK.get("require_both_feeds", True):
        logging.warning("polymarket.com: the settlement oracle (Chainlink) isn't wired in, so the oracle price is "
                        "missing and risk.require_both_feeds will keep the kill switch on. Set it to false to "
                        "trade with spot as the reference.")
    names = ", ".join(s.name for s in config.STRATEGIES) or "none"
    logging.info("Strategies enabled (%d): %s", len(config.STRATEGIES), names)


def main():
    lock = single_instance_lock()
    stop_cleanly_on_sigterm()
    keep_pc_awake()
    os.makedirs("data", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",       # the date matters once the log spans days (24/7 on a Pi)
        handlers=[logging.StreamHandler(),
                  # rotate at 5 MB, keep 5 old files (bot.log.1 ... bot.log.5) so the log can't fill the disk
                  RotatingFileHandler("data/bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")],
    )
    wait_for_clock_sync()
    if getattr(config, "EXCHANGE", "polymarket_intl") == "polymarket_us":
        feed = PolymarketUSFeed(window_seconds=config.WINDOW_SECONDS)
        logging.info("Exchange: Polymarket US (gateway.polymarket.us) -> %s", config.DB_PATH)
    else:
        feed = PolymarketFeed(window_seconds=config.WINDOW_SECONDS)
        logging.info("Exchange: polymarket.com -> %s", config.DB_PATH)
    md = build_market_data()
    log_startup()
    engine = Engine(config, config.STRATEGIES, feed, Storage(config.DB_PATH), market_data=md)
    try:
        engine.run()
    except KeyboardInterrupt:
        logging.info("Stopped. Data saved to %s", config.DB_PATH)
        engine.record_equity(engine.clock())
        engine.store.commit()


if __name__ == "__main__":
    main()
