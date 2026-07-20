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
    width, height = 900, 280
    padding = 40
    if len(price_history) < 2:
        price_history = price_history + [price_history[-1]]

    min_price = min(price_history) - 0.5
    max_price = max(price_history) + 0.5
    min_price = max(1.0, min_price)
    if max_price <= min_price:
        max_price = min_price + 1.0

    points = []
    for index, value in enumerate(price_history):
        x = padding + (index / max(1, len(price_history) - 1)) * (width - padding * 2)
        y = height - padding - ((value - min_price) / (max_price - min_price)) * (height - padding * 2)
        points.append((x, y))

    path_data = ""
    for idx, (x, y) in enumerate(points):
        command = "M" if idx == 0 else "L"
        path_data += f"{command}{x:.2f},{y:.2f} "

    grid_lines = []
    for step in range(5):
        y = padding + (step / 4) * (height - padding * 2)
        grid_lines.append(f'<line x1="{padding}" y1="{y:.2f}" x2="{width - padding}" y2="{y:.2f}" stroke="#1f3d34" stroke-width="1"/>')

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="280" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#07140f" rx="20"/>
  <rect x="20" y="20" width="860" height="240" rx="16" fill="#0c1f17" stroke="#1f3d34"/>
  {''.join(grid_lines)}
  <path d="{path_data.strip()}" fill="none" stroke="#34d399" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{path_data.strip()}" fill="none" stroke="#6ee7b7" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.45"/>
  <circle cx="{points[-1][0]:.2f}" cy="{points[-1][1]:.2f}" r="7" fill="#ecfdf5" stroke="#34d399" stroke-width="3"/>
  <text x="{padding}" y="{height - 10}" fill="#86efac" font-family="Segoe UI, Arial, sans-serif" font-size="14">Price trend</text>
</svg>
'''
    return svg


def build_readme_section(state, repo_slug):
    current_price = float(state.get("current_price", 1.0))
    previous_price = float(state.get("price_history", [1.0])[-2]) if len(state.get("price_history", [1.0])) > 1 else current_price
    if previous_price <= 0:
        previous_price = current_price
    change_pct = 0.0 if previous_price == current_price else round(((current_price - previous_price) / previous_price) * 100, 2)
    links = build_issue_links(repo_slug)
    leaderboard = build_leaderboard(state, current_price)

    rows = []
    for index, (username, shares, avg_cost, total_pnl) in enumerate(leaderboard, start=1):
        rows.append(f"| {index} | @{username} | {shares} | ${avg_cost:.2f} | ${total_pnl:.2f} |")

    if not rows:
        rows.append("| 1 | No trades yet | 0 | $0.00 | $0.00 |")

    section = f'''## Profile Stock Exchange

Welcome to the automated market exchange for this profile.

### Live Market Snapshot
- Current stock price: ${current_price:.2f}
- 24h change: {change_pct:+.2f}%
- Total volume: {int(state.get('total_volume', 0))}

![Market Chart](market_chart.svg)

### Trade Now
- [BUY 1 SHARE]({links['buy']})
- [SELL 1 SHARE]({links['sell']})

### Top 10 Shareholders & Profit Leaderboard
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
