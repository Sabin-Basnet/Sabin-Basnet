#!/usr/bin/env python3
"""
market_engine.py — GitHub Profile Stock Exchange Engine
Core business logic for $SABIN token trading via GitHub Issues.

Usage:
    python3 market_engine.py "<issue_title>" "<actor>"

Never raises past main(): all failures are caught, reported as a
human-readable trade result, and the script exits 0 so the CI
workflow can always comment on / close the issue.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STATE_FILE = "market_state.json"
CHART_FILE = "market_chart.svg"
README_FILE = "README.md"

TICKER = "$SABIN"
MAX_BUYS_PER_WINDOW = 3
WINDOW_HOURS = 24
BUY_IMPACT = 0.05          # +5% price per share bought
SELL_IMPACT = 0.04         # -4% price per share sold
PRICE_FLOOR = 1.00
MAX_CHART_POINTS = 30
MAX_SELL_QTY = 1000        # sanity cap against malicious/typo input

MARKET_START = "<!-- MARKET_START -->"
MARKET_END = "<!-- MARKET_END -->"


class TradeError(Exception):
    """Any user-facing, expected trade validation failure."""


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def default_state():
    return {
        "current_price": 1.0,
        "total_volume": 0,
        "price_history": [1.0],
        "users": {}
    }


def load_state():
    state = default_state()
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return state

    if not isinstance(data, dict):
        return state

    for key, val in state.items():
        data.setdefault(key, val)
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    if not isinstance(data.get("price_history"), list) or not data["price_history"]:
        data["price_history"] = [float(data.get("current_price", 1.0))]
    try:
        data["current_price"] = float(data["current_price"])
        data["total_volume"] = int(data["total_volume"])
    except (TypeError, ValueError):
        data["current_price"] = 1.0
        data["total_volume"] = 0
    return data


def save_state(state):
    """Atomic write to avoid corrupting the file on interruption."""
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, STATE_FILE)


def get_user(state, username):
    user = state["users"].setdefault(username, {
        "shares": 0,
        "avg_buy_price": 0.0,
        "buys": [],
        "realized_pnl": 0.0
    })
    user.setdefault("shares", 0)
    user.setdefault("avg_buy_price", 0.0)
    user.setdefault("buys", [])
    user.setdefault("realized_pnl", 0.0)
    return user


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_issue_title(title):
    """
    Accepted formats (case-insensitive, flexible whitespace):
        market: BUY
        market: SELL
        market: SELL 5
    Returns (action, quantity).
    """
    if not title or not title.strip():
        raise TradeError(
            "Empty issue title. Use `market: BUY` or `market: SELL <quantity>`."
        )

    match = re.match(r"^\s*market\s*:\s*(BUY|SELL)\s*(\d+)?\s*$", title.strip(), re.IGNORECASE)
    if not match:
        raise TradeError(
            f"Could not parse a valid command from title `{title}`. "
            "Expected `market: BUY` or `market: SELL <quantity>`."
        )

    action = match.group(1).upper()
    qty_str = match.group(2)

    if action == "BUY":
        # BUY always executes exactly 1 share regardless of any trailing number.
        return "BUY", 1

    qty = int(qty_str) if qty_str else 1
    if qty <= 0:
        raise TradeError("Sell quantity must be a positive integer.")
    if qty > MAX_SELL_QTY:
        raise TradeError(f"Sell quantity too large (max {MAX_SELL_QTY} per order).")
    return "SELL", qty


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def prune_old_buys(buy_timestamps, now):
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    kept = []
    for ts in buy_timestamps:
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            kept.append(ts)
    return kept


def execute_buy(state, username):
    now = datetime.now(timezone.utc)
    user = get_user(state, username)

    user["buys"] = prune_old_buys(user["buys"], now)
    if len(user["buys"]) >= MAX_BUYS_PER_WINDOW:
        raise TradeError(
            f"@{username} has reached the limit of {MAX_BUYS_PER_WINDOW} buys "
            f"per {WINDOW_HOURS}h rolling window. Please try again later."
        )

    price = state["current_price"]
    qty = 1

    prev_shares = user["shares"]
    prev_cost = prev_shares * user["avg_buy_price"]
    new_shares = prev_shares + qty
    user["avg_buy_price"] = round((prev_cost + price * qty) / new_shares, 6)
    user["shares"] = new_shares
    user["buys"].append(now.isoformat())

    new_price = round(price * (1 + BUY_IMPACT), 4)
    state["current_price"] = new_price
    state["total_volume"] += qty
    state["price_history"].append(new_price)

    return {
        "action": "BUY",
        "username": username,
        "qty": qty,
        "fill_price": price,
        "new_price": new_price,
        "shares_owned": user["shares"],
    }


def execute_sell(state, username, qty):
    user = state["users"].get(username)
    owned = user.get("shares", 0) if user else 0
    if not user or owned < qty:
        raise TradeError(
            f"@{username} cannot sell {qty} share(s); only owns {owned}."
        )

    price = state["current_price"]
    pnl = round((price - user["avg_buy_price"]) * qty, 6)

    user["shares"] -= qty
    user["realized_pnl"] = round(user.get("realized_pnl", 0.0) + pnl, 6)
    if user["shares"] == 0:
        user["avg_buy_price"] = 0.0

    new_price = round(max(PRICE_FLOOR, price * (1 - SELL_IMPACT * qty)), 4)
    state["current_price"] = new_price
    state["total_volume"] += qty
    state["price_history"].append(new_price)

    return {
        "action": "SELL",
        "username": username,
        "qty": qty,
        "fill_price": price,
        "new_price": new_price,
        "realized_pnl": pnl,
        "shares_owned": user["shares"],
    }


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

def compute_leaderboard(state):
    price = state["current_price"]
    rows = []
    for username, user in state["users"].items():
        shares = user.get("shares", 0)
        avg = user.get("avg_buy_price", 0.0)
        realized = user.get("realized_pnl", 0.0)
        unrealized = round((price - avg) * shares, 6) if shares > 0 else 0.0
        total_pnl = round(realized + unrealized, 6)
        rows.append({
            "username": username,
            "shares": shares,
            "avg_buy_price": avg,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": total_pnl,
        })
    # Primary: shares owned desc. Secondary: total PnL desc.
    rows.sort(key=lambda r: (-r["shares"], -r["total_pnl"]))
    return rows


# ---------------------------------------------------------------------------
# SVG chart
# ---------------------------------------------------------------------------

def generate_chart_svg(state, path=CHART_FILE):
    history = state["price_history"][-MAX_CHART_POINTS:]
    if not history:
        history = [1.0]
    if len(history) == 1:
        history = history * 2  # need 2 points to draw a line

    width, height = 900, 340
    pad_left, pad_right, pad_top, pad_bottom = 60, 30, 50, 40
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    y_min, y_max = min(history), max(history)
    if y_min == y_max:
        y_min -= 0.5
        y_max += 0.5
    span = y_max - y_min
    y_min -= span * 0.12
    y_max += span * 0.12
    span = y_max - y_min

    def x_at(i):
        return pad_left + (i / (len(history) - 1)) * plot_w

    def y_at(v):
        return pad_top + plot_h - ((v - y_min) / span) * plot_h

    points = [(x_at(i), y_at(v)) for i, v in enumerate(history)]
    line_path = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    area_path = (
        line_path
        + f" L {points[-1][0]:.2f},{pad_top + plot_h:.2f}"
        + f" L {points[0][0]:.2f},{pad_top + plot_h:.2f} Z"
    )

    # --- Trend-based theming: green if price is up over the visible window,
    # red if down, so the chart visually communicates momentum at a glance.
    window_start, window_end = history[0], history[-1]
    is_up = window_end >= window_start
    trend_color = "#34d399" if is_up else "#f87171"
    trend_fill_top = "#10b981" if is_up else "#ef4444"
    change_abs = window_end - window_start
    change_pct = (change_abs / window_start * 100) if window_start else 0.0
    arrow = "▲" if is_up else ("▼" if change_abs < 0 else "▬")

    grid_lines = []
    for i in range(5):
        gy = pad_top + (plot_h / 4) * i
        gval = y_max - span * (i / 4)
        grid_lines.append(
            f'<line x1="{pad_left}" y1="{gy:.2f}" x2="{width - pad_right}" y2="{gy:.2f}" '
            f'stroke="#123524" stroke-width="1" stroke-dasharray="4,4" />'
        )
        grid_lines.append(
            f'<text x="{pad_left - 10}" y="{gy + 4:.2f}" text-anchor="end" '
            f'font-size="11" fill="#5fdca0" font-family="monospace">${gval:.2f}</text>'
        )

    # High / low markers so the chart reads like a real ticker
    max_idx = history.index(max(history))
    min_idx = history.index(min(history))
    hi_x, hi_y = x_at(max_idx), y_at(history[max_idx])
    lo_x, lo_y = x_at(min_idx), y_at(history[min_idx])
    hi_lo_markers = f'''
    <circle cx="{hi_x:.2f}" cy="{hi_y:.2f}" r="3" fill="#a7f3d0" stroke="#07140f" stroke-width="1" />
    <text x="{hi_x:.2f}" y="{hi_y - 10:.2f}" text-anchor="middle" font-size="10" fill="#a7f3d0" font-family="monospace">${history[max_idx]:.2f}</text>
    <circle cx="{lo_x:.2f}" cy="{lo_y:.2f}" r="3" fill="#fca5a5" stroke="#07140f" stroke-width="1" />
    <text x="{lo_x:.2f}" y="{lo_y + 18:.2f}" text-anchor="middle" font-size="10" fill="#fca5a5" font-family="monospace">${history[min_idx]:.2f}</text>
    '''

    current_price = state["current_price"]
    last_x, last_y = points[-1]

    # Small trailing dots for recent trades
    dots = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{trend_color}" opacity="0.85" />'
        for x, y in points[-6:-1]
    )

    # Pulsing "live" dot on the most recent point — gives the impression
    # of an actively updating market rather than a static snapshot.
    live_dot = f'''
    <circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="4" fill="{trend_color}" />
    <circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="4" fill="{trend_color}" opacity="0.6">
      <animate attributeName="r" values="4;12;4" dur="1.8s" repeatCount="indefinite" />
      <animate attributeName="opacity" values="0.6;0;0.6" dur="1.8s" repeatCount="indefinite" />
    </circle>
    '''

    change_sign = "+" if change_abs >= 0 else ""
    svg = f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{TICKER} price chart">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#07140f" rx="12" />
  <defs>
    <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{trend_fill_top}" stop-opacity="0.35" />
      <stop offset="100%" stop-color="{trend_fill_top}" stop-opacity="0" />
    </linearGradient>
  </defs>
  {''.join(grid_lines)}
  <path d="{area_path}" fill="url(#areaFill)" stroke="none" />
  <path d="{line_path}" fill="none" stroke="{trend_color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
  {dots}
  {hi_lo_markers}
  {live_dot}

  <text x="{pad_left}" y="24" font-size="14" fill="#a7f3d0" font-family="monospace" font-weight="bold">{TICKER} / USD</text>
  <text x="{pad_left}" y="40" font-size="11" fill="{trend_color}" font-family="monospace">{arrow} {change_sign}{change_abs:.4f} ({change_sign}{change_pct:.2f}%) · last {len(history)} trades</text>

  <g transform="translate({width - pad_right - 130}, 14)">
    <rect x="0" y="0" width="130" height="24" rx="6" fill="#0f2a1d" stroke="{trend_color}" stroke-width="1" />
    <text x="65" y="16" text-anchor="middle" font-size="12" fill="{trend_color}" font-family="monospace" font-weight="bold">${current_price:.4f}</text>
  </g>

  <g transform="translate({width - pad_right - 130}, 40)">
    <circle cx="8" cy="6" r="4" fill="#ef4444">
      <animate attributeName="opacity" values="1;0.3;1" dur="1.5s" repeatCount="indefinite" />
    </circle>
    <text x="18" y="10" font-size="10" fill="#f87171" font-family="monospace" font-weight="bold">LIVE</text>
  </g>
</svg>'''

    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path


# ---------------------------------------------------------------------------
# README update
# ---------------------------------------------------------------------------

def build_readme_section(state, leaderboard):
    repo = os.environ.get("GITHUB_REPOSITORY", "owner/repo")
    owner_repo = repo if "/" in repo else "owner/repo"
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    cache_buster = int(time.time())
    chart_url = f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{CHART_FILE}?v={cache_buster}"

    total_circulating = sum(u.get("shares", 0) for u in state["users"].values())
    price = state["current_price"]
    market_cap = round(price * total_circulating, 2)

    buy_issue_url = (
        f"https://github.com/{owner_repo}/issues/new"
        f"?title={quote('market: BUY')}"
        f"&body={quote(f'Auto-generated buy order for 1 share of {TICKER}.')}"
    )
    sell_issue_url = (
        f"https://github.com/{owner_repo}/issues/new"
        f"?title={quote('market: SELL 1')}"
        f"&body={quote('Edit the title quantity as needed, e.g. `market: SELL 5`.')}"
    )

    cap_badge = (
        f"![Market Cap](https://img.shields.io/badge/Market%20Cap-%24{market_cap:,.2f}"
        f"-10b981?style=for-the-badge)"
    )
    supply_badge = (
        f"![Circulating Supply](https://img.shields.io/badge/Circulating%20Supply-"
        f"{total_circulating}-34d399?style=for-the-badge)"
    )
    buy_badge = (
        f"[![Buy 1 Share](https://img.shields.io/badge/BUY-1%20SHARE-10b981?"
        f"style=for-the-badge&logo=github)]({buy_issue_url})"
    )
    sell_badge = (
        f"[![Sell Shares](https://img.shields.io/badge/SELL-SHARES-ef4444?"
        f"style=for-the-badge&logo=github)]({sell_issue_url})"
    )

    history = state["price_history"]
    prev_price = history[-2] if len(history) >= 2 else price
    change = price - prev_price
    change_pct = (change / prev_price * 100) if prev_price else 0.0
    arrow = "🟢▲" if change > 0 else ("🔴▼" if change < 0 else "⚪▬")

    holders = sum(1 for u in state["users"].values() if u.get("shares", 0) > 0)
    snapshot_table = (
        "| Metric | Value |\n"
        "|---|---|\n"
        f"| Current Price | ${price:,.4f} |\n"
        f"| Last Change | {arrow} {change:+.4f} ({change_pct:+.2f}%) |\n"
        f"| Market Cap | ${market_cap:,.2f} |\n"
        f"| Circulating Supply | {total_circulating} {TICKER} |\n"
        f"| Total Volume | {state['total_volume']} |\n"
        f"| Holders | {holders} |\n"
    )

    top_rows = leaderboard[:10]
    if top_rows:
        leaderboard_rows = "\n".join(
            f"| {i + 1} | @{r['username']} | {r['shares']} | ${r['avg_buy_price']:.4f} | "
            f"${r['realized_pnl']:+,.2f} | ${r['unrealized_pnl']:+,.2f} | ${r['total_pnl']:+,.2f} |"
            for i, r in enumerate(top_rows)
        )
    else:
        leaderboard_rows = "| - | *No holders yet. Be the first to buy!* | - | - | - | - | - |"

    leaderboard_table = (
        "| Rank | Holder | Shares | Avg Buy | Realized PnL | Unrealized PnL | Total PnL |\n"
        "|---|---|---|---|---|---|---|\n"
        f"{leaderboard_rows}"
    )

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""{cap_badge} {supply_badge}

{buy_badge} {sell_badge}

![{TICKER} chart]({chart_url})

### 📊 Live Snapshot

{snapshot_table}
### 🏆 Top 10 Shareholders

{leaderboard_table}

<sub>Last updated {updated_at} · Powered by GitHub Profile Stock Exchange Engine</sub>"""


