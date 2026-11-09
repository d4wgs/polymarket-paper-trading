"""
A7. Risk limits. Checked by the engine before any order that ADDS risk (buys and maker bids);
exits (sells, and buys that close an opposite position) are never blocked.

Global limits (config `risk:`), applied to each strategy's own paper account. (There is no daily
loss stop: a strategy stops for good when its portfolio value falls to portfolio.stop_equity -- see
Engine._check_stop.)
  max_usd_per_trade        clip every new buy to this many dollars
  max_usd_per_market       cost basis held in one round can't exceed this
  max_open_markets         rounds with an open position at the same time
  basis_kill_pct           kill switch: |spot - oracle| / oracle (like for like: spot averaged over
                           the oracle's 60s averaging window) above this blocks all new risk and pulls
                           every maker quote (default 0.05%)
  require_both_feeds       treat a missing spot or oracle price as a tripped kill switch
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("risk")


@dataclass
class RiskDecision:
    ok: bool
    usd: Optional[float] = None       # clipped dollar size (buys sized in dollars)
    shares: Optional[float] = None    # clipped share size
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.max_trade = cfg.get("max_usd_per_trade")
        self.max_market = cfg.get("max_usd_per_market")
        self.max_open = cfg.get("max_open_markets")
        self.basis_kill = cfg.get("basis_kill_pct", 0.05)
        self.require_both = bool(cfg.get("require_both_feeds", True))
        self.kill_active = False
        self._kill_logged = 0.0

    # ------------------------------------------------------------- kill switch
    def update_kill(self, basis: Optional[float], spot: Optional[float], oracle: Optional[float], now: float) -> bool:
        tripped, why = False, ""
        if basis is None:
            if self.require_both and (spot is None or oracle is None):
                tripped, why = True, f"missing {'spot' if spot is None else 'oracle'} price"
        elif self.basis_kill is not None and abs(basis) * 100 > self.basis_kill:
            tripped, why = True, f"basis {basis * 100:+.3f}% beyond {self.basis_kill}%"
        if tripped and (not self.kill_active or now - self._kill_logged > 60):
            log.warning("KILL SWITCH active: %s. No new risk; maker quotes pulled.", why)
            self._kill_logged = now
        elif not tripped and self.kill_active:
            log.info("Kill switch cleared (basis back within %s%%)", self.basis_kill)
        self.kill_active = tripped
        return tripped

    # ------------------------------------------------------------- per order
    def check_buy(self, strategy: str, *, usd: Optional[float], shares: Optional[float], price: Optional[float],
                  market_cost: float, open_markets: int, in_this_market: bool, equity: float,
                  hedge: bool = False) -> RiskDecision:
        """
        usd/shares: the order's size (one of them); price: its limit (to convert shares to dollars).
        market_cost: dollars already at risk in this round; open_markets: rounds with a position.
        hedge: the order completes a position held in another market (it reduces risk), so the kill
               switch and the open-markets limit don't apply; the dollar caps still do.
        """
        if self.kill_active and not hedge:
            return RiskDecision(False, reason="kill switch (basis / feed)")
        if self.max_open is not None and not hedge and not in_this_market and open_markets >= self.max_open:
            return RiskDecision(False, reason=f"max open markets ({self.max_open})")
        px = price if price and price > 0 else 1.0
        want = usd if usd is not None else (shares or 0.0) * px
        cap = float("inf")
        if self.max_trade is not None:
            cap = min(cap, self.max_trade)
        if self.max_market is not None:
            cap = min(cap, max(0.0, self.max_market - max(market_cost, 0.0)))
        if cap <= 0.0:
            return RiskDecision(False, reason=f"max $ per market (${self.max_market:,.0f})")
        if want <= cap:
            return RiskDecision(True, usd=usd, shares=shares)
        if usd is not None:
            return RiskDecision(True, usd=cap, reason="clipped")
        return RiskDecision(True, shares=int(cap / px * 100) / 100.0, reason="clipped")
