#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "market_state.json"
README_FILE = ROOT / "README.md"
CHART_FILE = ROOT / "market_chart.svg"


def default_state():
    return {
        "current_price": 1.0,
        "total_volume": 0,
        "price_history": [1.0],
        "users": {},
    }


def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        state = default_state()
        state.update(data)
        if not isinstance(state.get("users"), dict):
            state["users"] = {}
        if not isinstance(state.get("price_history"), list):
            state["price_history"] = [1.0]
        state["current_price"] = float(state.get("current_price", 1.0))
        state["total_volume"] = int(state.get("total_volume", 0))
        state["price_history"] = [float(item) for item in state.get("price_history", [1.0])]
        return state
    return default_state()


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")


def sanitize_username(username):
    cleaned = (username or "unknown").strip().lstrip("@")
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", cleaned) or "unknown"
    return cleaned


def parse_action(title):
    if not title:
        return None
    normalized = title.strip()
    if normalized.lower().startswith("market:"):
        payload = normalized.split(":", 1)[1].strip()
        action = payload.split()[0].upper() if payload else ""
        if action not in {"BUY", "SELL"}:
            return None
        
        # Parse quantity from title
        quantity = 1
        if len(payload.split()) > 1:
            try:
                quantity = int(payload.split()[1])
            except ValueError:
                quantity = 1
        
        # BUY: force 1 share per transaction
        if action == "BUY":
            return {"action": action, "quantity": 1}
        
        # SELL: allow custom quantity
        return {"action": action, "quantity": max(1, quantity)}
    return None


def get_repo_slug():
    repo = os.getenv("GITHUB_REPOSITORY")
    if repo:
        return repo

    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            remote = result.stdout.strip()
            if remote.startswith("git@github.com:"):
                return remote.split(":", 1)[1].removesuffix(".git")
            if remote.startswith("https://github.com/"):
                return remote[len("https://github.com/"):].removesuffix(".git")
            if remote.startswith("http://github.com/"):
                return remote[len("http://github.com/"):].removesuffix(".git")
    except Exception:
        pass

    return "OWNER/REPO"


def build_issue_links(repo_slug):
    base = f"https://github.com/{repo_slug}/issues/new?title=market%3A+"
    return {
        "buy": f"{base}BUY",
        "sell": f"{base}SELL",
    }


def get_user_entry(state, username):
    users = state.setdefault("users", {})
    entry = users.get(username)
    if entry is None:
        entry = {
            "shares": 0,
            "avg_buy_price": 0.0,
            "buys": [],
            "realized_pnl": 0.0,
        }
        users[username] = entry
    entry.setdefault("shares", 0)
    entry.setdefault("avg_buy_price", 0.0)
    entry.setdefault("buys", [])
    entry.setdefault("realized_pnl", 0.0)
    return entry


def prune_recent_buys(entry, now):
    cutoff = now - timedelta(hours=24)
    recent_buys = []
    for stamp in entry.get("buys", []):
        try:
            if datetime.fromisoformat(stamp) >= cutoff:
                recent_buys.append(stamp)
        except ValueError:
            continue
    entry["buys"] = recent_buys


def execute_trade(state, action, username):
    now = datetime.now(timezone.utc)
    entry = get_user_entry(state, username)
    prune_recent_buys(entry, now)

    current_price = float(state.get("current_price", 1.0))
    total_volume = int(state.get("total_volume", 0))
    
    # Calculate market metrics for dynamic pricing
    total_shares_held = sum(int(user.get("shares", 0)) for user in state.get("users", {}).values())
    
    if action["action"] == "BUY":
        if len(entry.get("buys", [])) >= 3:
            return False, f"@{username} has reached the 3-buy limit in 24 hours. Max 1 share per transaction, max 3 per day."
        
        shares = int(entry.get("shares", 0))
        avg_cost = float(entry.get("avg_buy_price", 0.0))
        
        # Update average buy price
        new_avg = ((shares * avg_cost) + current_price) / (shares + 1) if shares > 0 else current_price
        entry["shares"] = shares + 1
        entry["avg_buy_price"] = round(new_avg, 2)
        entry["buys"].append(now.isoformat())
        
        # Dynamic price increase based on demand and volume
        demand_pressure = (total_volume / max(1, total_shares_held)) * 0.1
        price_increase = 0.3 + (0.2 * demand_pressure)
        new_price = round(max(1.0, current_price + price_increase), 2)
        
        state["current_price"] = new_price
        state["total_volume"] = total_volume + 1
        state["price_history"].append(new_price)
        
        return True, f"BUY 1 $sabin executed for @{username}. New price: ${new_price:.2f}. (3 buys per day max)"

    if action["action"] == "SELL":
        owned = int(entry.get("shares", 0))
        quantity = action["quantity"]
        
        if owned < quantity:
            return False, f"@{username} only owns {owned} $sabin and cannot sell {quantity}."
        
        trade_price = current_price
        entry["realized_pnl"] = float(entry.get("realized_pnl", 0.0)) + ((trade_price - float(entry.get("avg_buy_price", 0.0))) * quantity)
        entry["shares"] = owned - quantity
        
        if int(entry.get("shares", 0)) <= 0:
            entry["avg_buy_price"] = 0.0
        
        # Dynamic price decrease based on supply pressure (scales with quantity sold)
        supply_pressure = (total_shares_held / max(1, total_shares_held + 5)) * 0.1
        price_decrease = (0.25 + (0.15 * supply_pressure)) * quantity
        new_price = round(max(1.0, current_price - price_decrease), 2)
        
        state["current_price"] = new_price
        state["total_volume"] = total_volume + quantity
        state["price_history"].append(new_price)
        
        return True, f"SELL {quantity} $sabin executed for @{username}. New price: ${new_price:.2f}."

    return False, "Unsupported action."


