"""
vrp-agent dashboard generator.

Renders:
  - PNG chart: 7-day ETH price + VRP with horizontal levels + trade markers
  - HTML index.html: current state, recent trades, cumulative PnL, agent balance
  - SQLite is read directly; Deribit data is fetched fresh.

Run periodically via systemd timer (every 5 min).

Output: /opt/vrp-agent/out/{index.html, chart.png}
Serve: python3 -m http.server --directory /opt/vrp-agent/out 8082
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import requests

from . import config
from . import deribit_feed
from . import signal_engine as sig
from . import state_machine as sm
from . import transitions
from .hl_executor import HLExecutor
from .on_chain.client import BaseClient
from .on_chain.lp_manager import LPManager
from .pnl_tracker import PnLTracker

log = logging.getLogger("dashboard")

OUT_DIR = config.ROOT / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DISPLAY_HOURS = 7 * 24      # show last 7 days
FETCH_HOURS   = 10 * 24     # fetch 10d so 72h Parkinson is warm

WIN_SHORT = 6
WIN_LONG  = 72


def parkinson(H, L, win):
    c = 1.0 / (4.0 * np.log(2.0))
    ann = 365.0 * 24.0
    x = np.log(H / L).replace([np.inf, -np.inf], np.nan).pow(2)
    return np.sqrt(c * x.rolling(win, min_periods=win).mean()) * np.sqrt(ann) * 100.0


def fetch_signal_df():
    df = deribit_feed.fetch_combined("ETH", hours=FETCH_HOURS)
    df["R_short"]  = parkinson(df["high"], df["low"], WIN_SHORT)
    df["R_long"]   = parkinson(df["high"], df["low"], WIN_LONG)
    df["VRP"]      = df["dvol"] - df["R_long"]
    df["Momentum"] = df["R_short"] - df["R_long"]
    cutoff = df["datetime"].max() - pd.Timedelta(hours=DISPLAY_HOURS)
    return df[df["datetime"] >= cutoff].reset_index(drop=True)


def render_chart(df, trades_df, out_path):
    fig, axes = plt.subplots(2, 1, figsize=(14, 7), sharex=True,
                              gridspec_kw={"height_ratios": [3, 2]})

    # ─── Price panel ───────────────────────────────────────────
    ax = axes[0]
    ax.plot(df["datetime"], df["close"], lw=0.9, color="#1f3a5f")
    ax.fill_between(df["datetime"], df["low"], df["high"],
                     alpha=0.15, color="#3498db")
    # mark trade entries / exits
    for _, t in trades_df.iterrows():
        if t["entry_ts"]:
            try:
                ts = pd.Timestamp(t["entry_ts"])
                if df["datetime"].min() <= ts:
                    ax.scatter(ts, t["entry_price"], marker="^", s=80,
                                color="green", zorder=5, edgecolors="black", linewidth=0.5)
            except Exception:
                pass
        if t["exit_ts"]:
            try:
                ts = pd.Timestamp(t["exit_ts"])
                if df["datetime"].min() <= ts:
                    ax.scatter(ts, t["exit_price"], marker="v", s=80,
                                color="red", zorder=5, edgecolors="black", linewidth=0.5)
            except Exception:
                pass
    ax.set_ylabel("ETH price ($)", fontsize=10)
    ax.set_title(f"ETH 7d • realised vol estimator (short / long windows) • "
                  f"green ▲ entries, red ▼ exits", fontsize=11)
    ax.grid(True, alpha=0.35, linestyle="-", linewidth=0.4)

    # ─── VRP panel with levels ─────────────────────────────────
    ax = axes[1]
    ax.plot(df["datetime"], df["VRP"], lw=1.0, color="#16a085")
    ax.fill_between(df["datetime"], 0, df["VRP"], where=df["VRP"] > 0,
                     color="#16a085", alpha=0.18, interpolate=True)
    ax.fill_between(df["datetime"], 0, df["VRP"], where=df["VRP"] < 0,
                     color="#c0392b", alpha=0.18, interpolate=True)

    levels = [
        ( 0,                              "black",   "-",  1.4, "0"),
        (config.STRAT.vrp_exit_level,     "#c0392b", "--", 1.0, "exit threshold"),
        (config.STRAT.vrp_entry_level,    "#27ae60", "--", 1.0, "entry threshold"),
    ]
    for y, color, ls, lw, lbl in levels:
        ax.axhline(y, color=color, linestyle=ls, linewidth=lw, alpha=0.7)
        ax.text(df["datetime"].iloc[-1], y, f"  {lbl}",
                 color=color, fontsize=8, va="center", ha="left", alpha=0.85)

    ax.set_ylabel("VRP (vol pts)", fontsize=10)
    ax.grid(True, alpha=0.35, linestyle="-", linewidth=0.4)
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    ax.yaxis.set_minor_locator(mticker.MultipleLocator(5))
    ax.grid(which="minor", alpha=0.15, linestyle=":", linewidth=0.3)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        ax.tick_params(axis="x", rotation=30, labelsize=8)

    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close()


def collect_state(tracker, base, lp, hl):
    snap = transitions.snapshot(base, lp, hl, tracker)
    state = sm.reconcile(snap)

    # PnL summary
    pnl = tracker.summary()
    daily = tracker.daily_loss()

    # Active LP value (best-effort: from initial_amount0/1)
    active = tracker.active_lp() or {}

    return {
        "state":           state.value,
        "base_usdc":       snap.base_usdc / 1e6,
        "base_eurc":       snap.base_eurc / 1e6,
        "base_eth":        snap.base_eth_wei / 1e18,
        "lp_token_id":     snap.lp_token_id,
        "lp_liquidity":    snap.lp_liquidity,
        "hl_perp_size":    snap.hl_perp_size,
        "hl_perp_entry":   snap.hl_perp_entry,
        "hl_margin":       snap.hl_margin_usd,
        "active_lp_pool":  active.get("pool_label"),
        "active_lp_initial_amount0": active.get("initial_amount0", 0),
        "active_lp_initial_amount1": active.get("initial_amount1", 0),
        "closed_trades":   pnl["closed_trades"],
        "total_pnl_usd":   pnl["total_pnl_usd"],
        "win_rate":        pnl["win_rate"],
        "daily_pnl":       daily,
    }


def fetch_api_stats(url: str = "http://127.0.0.1:8090/stats") -> dict | None:
    """Pull the ARMS Signals counter (paid calls + probes per endpoint)."""
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"  arms-signals /stats fetch failed: {e}")
    return None


def fetch_github_stats(repos: list[str]) -> dict:
    """Fetch GitHub adoption metrics for given owner/repo strings.

    Stars/forks/watchers via public API. Clones/views require a PAT with
    `repo` scope in the GH_TOKEN env var.
    """
    out = {}
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for repo in repos:
        stats = {"stars": 0, "forks": 0, "watchers": 0,
                 "clones_14d": 0, "unique_clones_14d": 0,
                 "views_14d": 0, "unique_views_14d": 0,
                 "auth": bool(token)}
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}",
                             headers=headers, timeout=10)
            if r.status_code == 200:
                d = r.json()
                stats["stars"]    = int(d.get("stargazers_count", 0))
                stats["forks"]    = int(d.get("forks_count", 0))
                stats["watchers"] = int(d.get("subscribers_count", 0))
        except Exception as e:
            log.warning(f"  github repo fetch failed for {repo}: {e}")

        if token:
            try:
                r = requests.get(f"https://api.github.com/repos/{repo}/traffic/clones",
                                 headers=headers, timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    stats["clones_14d"]        = int(d.get("count", 0))
                    stats["unique_clones_14d"] = int(d.get("uniques", 0))
                r = requests.get(f"https://api.github.com/repos/{repo}/traffic/views",
                                 headers=headers, timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    stats["views_14d"]        = int(d.get("count", 0))
                    stats["unique_views_14d"] = int(d.get("uniques", 0))
            except Exception as e:
                log.warning(f"  github traffic fetch failed for {repo}: {e}")

        out[repo] = stats
    return out


def render_html(state_info, signal_snap, trades_df, chart_filename, out_path,
                 github_stats: dict | None = None,
                 api_stats: dict | None = None):
    rows = []
    for _, t in trades_df.head(20).iterrows():
        pnl_str = f"{t['pnl_pct']:+.3f}%" if t['pnl_pct'] is not None else "—"
        pnl_color = "green" if t['pnl_pct'] and t['pnl_pct'] > 0 else \
                    ("red" if t['pnl_pct'] and t['pnl_pct'] < 0 else "#888")
        rows.append(f"""
        <tr>
          <td>{t['id']}</td>
          <td>{t['entry_ts'] or ''}</td>
          <td>{f"${t['entry_price']:.2f}" if t['entry_price'] else ''}</td>
          <td>{f"{t['entry_size']:.4f}" if t['entry_size'] else ''}</td>
          <td>{t['exit_ts'] or '(open)'}</td>
          <td>{f"${t['exit_price']:.2f}" if t['exit_price'] else ''}</td>
          <td style="color: {pnl_color}; font-weight: bold;">{pnl_str}</td>
          <td>{t['exit_reason'] or ''}</td>
        </tr>""")
    trades_html = "\n".join(rows) if rows else (
        "<tr><td colspan='8' style='text-align:center;color:#888'>"
        "no trades yet</td></tr>"
    )

    state_color = {
        "PARKED_IN_LP": "#16a085",
        "LONG_ON_HL":   "#e74c3c",
        "CASH_ON_HL":   "#95a5a6",
        "UNKNOWN":      "#7f8c8d",
    }.get(state_info["state"], "#000")

    # Adoption panel from GitHub stats
    adoption_html = ""
    if github_stats:
        repo_panels = []
        for repo, s in github_stats.items():
            auth_note = "" if s["auth"] else (
                "<div style='font-size:10px;color:#888;margin-top:4px'>"
                "(clones/views need GH_TOKEN env var)</div>"
            )
            traffic_cells = ""
            if s["auth"]:
                traffic_cells = f"""
              <div class="metric"><div class="label">Clones (14d)</div>
                <div class="value">{s['clones_14d']}<small style='font-size:11px;color:#888'> ({s['unique_clones_14d']} unique)</small></div></div>
              <div class="metric"><div class="label">Views (14d)</div>
                <div class="value">{s['views_14d']}<small style='font-size:11px;color:#888'> ({s['unique_views_14d']} unique)</small></div></div>"""
            repo_panels.append(f"""
        <div style="margin-bottom:10px">
          <div style="font-size:13px;font-weight:600;margin-bottom:6px">
            <a href="https://github.com/{repo}" style="color:#222;text-decoration:none">⎘ github.com/{repo}</a>
          </div>
          <div class="grid">
            <div class="metric"><div class="label">⭐ Stars</div><div class="value">{s['stars']}</div></div>
            <div class="metric"><div class="label">🍴 Forks</div><div class="value">{s['forks']}</div></div>
            <div class="metric"><div class="label">👁 Watchers</div><div class="value">{s['watchers']}</div></div>{traffic_cells}
          </div>
          {auth_note}
        </div>""")
        adoption_html = f"""
  <div class="panel">
    <h3 style="margin-top:0">Adoption</h3>
    {"".join(repo_panels)}
  </div>"""

    # ARMS Signals API panel — x402 paid endpoints
    api_stats_html = ""
    if api_stats:
        ep_summary = api_stats.get("endpoint_summary", {}) or {}
        totals = api_stats.get("counts", {}) or {}
        # Sum across the paid endpoints only — `_total.200` includes free
        # endpoints (/, /health, /stats) which would inflate the count.
        total_paid   = sum(c["paid"]   for c in ep_summary.values())
        total_probes = sum(c["probes"] for c in ep_summary.values())
        total_reqs   = int(totals.get("_total.requests", 0))
        ep_rows = []
        for path, c in ep_summary.items():
            asset = "ETH" if "/eth/" in path else ("BTC" if "/btc/" in path else "?")
            ep_rows.append(f"""
        <tr>
          <td><code>{path}</code></td>
          <td>{asset}</td>
          <td style="color:#27ae60;font-weight:600">{c['paid']}</td>
          <td>{c['probes']}</td>
          <td>{c['total']}</td>
        </tr>""")
        ep_table = "\n".join(ep_rows) if ep_rows else (
            "<tr><td colspan='5' style='text-align:center;color:#888'>"
            "no traffic yet</td></tr>")
        api_stats_html = f"""
  <div class="panel">
    <h3 style="margin-top:0">
      ARMS Signals API — paid x402 endpoints
      <small style="font-weight:normal;font-size:12px;color:#666">
        github.com/tradingdesk26/armsys-signals
      </small>
    </h3>
    <div class="grid">
      <div class="metric"><div class="label">Total requests</div>
           <div class="value">{total_reqs}</div></div>
      <div class="metric"><div class="label">Paid calls (200)</div>
           <div class="value" style="color:#27ae60">{total_paid}</div></div>
      <div class="metric"><div class="label">Probes (402)</div>
           <div class="value">{total_probes}</div></div>
      <div class="metric"><div class="label">Price / call</div>
           <div class="value">$0.001</div></div>
      <div class="metric"><div class="label">Network</div>
           <div class="value">Base mainnet</div></div>
      <div class="metric"><div class="label">Facilitator</div>
           <div class="value" style="font-size:13px">Coinbase CDP</div></div>
    </div>
    <table style="margin-top:10px;width:100%">
      <tr>
        <th>Endpoint</th><th>Asset</th>
        <th style="color:#27ae60">Paid</th>
        <th>Probes</th><th>Total</th>
      </tr>
      {ep_table}
    </table>
    <div style="font-size:11px;color:#888;margin-top:8px">
      Endpoints listed on
      <a href="https://agentic.market" style="color:#666">agentic.market</a>.
      Try: <code>curl -i https://regimeshift.xyz/api/v1/asset/eth/vrp</code>
    </div>
  </div>"""

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>vrp-agent dashboard</title>
  <meta http-equiv="refresh" content="60">
  <style>
    body {{ font-family: -apple-system, sans-serif; max-width: 1200px; margin: 20px auto; padding: 0 20px; color: #222; }}
    h1 {{ font-size: 20px; margin-bottom: 4px; }}
    h1 small {{ color: #888; font-weight: normal; font-size: 13px; }}
    .panel {{ background: #f7f9fc; border: 1px solid #d8e0ea; border-radius: 6px; padding: 12px 16px; margin: 12px 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }}
    .metric {{ display: flex; flex-direction: column; padding: 6px 10px; background: white; border-radius: 4px; border: 1px solid #e1e5eb; }}
    .metric .label {{ font-size: 11px; color: #888; text-transform: uppercase; }}
    .metric .value {{ font-size: 17px; font-weight: bold; margin-top: 2px; }}
    .state-pill {{ display: inline-block; padding: 4px 10px; border-radius: 12px; color: white; font-weight: bold; background: {state_color}; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #e1e5eb; }}
    th {{ background: #f0f3f8; font-weight: 600; font-size: 11px; text-transform: uppercase; }}
    .footer {{ color: #888; font-size: 11px; margin-top: 20px; }}
    img {{ max-width: 100%; height: auto; }}
  </style>
</head>
<body>
  <h1>vrp-agent dashboard <small>last updated {now_utc}</small></h1>

  <div class="panel">
    <h3 style="margin-top:0">Current state: <span class="state-pill">{state_info['state']}</span></h3>
    <div class="grid">
      <div class="metric"><div class="label">ETH price</div><div class="value">${signal_snap.price:.2f}</div></div>
      <div class="metric"><div class="label">VRP</div><div class="value">{signal_snap.vrp:+.2f}</div></div>
      <div class="metric"><div class="label">R_long(72h)</div><div class="value">{signal_snap.r_long:.1f}%</div></div>
      <div class="metric"><div class="label">R_short(6h)</div><div class="value">{signal_snap.r_short:.1f}%</div></div>
      <div class="metric"><div class="label">DVOL</div><div class="value">{signal_snap.dvol:.1f}%</div></div>
      <div class="metric"><div class="label">Quiet regime</div><div class="value">{'YES' if signal_snap.in_quiet_regime else 'NO'}</div></div>
    </div>
  </div>

  <div class="panel">
    <h3 style="margin-top:0">Capital allocation</h3>
    <div class="grid">
      <div class="metric"><div class="label">Base USDC</div><div class="value">${state_info['base_usdc']:.4f}</div></div>
      <div class="metric"><div class="label">Base ETH</div><div class="value">{state_info['base_eth']:.6f}</div></div>
      <div class="metric"><div class="label">LP NFT</div><div class="value">{'#'+str(state_info['lp_token_id']) if state_info['lp_token_id'] else '—'}</div></div>
      <div class="metric"><div class="label">HL position</div><div class="value">{state_info['hl_perp_size']:+.4f} ETH</div></div>
      <div class="metric"><div class="label">HL margin</div><div class="value">${state_info['hl_margin']:.2f}</div></div>
    </div>
  </div>

  <div class="panel">
    <h3 style="margin-top:0">PnL summary</h3>
    <div class="grid">
      <div class="metric"><div class="label">Closed trades</div><div class="value">{state_info['closed_trades']}</div></div>
      <div class="metric"><div class="label">Total PnL</div><div class="value">${state_info['total_pnl_usd']:+.4f}</div></div>
      <div class="metric"><div class="label">Win rate</div><div class="value">{state_info['win_rate']*100:.0f}%</div></div>
      <div class="metric"><div class="label">Daily PnL</div><div class="value">${state_info['daily_pnl']:+.4f}</div></div>
    </div>
  </div>

  <div class="panel">
    <h3 style="margin-top:0">Signal chart (last 7d with entry / exit thresholds)</h3>
    <img src="{chart_filename}" alt="ETH price + VRP">
  </div>

{api_stats_html}

{adoption_html}

  <div class="panel">
    <h3 style="margin-top:0">Recent trades</h3>
    <table>
      <tr>
        <th>#</th><th>Entry ts</th><th>Entry $</th><th>Size ETH</th>
        <th>Exit ts</th><th>Exit $</th><th>P/L</th><th>Exit reason</th>
      </tr>
      {trades_html}
    </table>
  </div>

  <div class="footer">
    vrp-agent · auto-refresh every 60s · git commit
    <a href="https://github.com/tradingdesk26/vrp-agent">tradingdesk26/vrp-agent</a>
  </div>
</body>
</html>"""
    out_path.write_text(html)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    tracker = PnLTracker()
    base = BaseClient()
    lp   = LPManager(base)
    hl   = HLExecutor()

    log.info("Fetching signal data…")
    df = fetch_signal_df()

    log.info("Computing latest snapshot…")
    sig_snap = sig.compute_snapshot(df)
    state_info = collect_state(tracker, base, lp, hl)

    log.info("Reading trades from SQLite…")
    trades_df = pd.read_sql_query(
        "SELECT * FROM trades ORDER BY id DESC LIMIT 50",
        tracker._conn,
    )

    log.info("Rendering chart…")
    render_chart(df, trades_df, OUT_DIR / "chart.png")

    log.info("Fetching GitHub adoption stats…")
    gh_stats = fetch_github_stats([
        "tradingdesk26/vrp-agent",
        "tradingdesk26/regimeshift-fx",
        "tradingdesk26/armsys-signals",
    ])

    log.info("Fetching ARMS Signals API stats…")
    api_stats = fetch_api_stats()

    log.info("Rendering HTML…")
    render_html(state_info, sig_snap, trades_df, "chart.png",
                 OUT_DIR / "index.html",
                 github_stats=gh_stats,
                 api_stats=api_stats)

    log.info(f"Done — output in {OUT_DIR}")


if __name__ == "__main__":
    main()
