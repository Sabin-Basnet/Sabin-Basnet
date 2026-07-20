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
        action = normalized.split(":", 1)[1].strip().upper()
        return action if action in {"BUY", "SELL"} else None
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
    if action == "BUY":
        if len(entry.get("buys", [])) >= 3:
            return False, f"@{username} has reached the 3-buy limit in 24 hours."
        trade_price = current_price
        shares = int(entry.get("shares", 0))
        avg_cost = float(entry.get("avg_buy_price", 0.0))
        new_avg = ((shares * avg_cost) + trade_price) / (shares + 1) if shares > 0 else trade_price
        entry["shares"] = shares + 1
        entry["avg_buy_price"] = round(new_avg, 2)
        entry["buys"].append(now.isoformat())
        state["current_price"] = round(max(1.0, current_price + 0.5), 2)
        state["total_volume"] = int(state.get("total_volume", 0)) + 1
        state["price_history"].append(state["current_price"])
        return True, f"BUY executed for @{username}. New price: ${state['current_price']:.2f}."

    if action == "SELL":
        if int(entry.get("shares", 0)) < 1:
            return False, f"@{username} does not own any shares to sell."
        trade_price = current_price
        entry["realized_pnl"] = float(entry.get("realized_pnl", 0.0)) + (trade_price - float(entry.get("avg_buy_price", 0.0)))
        entry["shares"] = int(entry.get("shares", 0)) - 1
        if int(entry.get("shares", 0)) <= 0:
            entry["avg_buy_price"] = 0.0
        state["current_price"] = round(max(1.0, current_price - 0.5), 2)
        state["total_volume"] = int(state.get("total_volume", 0)) + 1
        state["price_history"].append(state["current_price"])
        return True, f"SELL executed for @{username}. New price: ${state['current_price']:.2f}."

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
    leaderboard.sort(key=lambda item: item[3], reverse=True)
    return leaderboard[:10]


def generate_chart_svg(price_history):
    width, height = 900, 320
    padding = 50
    values = [float(value) for value in price_history]
    if len(values) < 2:
        values = values + [values[-1]]

    min_price = min(values) - 0.5
    max_price = max(values) + 0.5
    min_price = max(1.0, min_price)
    if max_price <= min_price:
        max_price = min_price + 1.0

    points = []
    for index, value in enumerate(values):
        x = padding + (index / max(1, len(values) - 1)) * (width - padding * 2)
        y = height - padding - ((value - min_price) / (max_price - min_price)) * (height - padding * 2)
        points.append((x, y))

    line_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    area_points = f"{padding:.2f},{height - padding:.2f} " + line_points + f" {width - padding:.2f},{height - padding:.2f}"

    grid_lines = []
    for step in range(5):
        y = padding + (step / 4) * (height - padding * 2)
        grid_lines.append(f'<line x1="{padding}" y1="{y:.2f}" x2="{width - padding}" y2="{y:.2f}" stroke="#1f3d34" stroke-width="1" stroke-dasharray="4 4"/>')

    last_x, last_y = points[-1]
    latest_value = values[-1]
    trend_color = "#34d399" if latest_value >= values[0] else "#f59e0b"

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="panelGlow" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f2f24"/>
      <stop offset="100%" stop-color="#07130d"/>
    </linearGradient>
    <linearGradient id="lineGlow" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#6ee7b7"/>
      <stop offset="100%" stop-color="#34d399"/>
    </linearGradient>
    <linearGradient id="areaGlow" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#34d399" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#34d399" stop-opacity="0.03"/>
    </linearGradient>
    <filter id="softGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="3" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>
  <rect width="100%" height="100%" fill="url(#panelGlow)" rx="24"/>
  <rect x="18" y="18" width="864" height="284" rx="20" fill="#07140f" stroke="#1f3d34" stroke-width="2"/>
  <rect x="32" y="32" width="836" height="256" rx="16" fill="#081b12" stroke="#123b2a"/>
  <text x="52" y="72" fill="#86efac" font-family="Segoe UI, Arial, sans-serif" font-size="18" font-weight="600">Market Price History</text>
  <text x="52" y="96" fill="#6ee7b7" font-family="Segoe UI, Arial, sans-serif" font-size="13">Live trend • emerald mode</text>
  {''.join(grid_lines)}
  <polygon points="{area_points}" fill="url(#areaGlow)"/>
  <polyline points="{line_points}" fill="none" stroke="url(#lineGlow)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" filter="url(#softGlow)"/>
  <polyline points="{line_points}" fill="none" stroke="#ecfdf5" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.35"/>
  <circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="8" fill="#ecfdf5" stroke="{trend_color}" stroke-width="3"/>
  <text x="{width - 150}" y="72" fill="#34d399" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700">${latest_value:.2f}</text>
  <text x="{padding}" y="{height - 16}" fill="#86efac" font-family="Segoe UI, Arial, sans-serif" font-size="13">Price trend</text>
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

    rows = []
    for index, (username, shares, avg_cost, total_pnl) in enumerate(leaderboard, start=1):
        rows.append(f"| {index} | @{username} | {shares} | ${avg_cost:.2f} | ${total_pnl:.2f} |")

    if not rows:
        rows.append("| 1 | No trades yet | 0 | $0.00 | $0.00 |")

    section = f'''## 📈 Profile Stock Exchange

<div align="center">
  <img src="market_chart.svg" alt="Market price chart" width="100%" />
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
    <td><strong>Trend</strong></td>
    <td>{trend}</td>
  </tr>
</table>

<div align="center">
  <a href="{links['buy']}"><img src="https://img.shields.io/badge/BUY_1_SHARE-10B981?style=for-the-badge&logo=trending-up&logoColor=white" alt="Buy 1 Share" /></a>
  <a href="{links['sell']}"><img src="https://img.shields.io/badge/SELL_1_SHARE-F43F5E?style=for-the-badge&logo=trending-down&logoColor=white" alt="Sell 1 Share" /></a>
</div>

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
        output = "No trade executed. Use a title like 'market: BUY' or 'market: SELL'."
    else:
        success, detail = execute_trade(state, action, username)
        if success:
            save_state(state)
            with CHART_FILE.open("w", encoding="utf-8") as handle:
                handle.write(generate_chart_svg(state.get("price_history", [1.0])))
            update_readme(build_readme_section(state, get_repo_slug()))
            output = f"{detail}\nUser: @{username}\nCurrent price: ${state['current_price']:.2f}\nTotal volume: {state['total_volume']}"
        else:
            output = detail

    print(output)


if __name__ == "__main__":
    main()