def compute_unrealized_pnl(entry, current_price):
    shares = int(entry.get("shares", 0))
    avg_cost = float(entry.get("avg_buy_price", 0.0))
    if shares <= 0:
        return 0.0
    return round((shares * current_price) - (shares * avg_cost), 2)


def build_leaderboard(state, current_price):
    leaderboard = []
    for username, entry in state.get("users", {}).items():
        shares = int(entry.get("shares", 0))
        realized = float(entry.get("realized_pnl", 0.0))
        unrealized = compute_unrealized_pnl(entry, current_price)
        total_pnl = round(realized + unrealized, 2)
        leaderboard.append((username, shares, float(entry.get("avg_buy_price", 0.0)), total_pnl))
    
    # Sort primarily by Shares Owned (descending), secondarily by Total PnL (descending)
    leaderboard.sort(key=lambda item: (item[1], item[3]), reverse=True)
    return leaderboard[:10]


def generate_chart_svg(price_history):
    width, height = 900, 320
    padding = 50
    
    # Plot recent 30 trades to prevent crowding as history grows
    raw_values = [float(value) for value in price_history[-30:]] if len(price_history) >= 30 else [float(value) for value in price_history]
    values = raw_values if len(raw_values) >= 2 else raw_values + [raw_values[-1]]

    min_price = max(1.0, min(values) - 0.5)
    max_price = max(values) + 0.5
    if max_price <= min_price:
        max_price = min_price + 1.0

    points = []
    for index, value in enumerate(values):
        x = padding + (index / max(1, len(values) - 1)) * (width - padding * 2)
        y = height - padding - ((value - min_price) / (max_price - min_price)) * (height - padding * 2)
        points.append((x, y))

    line_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    last_x, last_y = points[-1]
    latest_value = values[-1]

    grid_lines = []
    for step in range(5):
        y = padding + (step / 4) * (height - padding * 2)
        grid_lines.append(f'<line x1="{padding}" y1="{y:.2f}" x2="{width - padding}" y2="{y:.2f}" stroke="#1f3d34" stroke-width="1" stroke-dasharray="4 4"/>')

    timestamp = datetime.now(timezone.utc).isoformat()
    
    svg = f'''<!-- Generated: {timestamp} -->
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#07140f" rx="24"/>
  <rect x="18" y="18" width="864" height="284" rx="20" fill="#091a12" stroke="#1f3d34" stroke-width="2"/>
  <text x="52" y="60" fill="#86efac" font-family="Segoe UI, Arial, sans-serif" font-size="18" font-weight="600">Market Price History ($sabin)</text>
  <text x="52" y="82" fill="#6ee7b7" font-family="Segoe UI, Arial, sans-serif" font-size="13">Live trend • emerald mode</text>

  <!-- Y-Axis Scale Bounds -->
  <text x="{width - padding}" y="{padding}" fill="#6ee7b7" font-family="Segoe UI, Arial, sans-serif" font-size="12" text-anchor="end">${max_price:.2f}</text>
  <text x="{width - padding}" y="{height - padding + 15}" fill="#6ee7b7" font-family="Segoe UI, Arial, sans-serif" font-size="12" text-anchor="end">${min_price:.2f}</text>

  {''.join(grid_lines)}
  <polyline points="{line_points}" fill="none" stroke="#34d399" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="8" fill="#ecfdf5" stroke="#34d399" stroke-width="3"/>

  <!-- Live Price Badge -->
  <rect x="{width - 160}" y="35" width="110" height="32" rx="8" fill="#10b981"/>
  <text x="{width - 105}" y="56" fill="#ffffff" font-family="Segoe UI, Arial, sans-serif" font-size="15" font-weight="700" text-anchor="middle">${latest_value:.2f}</text>
</svg>
'''
    return svg


