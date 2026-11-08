"""
Portfolio dashboard.   Run:  streamlit run dashboard.py
Opens in your browser at http://localhost:8501. Reads the database only,
so it's safe to keep open while the bot runs.

Layout: live section (refreshes every LIVE_SECONDS) with headline numbers, the leaderboard,
open bets and last-hour charts; then all-time analysis and a per-strategy drill-down.

"Restart charts from now" (sidebar) only changes what the CHARTS show: they start at the moment
you press it. Nothing in the database is touched and the bot keeps recording; tables and totals
stay all-time. The start time is kept in the page URL (?since=...), so a browser refresh keeps it.
"""
import glob
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config
from core import analytics, metrics

st.set_page_config(page_title="Polymarket Paper Desk", page_icon="📈", layout="wide")

LIVE_SECONDS = 5          # how often the live section refreshes
WINDOW = 3600             # the time charts always show this many seconds (last hour)

# Categorical palette (validated reference palette; light / dark steps). Colors follow the
# strategy (its order in the database), never its rank, so filtering never repaints a line.
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
DARK = getattr(getattr(st.context, "theme", None), "type", "light") == "dark"
PALETTE = PALETTE_DARK if DARK else PALETTE_LIGHT
MUTED = "#8a8985"

MARKET_NAMES = {m["asset"]: m.get("name") or m["asset"] for m in config.MARKETS}
# Break-even lines on the calibration chart (taker fee coefficients)
US_FEE_LINES = [(0.095, "Θ 0.095 (Polymarket US, under $10k 30-day volume)"),
                (0.0695, "Θ 0.0695 (Polymarket US, $250k+ volume / first 7 days)")]
INTL_FEE_LINES = [(0.07, "0.07 (polymarket.com crypto)")]
DB_LABELS = {"data/paper_us.db": "Live paper trading · Polymarket US", "data/sim_us.db": "Simulator · Polymarket US",
             "data/paper.db": "Live paper trading · polymarket.com", "data/sim.db": "Simulator · polymarket.com"}
CURRENT = {s.name for s in config.STRATEGIES}   # strategies in config.py right now (others are "retired")
TITLES = {s.name: s.title for s in config.STRATEGIES}
DESCRIPTIONS = {s.name: s.description for s in config.STRATEGIES}
DETAILS = {s.name: s.details for s in config.STRATEGIES}


def title_of(name):
    return TITLES.get(name, f"{name} (retired)")


def market_of(asset):
    return MARKET_NAMES.get(asset, asset)


def relabel(df, col="strategy"):
    """Swap internal strategy names for the friendly titles (for charts and tables)."""
    if df is None or not len(df) or col not in df:
        return df
    df = df.copy()
    df[col] = df[col].map(title_of)
    return df


def money(x, signed=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+,.2f}".replace("+", "+$").replace("-", "-$") if signed else f"${x:,.2f}"


def pct(x, signed=False, digits=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+.{digits}f}%" if signed else f"{x:.{digits}f}%"


def style(fig, height, y_title=None, legend=True):
    """One look for every chart: thin lines, recessive grid, crosshair hover, legend on top."""
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8), legend_title_text="",
        showlegend=legend, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0),
        yaxis_title=y_title, xaxis_title=None)
    fig.update_xaxes(showgrid=False, showspikes=True, spikemode="across", spikethickness=1,
                     spikecolor=MUTED, spikedash="solid")
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)", zerolinecolor="rgba(128,128,128,0.4)")
    fig.update_traces(selector=dict(type="scatter", mode="lines"), line_width=2)
    return fig


SIGNED_MONEY = lambda x: money(x, signed=True)   # noqa: E731


def table(df, formats, column_config=None, **kw):
    """
    A dataframe with consistent number formats ('—' for blanks, '+$1.23' for signed money).
    Values are formatted to text here: Streamlit shows empty numbers as "None" whatever the
    Styler says. Rows keep the order they arrive in (newest first / best first).
    """
    out = df.copy()
    for col, f in formats.items():
        if col in out:
            fn = f if callable(f) else f.format
            out[col] = [("—" if v is None or (not isinstance(v, str) and pd.isna(v)) else fn(v)) for v in out[col]]
    st.dataframe(out, hide_index=True, width="stretch", column_config=column_config, **kw)


def TS(t):
    return "—" if pd.isna(t) else f"{t:%b %d %H:%M:%S}"


def chart(fig, key):
    st.plotly_chart(fig, width="stretch", key=key, config={"displayModeBar": False})


# ---------------------------------------------------------------- data (cached)
def _db_version(path):
    """Changes whenever the bot writes (the WAL file grows), so the cache below refreshes then."""
    return tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in (path, path + "-wal"))


@st.cache_resource(show_spinner=False)
def _dd_tracker(path, db_id):
    return metrics.DrawdownTracker(path)     # one per database file, shared by every browser tab


@st.cache_data(ttl=60, show_spinner=False)
def load_max_dd(path, minute):
    # Only reads equity rows added since the last refresh. db_id changes if the file is replaced
    # (e.g. deleted and started fresh), which starts a new tracker instead of keeping old peaks.
    try:
        db_id = os.stat(path).st_ino or os.stat(path).st_ctime
    except OSError:
        db_id = 0
    return _dd_tracker(path, db_id).update()