def update_readme(state, leaderboard, path=README_FILE):
    if not os.path.exists(path):
        content = f"# Welcome\n\n{MARKET_START}\n{MARKET_END}\n"
    else:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

    if MARKET_START not in content or MARKET_END not in content:
        content = content.rstrip() + f"\n\n{MARKET_START}\n{MARKET_END}\n"

    section = build_readme_section(state, leaderboard)
    replacement = f"{MARKET_START}\n{section}\n{MARKET_END}"

    pattern = re.compile(re.escape(MARKET_START) + r".*?" + re.escape(MARKET_END), re.DOTALL)
    # Use a callable replacement so backslashes/groups in `section` are never
    # interpreted as regex backreferences by re.sub.
    new_content = pattern.sub(lambda _m: replacement, content, count=1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)


# ---------------------------------------------------------------------------
# Result formatting + entrypoint
# ---------------------------------------------------------------------------

def format_result_message(result, error=None):
    if error:
        return f"❌ **Trade Failed**\n\n{error}"

    action = result["action"]
    username = result["username"]
    if action == "BUY":
        return (
            "✅ **BUY Executed**\n\n"
            f"- Trader: @{username}\n"
            "- Quantity: 1 share\n"
            f"- Fill Price: ${result['fill_price']:.4f}\n"
            f"- New {TICKER} Price: ${result['new_price']:.4f}\n"
            f"- Shares Owned: {result['shares_owned']}"
        )

    return (
        "✅ **SELL Executed**\n\n"
        f"- Trader: @{username}\n"
        f"- Quantity: {result['qty']} share(s)\n"
        f"- Fill Price: ${result['fill_price']:.4f}\n"
        f"- Realized PnL (this trade): ${result['realized_pnl']:+.2f}\n"
        f"- New {TICKER} Price: ${result['new_price']:.4f}\n"
        f"- Shares Remaining: {result['shares_owned']}"
    )


def main():
    if len(sys.argv) < 3:
        print(format_result_message(None, error="Usage: market_engine.py <issue_title> <actor>"))
        sys.exit(0)

    issue_title = sys.argv[1]
    actor = (sys.argv[2] or "").strip()

    if not actor:
        print(format_result_message(None, error="Could not determine the acting GitHub user."))
        sys.exit(0)

    state = load_state()

    try:
        action, qty = parse_issue_title(issue_title)
        if action == "BUY":
            result = execute_buy(state, actor)
        else:
            result = execute_sell(state, actor, qty)
    except TradeError as e:
        print(format_result_message(None, error=str(e)))
        sys.exit(0)
    except Exception as e:  # never let an unexpected error crash the workflow
        print(format_result_message(None, error=f"Unexpected error: {e}"))
        sys.exit(0)

    save_state(state)
    generate_chart_svg(state)
    leaderboard = compute_leaderboard(state)
    update_readme(state, leaderboard)

    print(format_result_message(result))


if __name__ == "__main__":
    main()