def build_readme_section(state, repo_slug):
    current_price = float(state.get("current_price", 1.0))
    previous_price = float(state.get("price_history", [1.0])[-2]) if len(state.get("price_history", [1.0])) > 1 else current_price
    if previous_price <= 0:
        previous_price = current_price
    
    change_pct = 0.0 if previous_price == current_price else round(((current_price - previous_price) / previous_price) * 100, 2)
    trend = "▲ Bullish" if change_pct > 0 else "▼ Bearish" if change_pct < 0 else "◆ Neutral"
    links = build_issue_links(repo_slug)
    leaderboard = build_leaderboard(state, current_price)
    
    # Calculate Total Market Cap (Current Price * Total Shares Held Across All Users)
    total_circulating_shares = sum(int(user.get("shares", 0)) for user in state.get("users", {}).values())
    market_cap = round(current_price * total_circulating_shares, 2)

    # Append timestamp parameter as cache buster for GitHub Camo CDN
    cache_buster = int(datetime.now(timezone.utc).timestamp())
    chart_url = f"https://raw.githubusercontent.com/{repo_slug}/main/market_chart.svg?v={cache_buster}"

    rows = []
    for index, (username, shares, avg_cost, total_pnl) in enumerate(leaderboard, start=1):
        rows.append(f"| {index} | @{username} | {shares} | ${avg_cost:.2f} | ${total_pnl:.2f} |")

    if not rows:
        rows.append("| 1 | No trades yet | 0 | $0.00 | $0.00 |")

    mcap_badge = f"https://img.shields.io/badge/Market%20Cap-${market_cap:.2f}-10B981?logo=bitcoin&logoColor=white"
    supply_badge = f"https://img.shields.io/badge/Circulating%20Supply-{total_circulating_shares}%20shares-3B82F6?logo=analytics&logoColor=white"

    section = f'''## 📈 $sabin Coin Market

<div align="center">
  <img src="{chart_url}" alt="$sabin price chart" width="100%" />
</div>

<div align="center">
  <h1 style="color: #34d399; margin: 10px 0;">💰 Current Price: ${current_price:.2f}</h1>
  <p style="color: #6ee7b7; font-size: 14px; margin: 5px 0;">Change: <strong>{change_pct:+.2f}%</strong> | Trend: <strong>{trend}</strong></p>
</div>

### ⚡ Live Market Snapshot

<table>
  <tr>
    <td><strong>Current Price</strong></td>
    <td>${current_price:.2f}</td>
    <td><strong>24h Change</strong></td>
    <td>{change_pct:+.2f}%</td>
  </tr>
  <tr>
    <td><strong>Total Volume</strong></td>
    <td>{int(state.get('total_volume', 0))}</td>
    <td><strong>Market Cap</strong></td>
    <td>${market_cap:.2f}</td>
  </tr>
</table>

<div align="center">
  <img src="{mcap_badge}" alt="Market cap badge" />
  <img src="{supply_badge}" alt="Circulating supply badge" />
</div>

<div align="center">
  <a href="{links['buy']}"><img src="https://img.shields.io/badge/BUY_1_SHARE-10B981?style=for-the-badge&logo=trending-up&logoColor=white" alt="Buy 1 share" /></a>
  <a href="{links['sell']}"><img src="https://img.shields.io/badge/SELL_SHARES-F43F5E?style=for-the-badge&logo=trending-down&logoColor=white" alt="Sell shares" /></a>
</div>

> 📋 **Rules**: BUY **1 share at a time** (max 3 per day) • SELL **1+ shares anytime** • Price adjusts dynamically based on demand/supply

### 🏆 Top 10 Shareholders & Profit Leaderboard

| Rank | Investor | Shares Owned | Avg Buy Price | Total Profit/Loss |
| --- | --- | ---: | ---: | ---: |
{chr(10).join(rows)}
'''
    return section


def update_readme(section):
    start_marker = "<!-- MARKET_START -->"
    end_marker = "<!-- MARKET_END -->"
    content = README_FILE.read_text(encoding="utf-8") if README_FILE.exists() else ""

    if start_marker in content and end_marker in content:
        before, _, remainder = content.partition(start_marker)
        _, _, after = remainder.partition(end_marker)
        new_content = before + start_marker + "\n" + section.strip() + "\n" + end_marker + after
    else:
        new_content = content.rstrip() + "\n\n" + start_marker + "\n" + section.strip() + "\n" + end_marker + "\n"

    README_FILE.write_text(new_content, encoding="utf-8")


def main():
    if len(sys.argv) < 3:
        issue_title = "market: BUY"
        github_actor = "demo"
    else:
        issue_title = sys.argv[1]
        github_actor = sys.argv[2]

    state = load_state()
    action = parse_action(issue_title)
    username = sanitize_username(github_actor)

    if action is None:
        output = "No trade executed. Use a title like 'market: BUY' or 'market: SELL 3'."
    else:
        success, detail = execute_trade(state, action, username)
        if success:
            save_state(state)
            output = f"{detail}\nUser: @{username}\nCurrent price: ${state['current_price']:.2f}\nTotal volume: {state['total_volume']}"
        else:
            output = detail

    with CHART_FILE.open("w", encoding="utf-8") as handle:
        handle.write(generate_chart_svg(state.get("price_history", [1.0])))
    update_readme(build_readme_section(state, get_repo_slug()))

    print(output)


if __name__ == "__main__":
    main()