@st.cache_data(ttl=60, show_spinner=False, max_entries=4)
def load_data(path, version, since=None):
    return metrics.load(path, equity_window=WINDOW, max_dd=load_max_dd(path, int(time.time() // 60)), since=since)


# ---------------------------------------------------------------- sidebar
st.sidebar.title("📈 Paper Desk")
dbs = sorted(p.replace("\\", "/") for p in glob.glob("data/*.db"))   # Windows returns backslashes
if not dbs:
    st.info("No data yet. Start the bot (`python run.py`) or the simulator (`python tools/simulate.py`).")
    st.stop()
default_db = config.DB_PATH.replace("\\", "/")
db = st.sidebar.selectbox(
    "Data source", dbs, index=dbs.index(default_db) if default_db in dbs else 0,
    format_func=lambda p: DB_LABELS.get(p, p))

# ---- chart start ("Restart charts from now"): display only, kept in the URL so a refresh keeps it
try:
    SINCE = float(st.query_params.get("since")) if st.query_params.get("since") else None
except (TypeError, ValueError):
    SINCE = None
with st.sidebar.container(border=True):
    st.markdown("**Charts**")
    if st.button("Restart charts from now", icon=":material/restart_alt:", width="stretch",
                 help="Charts start fresh from this moment. Saved data is untouched and the bot keeps "
                      "recording; tables and totals stay all-time."):
        st.query_params["since"] = str(int(time.time()))
        st.cache_data.clear()
        st.rerun()
    if SINCE is not None:
        st.caption(f"Charts since {pd.Timestamp(SINCE, unit='s', tz='UTC').tz_convert(metrics.TZ):%b %d %H:%M:%S} ET")
        if st.button("Show full history", width="stretch"):
            del st.query_params["since"]
            st.cache_data.clear()
            st.rerun()
    else:
        st.caption("Showing the last hour (live charts) and all history (analysis charts).")

d0 = load_data(db, _db_version(db), SINCE)
EXCHANGE = d0.get("exchange", "polymarket_intl")
FEE_LINES = US_FEE_LINES if EXCHANGE == "polymarket_us" else INTL_FEE_LINES
if not len(d0["strategies"]):
    st.info("The database is empty. Start the bot and give it a minute.")
    st.stop()

all_strats = d0["strategies"]["name"].tolist()
retired = [n for n in all_strats if n not in CURRENT]
show_retired = bool(retired) and st.sidebar.toggle(
    f"Show retired strategies ({len(retired)})", value=False,
    help="Strategies in this database that are no longer in config.py (e.g. replaced by a _v2).")
shown = all_strats if show_retired else ([n for n in all_strats if n in CURRENT] or all_strats)
picked = st.sidebar.multiselect("Strategies", shown, default=shown, format_func=title_of)
live = st.sidebar.toggle(f"Live (refresh every {LIVE_SECONDS}s)", value=True)
# config.py order first, then retired ones: fixed per strategy, so filtering never repaints a line
color_order = [s.name for s in config.STRATEGIES] + [n for n in all_strats if n not in CURRENT]
color_map = {title_of(s): PALETTE[i % len(PALETTE)] for i, s in enumerate(color_order)}

with st.sidebar.expander("Strategy guide"):
    for name in picked:
        st.markdown(f"**{title_of(name)}** — {DESCRIPTIONS.get(name) or 'no description'}")
        if DETAILS.get(name):
            with st.popover("How it works"):
                st.markdown(DETAILS[name])
with st.sidebar.expander("Glossary"):
    st.markdown(
        "- **t-stat**: average P&L ÷ its standard error. Under ~2 = could be luck.\n"
        "- **Max DD**: worst drop from a previous high.\n"
        "- **Profit / $1**: realized P&L per dollar spent buying (rounds that have finished only).\n"
        "- **Avg price**: average price paid per share; at 0.90 you need a >91% win rate to break even.\n"
        "- **Unrealized**: what an open bet is worth at the current mid, minus what it cost.\n"
        "- **Fee Θ**: taker fee coefficient on the latest trade. Polymarket US fee = Θ × contracts × p × (1−p), "
        "rounded to the cent; Θ is 0.095 below $10k of 30-day volume, 0.085 / 0.075 / 0.0695 above.\n"
        "- **Volume** (Polymarket US): contracts × price of the side received (a sale counts contracts × (1 − price)).\n"
        "- **Stopped**: the strategy's portfolio value fell to the stop level, so it no longer trades.")


# ---------------------------------------------------------------- live market window (BTC chart + order book)
BID_COLOR, ASK_COLOR = ("#199e70", "#e66767") if DARK else ("#1baf7a", "#e34948")
MARKET_REFRESH = 2        # seconds; the bot records a snapshot + book about every 2s


@st.cache_data(ttl=MARKET_REFRESH, show_spinner=False, max_entries=8)
def load_market_view(path, asset, version):
    """The current round of `asset`: its price path, the round's terms and the latest order book."""
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        last = con.execute("SELECT slug FROM snapshots WHERE asset = ? ORDER BY ts DESC LIMIT 1", (asset,)).fetchone()
        if not last:
            return None
        slug = last[0]
        cols = [r[1] for r in con.execute("PRAGMA table_info(snapshots)")]
        want = [c for c in ("ts", "seconds_left", "spot", "index_price", "price_to_beat", "k", "up_bid", "up_ask",
                            "fv_up") if c in cols]
        snaps = pd.read_sql_query(f"SELECT {', '.join(want)} FROM snapshots WHERE slug = ? ORDER BY ts", con,
                                  params=(slug,))
        book = con.execute("SELECT ts, seconds_left, up_bids, up_asks FROM books WHERE asset = ? "
                           "ORDER BY ts DESC LIMIT 1", (asset,)).fetchone()
        try:
            win = con.execute("SELECT window_start, window_end, price_to_beat FROM windows WHERE slug = ?",
                              (slug,)).fetchone()
        except sqlite3.OperationalError:      # older databases have no windows table
            win = None
    finally:
        con.close()
    for c in ("spot", "index_price", "price_to_beat", "k", "fv_up"):
        if c not in snaps:
            snaps[c] = np.nan
    snaps["time"] = pd.to_datetime(snaps["ts"], unit="s", utc=True).dt.tz_convert(metrics.TZ)
    window_s = next((m["window_seconds"] for m in config.MARKETS if m["asset"] == asset), 300)
    if win:
        w_start, w_end, ptb = win
    else:
        w_end = float((snaps["ts"] + snaps["seconds_left"]).iloc[-1])
        w_start, ptb = w_end - window_s, None
    ptb = ptb or (snaps["price_to_beat"].dropna().iloc[-1] if snaps["price_to_beat"].notna().any() else None)
    out = {"slug": slug, "snaps": snaps, "start": w_start, "end": w_end, "ptb": ptb, "book": None}
    if book:
        out["book"] = {"ts": book[0], "seconds_left": book[1], "bids": json.loads(book[2] or "[]"),
                       "asks": json.loads(book[3] or "[]")}
    return out


def _et(ts):
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(metrics.TZ)


def market_panel(asset):
    """BTC price for the current round (with the price to beat) and the round's live order book."""
    v = load_market_view(db, asset, _db_version(db))
    if v is None or not len(v["snaps"]):
        st.caption(f"No data for {market_of(asset)} yet. Is the bot running?")
        return
    s, ptb, now = v["snaps"], v["ptb"], time.time()
    last = s.iloc[-1]
    left = max(0.0, v["end"] - now)
    idx = last["index_price"] if pd.notna(last["index_price"]) else None
    spot = last["spot"] if pd.notna(last["spot"]) else None
    up_mid = (last["up_bid"] + last["up_ask"]) / 2 if pd.notna(last["up_bid"]) and pd.notna(last["up_ask"]) else None

    a, b, c, e, f = st.columns(5)
    a.metric("BTC index", f"${idx:,.2f}" if idx else "—", border=True,
             help="CF Benchmarks BRTI as Polymarket US serves it: a trailing 60-second average, the same "
                  "quantity the round settles on (and what Polymarket's own chart shows).")
    b.metric("Price to beat", f"${ptb:,.2f}" if ptb else "—",
             (f"{idx - ptb:+,.2f} now" if idx and ptb else None), border=True,
             help="The round's open value. Up wins if the close (the 60s average at the end) is at or above it.")
    c.metric("Coinbase spot", f"${spot:,.2f}" if spot else "—", border=True,
             help="The fast feed the model uses. It leads the index by ~30s because the index is a 60s average.")
    e.metric("Up price (mid)", f"{up_mid:.3f}" if up_mid is not None else "—",
             (f"model {last['fv_up']:.3f}" if pd.notna(last["fv_up"]) else None), delta_color="off",
             delta_arrow="off", border=True,
             help="Market mid for Up; the delta shows the bot's fair-value model for comparison.")
    f.metric("Time left", f"{int(left // 60)}:{int(left % 60):02d}", border=True,
             help=f"Round {_et(v['start']):%H:%M}–{_et(v['end']):%H:%M} ET.")

    x0, x1 = _et(v["start"]), _et(v["end"])
    g1, g2 = st.columns([3, 2])
    with g1:
        fig = go.Figure()
        if s["spot"].notna().any():
            fig.add_trace(go.Scatter(x=s["time"], y=s["spot"], mode="lines", name="Coinbase spot",
                                     line=dict(color=PALETTE[1], width=1.5)))
        if s["index_price"].notna().any():
            fig.add_trace(go.Scatter(x=s["time"], y=s["index_price"], mode="lines", name="BTC index (60s avg)",
                                     line=dict(color=PALETTE[0], width=2.5)))
        if ptb:
            fig.add_hline(y=ptb, line_dash="dash", line_color=MUTED, line_width=1.5,
                          annotation_text=f"Price to beat ${ptb:,.2f}", annotation_position="top left",
                          annotation_font_color=MUTED)
        fig.update_xaxes(range=[x0, x1])
        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
        chart(style(fig, 330, "BTC (USD)"), f"btc_{asset}")
    with g2:
        fig = go.Figure()
        um = s[["up_bid", "up_ask"]].mean(axis=1)
        fig.add_trace(go.Scatter(x=s["time"], y=um, mode="lines", name="Up price (mid)",
                                 line=dict(color=PALETTE[0], width=2)))
        if s["fv_up"].notna().any():
            fig.add_trace(go.Scatter(x=s["time"], y=s["fv_up"], mode="lines", name="Model fair value",
                                     line=dict(color=PALETTE[3], width=1.5, dash="dot")))
        fig.add_hline(y=0.5, line_dash="dot", line_color=MUTED, line_width=1)
        fig.update_xaxes(range=[x0, x1])
        fig.update_yaxes(range=[0, 1])
        chart(style(fig, 330, "Probability of Up"), f"upprob_{asset}")

    bk = v["book"]
    st.markdown("**Order book** · Up (YES) contract"
                + (f" · as of {_et(bk['ts']):%H:%M:%S} ET" if bk else ""))
    if not bk or not (bk["bids"] or bk["asks"]):
        st.caption("No order book recorded for this round yet.")
        return
    bids = pd.DataFrame(bk["bids"], columns=["price", "size"])
    asks = pd.DataFrame(bk["asks"], columns=["price", "size"])
    for t in (bids, asks):
        t["value"] = t["price"] * t["size"]
        t["cum_size"] = t["size"].cumsum()
    best_bid = bids["price"].max() if len(bids) else None
    best_ask = asks["price"].min() if len(asks) else None
    if best_bid is not None and best_ask is not None:
        st.caption(f"Spread {best_ask - best_bid:.3f} · Down is the mirror of this book: Down ask = 1 − Up bid "
                   f"({1 - best_bid:.3f}), Down bid = 1 − Up ask ({1 - best_ask:.3f}).")
    fmt = {"price": "{:.3f}", "size": "{:,.0f}", "value": "${:,.0f}", "cum_size": "{:,.0f}"}
    names = {"price": "Price", "size": "Contracts", "value": st.column_config.Column("$", help="Price × contracts."),
             "cum_size": st.column_config.Column("Total", help="Contracts from the best price out to this level.")}
    h = 38 * (max(len(bids), len(asks), 1) + 1) + 4
    o1, o2, o3 = st.columns([1, 1, 2])
    with o1:
        st.markdown(f"<span style='color:{BID_COLOR};font-weight:600'>Bids</span> · buy Up", unsafe_allow_html=True)
        table(bids[["price", "size", "value", "cum_size"]], fmt, column_config=names, height=h)
    with o2:
        st.markdown(f"<span style='color:{ASK_COLOR};font-weight:600'>Asks</span> · sell Up", unsafe_allow_html=True)
        table(asks[["price", "size", "value", "cum_size"]], fmt, column_config=names, height=h)
    with o3:
        fig = go.Figure()
        if len(bids):
            fig.add_trace(go.Scatter(x=bids["price"], y=bids["cum_size"], mode="lines", line_shape="hv",
                                     fill="tozeroy", name="Bids (buy Up)", line=dict(color=BID_COLOR, width=2)))
        if len(asks):
            fig.add_trace(go.Scatter(x=asks["price"], y=asks["cum_size"], mode="lines", line_shape="hv",
                                     fill="tozeroy", name="Asks (sell Up)", line=dict(color=ASK_COLOR, width=2)))
        fig.update_xaxes(title="Price of Up", tickformat=".2f")
        fig = style(fig, h, "Cumulative contracts")
        fig.update_layout(hovermode="closest")
        chart(fig, f"depth_{asset}")


@st.dialog("BTC · live price & order book", width="large")
def market_window():
    assets = [m["asset"] for m in config.MARKETS]
    asset = st.segmented_control("Round", assets, default=assets[0], format_func=market_of,
                                 key="mw_asset", label_visibility="collapsed") or assets[0]
    st.fragment(run_every=MARKET_REFRESH)(market_panel)(asset)
    st.caption(f"What the bot sees: recorded every ~2s from Polymarket US (refreshes every {MARKET_REFRESH}s). "
               "Close this window to return to the dashboard.")


with st.sidebar.container(border=True):
    st.markdown("**Market**")
    if st.button("BTC chart & order book", icon=":material/candlestick_chart:", width="stretch",
                 help="Opens a window with the live BTC price for the current round (with the price to beat) "
                      "and the round's order book."):
        market_window()


def filtered(d):
    d = dict(d)
    d["strategies"] = d["strategies"][d["strategies"]["name"].isin(picked)]
    for t in ("fills", "settlements", "equity"):
        if len(d[t]):
            d[t] = d[t][d[t]["strategy"].isin(picked)]
    return d


# ---------------------------------------------------------------- live section
def live_section():
    d = filtered(load_data(db, _db_version(db), SINCE))
    if not len(d["strategies"]):
        st.info("No strategies selected.")
        return
    now = time.time()
    last_ts = max([d[t]["ts"].max() for t in ("equity", "snapshots") if len(d[t])], default=None)
    is_live = last_ts is not None and now - last_ts < 120
    anchor = now if is_live or last_ts is None else last_ts     # stopped bot / simulator: its last hour
    start_ts = SINCE if SINCE is not None else anchor - WINDOW      # where the live charts begin
    x0 = pd.Timestamp(start_ts, unit="s", tz="UTC").tz_convert(metrics.TZ)
    x1 = pd.Timestamp(anchor, unit="s", tz="UTC").tz_convert(metrics.TZ)

    # header
    h1, h2 = st.columns([3, 2], vertical_alignment="center")
    h1.markdown("### Polymarket Paper Desk")
    with h2:
        if is_live:
            st.badge(f"Live · data {now - last_ts:.0f}s old", icon=":material/radio_button_checked:", color="green")
        elif last_ts is not None:
            st.badge(f"Not live · last data {x1:%b %d %H:%M} ET", icon=":material/pause_circle:", color="orange")

    summ = metrics.summary(d)
    ops = metrics.open_positions_marked(d)

    # KPI row
    bankroll = d["strategies"]["bankroll"].sum()
    total = summ["Portfolio value"].sum()
    best = summ.sort_values("Total return", ascending=False).iloc[0]
    kpis = st.container(horizontal=True, wrap=True, gap="small")
    W = 190
    kpis.metric("Best strategy", title_of(best["Strategy"]), pct(best["Total return"], signed=True, digits=2),
                border=True, width=W, help="Highest total return so far.")
    kpis.metric("Combined value", money(total), money(total - bankroll, signed=True), border=True, width=W,
                help=f"All selected strategies together, vs. their combined bankroll of {money(bankroll)}.")
    kpis.metric("Realized P&L", money(summ["Realized P&L"].sum(), signed=True), border=True, width=W,
                help="Profit or loss from finished trades only.")
    kpis.metric("Open bets", money(ops["value"].sum() if len(ops) else 0.0),
                money(ops["unrealized"].sum(), signed=True) if len(ops) else None, border=True, width=W,
                help="Current value of bets in rounds that haven't ended (at the mid price); delta = unrealized P&L.")
    kpis.metric("Trades finished", f"{int(summ['Trades'].sum()):,}", border=True, width=W,
                help="Bets whose round has ended and been paid out.")
    kpis.metric("Fees", money(summ["Fees"].sum()), border=True, width=W, help="Polymarket taker fees paid.")
    kpis.metric("Money bet", f"${summ['Money bet'].sum():,.0f}", border=True, width=W,
                help="Dollars spent buying shares (sales not included).")
    n_stop = int((summ["Status"] == "Stopped").sum())
    kpis.metric("Stopped", f"{n_stop} of {len(summ)}", border=True, width=W,
                help=f"Strategies whose portfolio value fell to the stop level "
                     f"(${float(d0['meta'].get('stop_equity', 0) or 0):,.0f}) and no longer trade.")
    if summ["Trades"].sum() < 30:
        st.caption(":material/info: Fewer than 30 finished trades — results are mostly luck so far.")

    # leaderboard with last-hour sparklines
    eq = metrics.equity_curves(d)
    spark = {}
    if len(eq):
        recent = eq[eq["ts"] >= start_ts]
        spark = {n: g.sort_values("ts")["return_pct"].round(3).tolist() for n, g in recent.groupby("strategy")}
    lb = summ.assign(Trend=summ["Strategy"].map(lambda n: spark.get(n, [])),
                     Strategy=summ["Strategy"].map(title_of)).sort_values("Total return", ascending=False)
    cols = ["Strategy", "Status", "Trend", "Portfolio value", "Total return", "Realized P&L", "Trades", "Win rate",
            "Avg P&L per trade", "Luck check (t-stat)", "Worst drop", "Profit per $1 bet", "Avg price paid",
            "Fees", "Fee %", "Fee theta", "Rebates", "Money bet", "Volume traded"]
    cc = st.column_config
    table(lb[cols], {
        "Portfolio value": money, "Total return": lambda x: pct(x, signed=True, digits=2),
        "Realized P&L": SIGNED_MONEY, "Trades": "{:,.0f}", "Win rate": pct, "Avg P&L per trade": SIGNED_MONEY,
        "Luck check (t-stat)": "{:.2f}", "Worst drop": lambda x: pct(x, digits=2), "Profit per $1 bet": "{:+.3f}",
        "Avg price paid": "{:.3f}", "Fees": money, "Money bet": "${:,.0f}", "Volume traded": "${:,.0f}",
        "Fee theta": "{:.4f}", "Rebates": money, "Fee %": lambda x: pct(x, digits=1),
    }, column_config={
        "Fee %": cc.Column("Fee %", help="Fees ÷ dollars traded (buys and sells). Polymarket US charges "
                                         "Θ × (1 − price) per dollar paid: ~9% on a 5¢ contract, ~1% at 90¢, "
                                         "and round trips (buy then sell) pay it twice."),
        "Status": cc.Column("Status", help="Stopped = portfolio value hit the stop level; it no longer trades."),
        "Fee theta": cc.Column("Fee Θ", help="Taker fee coefficient on the latest trade (Polymarket US: by 30-day volume tier)."),
        "Rebates": cc.Column("Rebates", help="Taker rebates earned (Polymarket US: from $250k of 30-day volume)."),
        "Trend": cc.LineChartColumn("Since chart start" if SINCE else "Last hour",
                                    help="Return % over the charted period.", width="small"),
        "Portfolio value": cc.Column("Value", help="Cash plus open bets at the mid price."),
        "Total return": cc.Column("Return", help="Versus the starting bankroll."),
        "Realized P&L": cc.Column("Realized", help="Finished trades only."),
        "Trades": cc.Column("Trades", help="Finished (paid-out) trades."),
        "Win rate": cc.Column("Win %", help="Share of finished trades with a profit. Read it with Avg price."),
        "Avg P&L per trade": cc.Column("Avg P&L"),
        "Luck check (t-stat)": cc.Column("t-stat", help="Under ~2 = can't rule out luck."),
        "Worst drop": cc.Column("Max DD", help="Worst drop from a previous high (all time)."),
        "Profit per $1 bet": cc.Column("P&L / $1", help="Realized P&L per dollar bought."),
        "Avg price paid": cc.Column("Avg price", help="Average price paid per share."),
        "Fees": cc.Column("Fees"), "Money bet": cc.Column("Bought", help="Dollars spent buying."),
        "Volume traded": cc.Column("Volume", help="Polymarket US volume: contracts × price of the side received."
                                   if EXCHANGE == "polymarket_us" else "Bought plus sold."),
    })

    # last-hour charts (fixed 60-minute x-axis)
    st.markdown("##### Since chart restart" if SINCE is not None else "##### Last hour")
    c1, c2 = st.columns([3, 2])
    with c1:
        if len(eq):
            e = eq[eq["ts"] >= start_ts].copy()
            y_lbl = "Return %"
            if SINCE is not None and len(e):     # fresh start: change since the restart point
                first = e.sort_values("ts").groupby("strategy")["equity"].transform("first")
                e["return_pct"] = (e["equity"] / first - 1) * 100
                y_lbl = "Change since chart restart %"
            e = relabel(e)
            fig = px.line(e, x="time", y="return_pct", color="strategy", color_discrete_map=color_map,
                          labels={"return_pct": y_lbl, "strategy": "Strategy", "time": "Time (ET)"})
            fig.add_hline(y=0, line_dash="dot", line_color=MUTED, line_width=1)
            fig.update_xaxes(range=[x0, x1])
            chart(style(fig, 300, y_lbl), "eq")
        else:
            st.caption("Equity appears after the bot has run for a few seconds.")
    with c2:
        tl = metrics.realized_timeline(d)
        fig = go.Figure()
        for name in picked:
            s = tl[tl.strategy == name] if len(tl) else tl
            before = s[s.ts < start_ts]
            base = before["cum_pnl"].iloc[-1] if len(before) else 0.0
            inside = s[s.ts >= start_ts]
            offset = base if SINCE is not None else 0.0     # after a restart: P&L since the restart
            start = base - offset
            xs = [x0] + inside["time"].tolist() + [x1]
            ys = [start] + (inside["cum_pnl"] - offset).tolist() + [
                (inside["cum_pnl"].iloc[-1] - offset) if len(inside) else start]
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line_shape="hv", name=title_of(name),
                                     line=dict(color=color_map.get(title_of(name)))))
        fig.add_hline(y=0, line_dash="dot", line_color=MUTED, line_width=1)
        fig.update_xaxes(range=[x0, x1])
        fig.update_yaxes(tickprefix="$")
        chart(style(fig, 300, "Realized P&L since restart" if SINCE is not None else "Realized P&L (all time)",
                    legend=False), "realized")

    c1, c2 = st.columns([3, 2])
    with c1:
        snaps = d["snapshots"]
        snaps = snaps[snaps["ts"] >= start_ts] if len(snaps) else snaps
        if len(snaps):
            s = snaps.copy()
            s["up_mid"] = s[["up_bid", "up_ask"]].mean(axis=1)   # one side missing -> the other
            s["Market"] = s["asset"].map(market_of)
            # markets aren't strategies, so they stay neutral gray (dash style tells them apart)
            fig = px.line(s, x="time", y="up_mid", line_dash="Market", color_discrete_sequence=[MUTED],
                          labels={"up_mid": "Price of Up", "time": "Time (ET)"})
            fig.add_hline(y=0.5, line_dash="dot", line_color=MUTED, line_width=1)
            fig.update_xaxes(range=[x0, x1])
            fig.update_yaxes(range=[0, 1])
            chart(style(fig, 260, "Price of 'Up'"), "market")
        else:
            st.caption("No market data in the last hour.")
    with c2:
        if len(snaps):
            latest = snaps.sort_values("ts").groupby("asset").tail(1)
            for c in ("price_to_beat", "index_price"):
                if c not in latest:
                    latest = latest.assign(**{c: float("nan")})
            q = latest.assign(Market=latest["asset"].map(market_of))[[
                "Market", "seconds_left", "up_bid", "up_ask", "down_bid", "down_ask", "up_ask_depth", "down_ask_depth",
                "price_to_beat", "index_price"]]
            px3 = "{:.3f}"
            table(q, {"seconds_left": "{:.0f}", "up_bid": px3, "up_ask": px3, "down_bid": px3, "down_ask": px3,
                      "up_ask_depth": "${:,.0f}", "down_ask_depth": "${:,.0f}",
                      "price_to_beat": "${:,.2f}", "index_price": "${:,.2f}"}, column_config={
                "price_to_beat": cc.Column("Price to beat", help="The round's open value (Polymarket US). Up wins if the close is at or above it."),
                "index_price": cc.Column("BTC index", help="CF Benchmarks BRTI, the index Polymarket US settles on."),
                "seconds_left": "Secs left", "up_bid": "Up bid", "up_ask": "Up ask", "down_bid": "Down bid",
                "down_ask": "Down ask",
                "up_ask_depth": cc.Column("Up depth", help="Dollars on offer to buy Up."),
                "down_ask_depth": cc.Column("Down depth", help="Dollars on offer to buy Down.")})

    # open bets (live)
    st.markdown("##### Open bets")
    if len(ops):
        o = ops.assign(Strategy=ops["strategy"].map(title_of), Market=ops["asset"].map(market_of),
                       Side=ops["outcome"].str.title())
        table(o[["Strategy", "Market", "Side", "shares", "net_cost", "mark", "value", "unrealized", "seconds_left"]],
              {"shares": "{:.1f}", "net_cost": money, "mark": "{:.3f}", "value": money, "unrealized": SIGNED_MONEY,
               "seconds_left": "{:.0f}"}, column_config={
                  "shares": "Shares", "net_cost": cc.Column("Cost", help="Net money put in, fees included."),
                  "mark": cc.Column("Mark", help="Current mid price of that side."), "value": "Value",
                  "unrealized": "Unrealized", "seconds_left": "Secs left"})
    else:
        st.caption("No open bets.")
    return d


st.fragment(run_every=LIVE_SECONDS if live else None)(live_section)()


# ---------------------------------------------------------------- strategy lab (Part C comparison)
@st.cache_data(ttl=60, show_spinner=False, max_entries=4)
def load_lab(path, version):
    return analytics.load_lab(path)


@st.cache_data(ttl=600, show_spinner=False, max_entries=4)
def load_tick_calibration(path, ten_minutes):
    return analytics.model_calibration_ticks(path)


def strategy_lab():
    lab = load_lab(db, _db_version(db))
    bankrolls = dict(zip(d0["strategies"]["name"], d0["strategies"]["bankroll"]))
    sc = analytics.scorecard(lab, picked, max_dd=load_max_dd(db, int(time.time() // 60)), bankrolls=bankrolls)
    st.markdown("##### Strategy lab")
    st.caption("Same live data, separate bankrolls. Edge = model fair value − (price + fee per share). "
               "Realized edge = (1 if the side won) − (price + fee per share). Breakeven = the hit rate needed "
               "to break even holding to the result.")
    if len(sc):
        show = sc.assign(strategy=sc["strategy"].map(title_of))
        cc = st.column_config
        table(show[["strategy", "pnl_after_fees", "pnl_before_fees", "trades", "rounds", "win_rate", "hit_rate",
                    "breakeven_win_rate", "avg_entry", "avg_edge_entry", "avg_realized_edge", "sharpe", "t_stat",
                    "max_dd_pct", "fees", "maker_share"]],
              {"pnl_after_fees": SIGNED_MONEY, "pnl_before_fees": SIGNED_MONEY, "trades": "{:,.0f}",
               "rounds": "{:,.0f}", "win_rate": lambda x: pct(x * 100), "hit_rate": lambda x: pct(x * 100),
               "breakeven_win_rate": lambda x: pct(x * 100), "avg_entry": "{:.3f}", "avg_edge_entry": "{:+.3f}",
               "avg_realized_edge": "{:+.3f}", "sharpe": "{:+.2f}", "t_stat": "{:+.2f}",
               "max_dd_pct": lambda x: pct(x, digits=2), "fees": money, "maker_share": lambda x: pct(x * 100, digits=0)},
              column_config={
                  "strategy": "Strategy", "pnl_after_fees": cc.Column("PnL after fees", help="Settled rounds."),
                  "pnl_before_fees": cc.Column("PnL before fees"), "trades": cc.Column("Trades", help="Entries (BUY fills)."),
                  "rounds": cc.Column("Rounds", help="Settled rounds with money in."),
                  "win_rate": cc.Column("Win %", help="Rounds with positive P&L."),
                  "hit_rate": cc.Column("Hit %", help="Share of shares bought whose side won."),
                  "breakeven_win_rate": cc.Column("Breakeven %", help="Avg price + fee per share paid: the hit rate needed to break even."),
                  "avg_entry": "Avg entry", "avg_edge_entry": cc.Column("Edge at entry", help="Model fair value − (fill price + fee)."),
                  "avg_realized_edge": cc.Column("Realized edge", help="Outcome (1/0) − (fill price + fee), per share."),
                  "sharpe": cc.Column("Sharpe/trade", help="Mean ÷ std of round P&L (not annualized)."),
                  "t_stat": cc.Column("t-stat", help="Under ~2 = can't rule out luck."),
                  "max_dd_pct": "Max DD", "fees": "Fees", "maker_share": cc.Column("Maker %", help="Share of fills that were maker fills.")})
    # ---- 5m vs 15m: the leaderboard above combines both round lengths; this splits them
    st.markdown("##### 5-minute vs 15-minute rounds")
    st.caption("Everything above combines 5m and 15m. Here each strategy is scored separately per round length "
               "(max drawdown isn't split because each strategy has one bankroll).")
    by_kind = analytics.scorecard_by_kind(lab, picked, bankrolls=bankrolls)
    if len(by_kind):
        view = st.segmented_control("Show", ["Both", "5m", "15m"], default="Both", key="kind_view",
                                    label_visibility="collapsed") or "Both"
        bk_show = by_kind if view == "Both" else by_kind[by_kind["kind"] == view]
        bk_show = bk_show.assign(strategy=bk_show["strategy"].map(title_of)).sort_values(
            ["strategy", "kind"] if view == "Both" else ["pnl_after_fees"], ascending=[True, True] if view == "Both" else [False])
        table(bk_show[["strategy", "kind", "pnl_after_fees", "pnl_before_fees", "trades", "rounds", "win_rate",
                       "breakeven_win_rate", "avg_entry", "t_stat", "fees"]],
              {"pnl_after_fees": SIGNED_MONEY, "pnl_before_fees": SIGNED_MONEY, "trades": "{:,.0f}",
               "rounds": "{:,.0f}", "win_rate": lambda x: pct(x * 100), "breakeven_win_rate": lambda x: pct(x * 100),
               "avg_entry": "{:.3f}", "t_stat": "{:+.2f}", "fees": money},
              column_config={"strategy": "Strategy", "kind": "Market", "pnl_after_fees": "PnL after fees",
                             "pnl_before_fees": "PnL before fees", "trades": "Trades", "rounds": "Rounds",
                             "win_rate": "Win %", "breakeven_win_rate": "Breakeven %", "avg_entry": "Avg entry",
                             "t_stat": "t-stat", "fees": "Fees"})
    else:
        st.caption("No trades yet.")

    c1, c2 = st.columns(2)
    with c1:
        st.caption("Model calibration: fair value of the side traded vs. how often it won. "
                   "On the dotted diagonal = the model is right. Gray = every logged tick (Up).")
        cal = analytics.model_calibration({**lab, "fills": lab["fills"][lab["fills"].strategy.isin(picked + ["All trades"])]
                                           if len(lab["fills"]) else lab["fills"]})
        ticks = load_tick_calibration(db, int(time.time() // 600))
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(dash="dot", color=MUTED, width=1),
                                 showlegend=False, hoverinfo="skip"))
        if len(ticks):
            fig.add_trace(go.Scatter(x=ticks["avg_fair"], y=ticks["win_rate"], mode="lines+markers", name="All ticks (model)",
                                     line=dict(color=MUTED, width=1.5), customdata=ticks["ticks"],
                                     hovertemplate="fair %{x:.2f} → won %{y:.2f} (%{customdata} ticks)<extra></extra>"))
        if len(cal):
            for name, g in cal.groupby("strategy"):
                label = name if name == "All trades" else title_of(name)
                fig.add_trace(go.Scatter(x=g["avg_fair"], y=g["win_rate"], mode="markers", name=label,
                                         marker=dict(size=np.clip(np.sqrt(g["trades"]) * 4, 6, 22),
                                                     color=color_map.get(label, PALETTE[0] if name == "All trades" else None)),
                                         customdata=np.stack([g["label"], g["trades"]], axis=1),
                                         hovertemplate="%{customdata[0]}: won %{y:.2f} (%{customdata[1]} trades)<extra>" + label + "</extra>"))
        fig.update_xaxes(range=[-0.02, 1.02], title_text="Model fair value")
        fig.update_yaxes(range=[-0.05, 1.05])
        if len(cal) or len(ticks):
            chart(style(fig, 320, "Actual win rate").update_layout(hovermode="closest"), "model_cal")
            if len(cal):
                tab = cal[cal.strategy == "All trades"][["label", "trades", "avg_fair", "win_rate"]]
                table(tab, {"trades": "{:,.0f}", "avg_fair": "{:.3f}", "win_rate": "{:.3f}"},
                      column_config={"label": "Fair value bucket", "trades": "Trades", "avg_fair": "Avg fair",
                                     "win_rate": "Actual win rate"})
        else:
            st.caption("Needs settled trades (or ticks) with a model fair value.")
    with c2:
        st.caption("PnL by market (5m vs 15m) and by time left at entry")
        bk = analytics.pnl_by_kind(lab)
        bk = bk[bk.strategy.isin(picked)] if len(bk) else bk
        if len(bk):
            fig = px.bar(relabel(bk), x="strategy", y="pnl", color="kind", barmode="group",
                         labels={"pnl": "P&L ($)", "strategy": "", "kind": "Market"},
                         color_discrete_sequence=[PALETTE[0], PALETTE[1], PALETTE[2]])
            fig.update_traces(marker_line_width=0, marker_cornerradius=4)
            chart(style(fig, 220, "P&L ($)").update_layout(hovermode="closest"), "pnl_kind")
        bt = analytics.pnl_by_time_left(lab)
        bt = bt[bt.strategy.isin(picked)] if len(bt) else bt
        if len(bt):
            order = [b for b in analytics.TIME_LEFT_LABELS if b in set(bt["bucket"])]
            fig = px.bar(relabel(bt), x="bucket", y="pnl", color="strategy", barmode="group",
                         category_orders={"bucket": order}, color_discrete_map=color_map,
                         labels={"pnl": "P&L ($)", "bucket": "Time left at entry", "strategy": "Strategy"})
            fig.update_traces(marker_line_width=0, marker_cornerradius=4)
            chart(style(fig, 220, "P&L ($)", legend=False).update_layout(hovermode="closest"), "pnl_tleft")
        if not len(bk) and not len(bt):
            st.caption("No settled rounds yet.")
    adv = analytics.adverse_selection(lab)
    adv = adv[adv.strategy.isin(picked)] if len(adv) else adv
    ev = lab["events"]
    ev = ev[ev.strategy.isin(picked)] if len(ev) else ev
    c1, c2 = st.columns(2)
    with c1:
        st.caption("Maker adverse selection: fair-value move in the 5s after each maker fill (negative = picked off)")
        if len(adv):
            table(relabel(adv), {"maker_fills": "{:,.0f}", "avg_fv_move": "{:+.4f}", "adverse_share": lambda x: pct(x * 100)},
                  column_config={"strategy": "Strategy", "maker_fills": "Maker fills", "avg_fv_move": "Avg FV move",
                                 "adverse_share": "Share adverse"})
        else:
            st.caption("No maker fills yet.")
    with c2:
        st.caption("Diagnostics (near-misses, skipped signals, risk rejections, failed legs)")
        if len(ev):
            table(relabel(ev).sort_values(["strategy", "n"], ascending=[True, False])[["strategy", "kind", "n"]],
                  {"n": "{:,.0f}"}, column_config={"strategy": "Strategy", "kind": "Event", "n": "Count"})
        else:
            st.caption("No events yet.")


strategy_lab()

# ---------------------------------------------------------------- all-time analysis
d = filtered(d0)
if not len(d["strategies"]):
    st.stop()
dc = dict(d)          # chart data: only what happened after the chart restart (if one was pressed)
if SINCE is not None:
    for t in ("fills", "settlements"):
        if len(dc[t]):
            dc[t] = dc[t][dc[t]["ts"] >= SINCE]
st.markdown(f"##### Since chart restart" if SINCE is not None else "##### All time")
c1, c2, c3 = st.columns(3)
with c1:
    st.caption("P&L per finished trade")
    if len(dc["settlements"]):
        fig = px.strip(relabel(dc["settlements"]), x="strategy", y="pnl", color="strategy",
                       color_discrete_map=color_map, labels={"pnl": "P&L ($)", "strategy": ""})
        fig.add_hline(y=0, line_dash="dot", line_color=MUTED, line_width=1)
        fig.update_traces(marker=dict(size=8, opacity=0.6))
        fig.update_layout(hovermode="closest")
        chart(style(fig, 300, "P&L ($)", legend=False).update_layout(hovermode="closest"), "dist")
    else:
        st.caption("No finished trades yet.")
with c2:
    st.caption("Price paid vs. win rate (held to result). Dotted = fair price; solid = break-even after "
               "taker fees. Only dots above the solid line made money.")
    cal = metrics.calibration(dc)
    if len(cal):
        fig = px.scatter(relabel(cal), x="avg_price", y="win_rate", size="fills", color="strategy",
                         color_discrete_map=color_map, size_max=18,
                         labels={"avg_price": "Avg price paid", "win_rate": "Win rate", "fills": "Trades",
                                 "strategy": "Strategy"})
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(dash="dot", color=MUTED, width=1),
                                 name="fair price", showlegend=False, hoverinfo="skip"))
        for i, (theta, label) in enumerate(FEE_LINES):   # what you need to win once the taker fee is paid
            be = metrics.break_even_curve(theta)
            fig.add_trace(go.Scatter(x=be["price"], y=be["win_rate"], mode="lines",
                                     line=dict(color=MUTED, width=1.5, dash="solid" if i == 0 else "dash"),
                                     name=f"break-even, {label}", showlegend=False,
                                     hovertemplate=f"break-even {label}: %{{y:.3f}}<extra></extra>"))
        fig.update_xaxes(range=[-0.02, 1.02])
        fig.update_yaxes(range=[-0.05, 1.08])   # room so bubbles at 0% / 100% aren't clipped
        chart(style(fig, 300, "Win rate", legend=False).update_layout(hovermode="closest"), "cal")
    else:
        st.caption("Needs finished trades held to the result.")
with c3:
    st.caption("Realized P&L by hour of day (ET)")
    hr = metrics.pnl_by_hour(dc)
    if len(hr):
        fig = px.bar(relabel(hr), x="hour_et", y="pnl", color="strategy", barmode="group",
                     color_discrete_map=color_map, labels={"hour_et": "Hour (ET)", "pnl": "P&L ($)",
                                                           "strategy": "Strategy"})
        fig.update_traces(marker_line_width=0, marker_cornerradius=4)
        fig.update_layout(bargap=0.25, bargroupgap=0.1)
        fig.update_xaxes(dtick=2)
        chart(style(fig, 300, "P&L ($)", legend=False).update_layout(hovermode="closest"), "hour")
    else:
        st.caption("No finished trades yet.")

# ---------------------------------------------------------------- drill-down
st.markdown("##### Strategy detail")
who = st.selectbox("Strategy", picked, format_func=title_of, label_visibility="collapsed")
cc = st.column_config
t1, t2, t3, t4 = st.tabs(["Round trips", "Finished trades", "Fills", "Settings"])
with t1:
    rt = metrics.round_trips({"fills": d["fills"][d["fills"].strategy == who] if len(d["fills"]) else d["fills"],
                              "settlements": d["settlements"]})
    if len(rt):
        done = rt[rt.exit_type != "Open"]
        a, b, c, e = st.columns(4)
        a.metric("Round trips", f"{len(done):,}", border=True)
        b.metric("Win rate", pct((done.pnl > 0).mean() * 100) if len(done) else "—", border=True)
        c.metric("Avg P&L", money(done.pnl.mean(), signed=True) if len(done) else "—", border=True)
        e.metric("Avg hold", f"{done.hold_s.mean():.0f}s" if len(done) else "—", border=True)
        left, right = st.columns([1, 2])
        with left:
            if len(done):
                by = done.groupby("exit_type").agg(trips=("pnl", "size"), total=("pnl", "sum"),
                                                   avg=("pnl", "mean")).reset_index()
                table(by, {"trips": "{:,.0f}", "total": SIGNED_MONEY, "avg": SIGNED_MONEY},
                      column_config={"exit_type": "Exit", "trips": "Trips", "total": "Total", "avg": "Avg"})
            labels = [f"{r.entry_time:%b %d %H:%M:%S} · {r.outcome.title()} · "
                      f"{('open' if r.exit_type == 'Open' else money(r.pnl, signed=True))}" for r in rt.itertuples()]
            pick = st.selectbox("Price path for", range(len(rt)), format_func=lambda i: labels[i], key="rt_pick")
        with right:
            r = rt.iloc[pick]
            t_end = r.exit_ts if r.exit_ts == r.exit_ts else time.time()
            path = metrics.price_path(db, r.slug, r.entry_ts, t_end)
            if len(path):
                fig = go.Figure()
                if r.outcome == "BOTH":   # a hedge: show the sell price of both sides
                    for k, name, color in (("up", "Up bid", PALETTE[0]), ("down", "Down bid", PALETTE[1])):
                        fig.add_trace(go.Scatter(x=path["time"], y=path[f"{k}_bid"], name=name, mode="lines",
                                                 line=dict(color=color)))
                else:
                    k = "up" if r.outcome == "UP" else "down"
                    fig.add_trace(go.Scatter(x=path["time"], y=path[f"{k}_ask"], name="Ask (buy)", mode="lines",
                                             line=dict(color=PALETTE[1])))
                    fig.add_trace(go.Scatter(x=path["time"], y=path[f"{k}_bid"], name="Bid (sell)", mode="lines",
                                             line=dict(color=PALETTE[0])))
                    fig.add_trace(go.Scatter(x=[r.entry_time], y=[r.entry_price], name="Entry", mode="markers",
                                             marker=dict(size=12, symbol="triangle-up", color=PALETTE[2],
                                                         line=dict(width=2, color="white"))))
                    if r.exit_type != "Open":
                        fig.add_trace(go.Scatter(x=[r.exit_time], y=[r.exit_price], name=r.exit_type,
                                                 mode="markers", marker=dict(size=12, symbol="x", color=MUTED)))
                fig.update_yaxes(range=[0, 1.02])
                chart(style(fig, 280, f"Price of {r.outcome.title()}"), "path")
            else:
                st.caption("No price data recorded for this period.")
        show = rt.assign(Market=rt["asset"].map(market_of), outcome=rt["outcome"].str.title())
        table(show[["entry_time", "exit_time", "Market", "outcome", "entry_price", "exit_price", "shares",
                    "hold_s", "exit_type", "fees", "pnl", "return_pct"]],
              {"entry_time": TS, "exit_time": TS, "entry_price": "{:.3f}", "exit_price": "{:.3f}",
               "shares": "{:.1f}", "hold_s": "{:.0f}", "fees": money, "pnl": SIGNED_MONEY,
               "return_pct": lambda x: pct(x, signed=True)},
              column_config={"entry_time": "Entered (ET)", "exit_time": "Exited (ET)",
                             "outcome": cc.Column("Side", help="BOTH = bought both sides; prices are per pair."),
                             "entry_price": "Entry", "exit_price": "Exit", "shares": "Shares", "hold_s": "Held (s)",
                             "exit_type": "Exit", "fees": "Fees", "pnl": "P&L", "return_pct": "Return"})
    else:
        st.caption("No round trips yet.")
with t2:
    s = d["settlements"]
    s = s[s.strategy == who] if len(s) else s
    if len(s):
        s = s.sort_values("ts", ascending=False).assign(Market=lambda x: x["asset"].map(market_of),
                                                         winner=lambda x: x["winner"].str.title())
        table(s[["time", "Market", "winner", "up_shares", "down_shares", "net_cost", "payout", "pnl"]],
              {"time": TS, "up_shares": "{:.1f}", "down_shares": "{:.1f}", "net_cost": money,
               "payout": money, "pnl": SIGNED_MONEY},
              column_config={"time": "Time (ET)", "winner": "Result", "up_shares": "Up sh", "down_shares": "Down sh",
                             "net_cost": "Cost", "payout": "Payout", "pnl": "P&L"})
    else:
        st.caption("No finished trades yet.")
with t3:
    f = d["fills"]
    f = f[f.strategy == who] if len(f) else f
    if len(f):
        f = f.sort_values("ts", ascending=False).assign(
            Market=lambda x: x["asset"].map(market_of), outcome=lambda x: x["outcome"].str.title(),
            side=lambda x: x["side"].map({"BUY": "Buy", "SELL": "Sell"}))
        table(f[["time", "Market", "side", "outcome", "shares", "avg_price", "usd", "fee", "reason"]],
              {"time": TS, "shares": "{:.1f}", "avg_price": "{:.3f}", "usd": money, "fee": "${:.3f}"},
              column_config={"time": "Time (ET)", "side": "Action", "outcome": "Side", "shares": "Shares",
                             "avg_price": "Price", "usd": "Amount", "fee": "Fee", "reason": "Why"})
    else:
        st.caption("No fills yet.")
with t4:
    row = d["strategies"].set_index("name").loc[who]
    stat = d0["status"]
    stop_row = stat[stat["name"] == who] if len(stat) else stat
    if len(stop_row):
        r0 = stop_row.iloc[0]
        st.warning(f"Stopped trading at {pd.Timestamp(r0['stopped_at'], unit='s', tz='UTC').tz_convert(metrics.TZ):%b %d %H:%M:%S} ET: "
                   f"{r0['reason']}. Open bets still settle.")
    st.json({"Exchange": "Polymarket US" if EXCHANGE == "polymarket_us" else "polymarket.com",
             "Type": row["class"], "Starting bankroll ($)": row["bankroll"],
             "Stop level ($)": float(d0["meta"].get("stop_equity", 0) or 0),
             "Markets": json.loads(row["assets"]) if isinstance(row["assets"], str) else row["assets"],
             "Settings": json.loads(row["params"]) if isinstance(row["params"], str) else row["params"]})
    if DETAILS.get(who):
        st.markdown(DETAILS[who])
