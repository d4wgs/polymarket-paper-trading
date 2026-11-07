"""
Finds the real slug format for the 5-minute and 15-minute markets, so config.MARKETS is right.

    python tools/find_markets.py              # searches bitcoin
    python tools/find_markets.py "ethereum"   # search anything

What it does:
  1. Searches Polymarket for "<asset> up or down" and prints active events.
  2. Guesses common slug formats for the CURRENT 5m and 15m windows and reports
     which ones exist.
Copy the working prefix (everything before the final -<numbers>) into config.py.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

from core.polymarket import GAMMA, window_start_for

GUESSES = {   # term -> [(slug_prefix, window_seconds)]
    "bitcoin":  [("btc-updown-5m", 300), ("btc-updown-15m", 900)],
    "ethereum": [("eth-updown-5m", 300), ("eth-updown-15m", 900)],
}


def search(term: str):
    print(f"\n=== Search: '{term} up or down' ===")
    try:
        r = requests.get(f"{GAMMA}/public-search", params={"q": f"{term} up or down", "limit_per_type": 25},
                         timeout=10)
        r.raise_for_status()
        events = r.json().get("events", []) or []
    except Exception as e:
        print("  search failed:", e)
        return
    shown = 0
    for ev in events:
        if ev.get("closed"):
            continue
        slug = ev.get("slug", "")
        prefix = re.sub(r"-\d{9,}$", "", slug)
        print(f"  {ev.get('title', '')[:60]:60}  slug={slug}")
        if prefix != slug:
            print(f"  {'':60}  -> slug_prefix = '{prefix}'")
        shown += 1
        if shown >= 12:
            break
    if not shown:
        print("  (no active events found)")


def try_guesses(term: str):
    for prefix, window in GUESSES.get(term, []):
        slug = f"{prefix}-{window_start_for(time.time(), window)}"
        try:
            r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=10)
            ok = r.ok and isinstance(r.json(), list) and len(r.json()) > 0
        except Exception:
            ok = False
        print(f"  guess {slug:45} {'FOUND <-- use this prefix' if ok else 'not found'}")
        if ok:
            m = r.json()[0]["markets"][0]
            print("     outcomes:", m.get("outcomes"), "| question:", m.get("question"))


if __name__ == "__main__":
    terms = sys.argv[1:] or ["bitcoin"]
    for t in terms:
        search(t)
        try_guesses(t)
