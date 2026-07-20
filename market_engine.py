import sys
import json
import re
from datetime import datetime, timezone, timedelta

STATE_FILE = "market_state.json"
README_FILE = "README.md"
CHART_FILE = "market_chart.svg"

# Configuration Rules
PRICE_STEP = 0.50         # Price increases/decreases by $0.50 per trade
MIN_PRICE = 1.00          # Minimum stock price floor
MAX_DAILY_BUYS = 3        # Rule 1: Max 3 buys per user in 24 hours

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"current_price": 1.00, "total_volume": 0, "price_history": [1.00], "users": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def generate_svg_chart(history):
    """Generates a high-contrast dark mode emerald line chart SVG."""
    points = history[-20:] if len(history) >= 20 else history
    if len(points) < 2:
        points = [points[0], points[0]] if points else [1.0, 1.0]

    min_p = min(points) * 0.9
    max_p = max(points) * 1.1
    if max_p == min_p:
        max_p += 1.0

    width, height = 500, 150
    padding = 20

    # Map points to SVG coordinates
    svg_points = []
    for i, p in enumerate(points):
        x = padding + (i / (len(points) - 1)) * (width - 2 * padding)
        y = height - padding - ((p - min_p) / (max_p - min_p)) * (height - 2 * padding)
        svg_points.append(f"{x:.1f},{y:.1f}")

    path_d = "M " + " L ".join(svg_points)
    fill_d = path_d + f" L {width-padding},{height-padding} L {padding},{height-padding} Z"

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="grad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#10B981" stop-opacity="0.4" />
      <stop offset="100%" stop-color="#10B981" stop-opacity="0.0" />
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="#0D1117" rx="8"/>
  <path d="{fill_d}" fill="url(#grad)" />
  <path d="{path_d}" fill="none" stroke="#10B981" stroke-width="3" stroke-linecap="round"/>
  <circle cx="{svg_points[-1].split(',')[0]}" cy="{svg_points[-1].split(',')[1]}" r="5" fill="#10B981" />
</svg>"""

    with open(CHART_FILE, "w") as f:
        f.write(svg)

def process_trade(issue_title, actor):
    state = load_state()
    price = state.get("current_price", 1.00)
    users = state.get("users", {})
    history = state.get("price_history", [1.00])
    
    action = None
    if "BUY" in issue_title.upper():
        action = "BUY"
    elif "SELL" in issue_title.upper():
        action = "SELL"
    else:
        return "Invalid command. Issue title must contain 'BUY' or 'SELL'."

    now = datetime.now(timezone.utc)
    user_data = users.get(actor, {
        "shares": 0,
        "avg_buy_price": 0.0,
        "buys": [],
        "realized_pnl": 0.0
    })

    # Rule 1 Check: Max 3 buys per 24 hours
    if action == "BUY":
        recent_buys = [
            t for t in user_data.get("buys", [])
            if (now - datetime.fromisoformat(t)).total_seconds() < 86400
        ]
        if len(recent_buys) >= MAX_DAILY_BUYS:
            return f"⚠️ Order Rejected for @{actor}: Daily limit reached (Max 3 buys per 24h)."

        # Execute Buy
        old_shares = user_data["shares"]
        old_avg = user_data["avg_buy_price"]
        new_shares = old_shares + 1
        new_avg = ((old_shares * old_avg) + price) / new_shares

        recent_buys.append(now.isoformat())
        user_data["shares"] = new_shares
        user_data["avg_buy_price"] = new_avg
        user_data["buys"] = recent_buys

        state["current_price"] = round(price + PRICE_STEP, 2)
        state["total_volume"] += 1
        msg = f"✅ SUCCESS: @{actor} bought 1 share of $PRFL at ${price:.2f}."

    # Rule 2 Check: Must own shares to sell
    elif action == "SELL":
        if user_data["shares"] <= 0:
            return f"⚠️ Order Rejected for @{actor}: You do not own any shares to sell."

        # Execute Sell
        avg_buy = user_data["avg_buy_price"]
        profit_on_trade = price - avg_buy
        user_data["shares"] -= 1
        user_data["realized_pnl"] += profit_on_trade

        if user_data["shares"] == 0:
            user_data["avg_buy_price"] = 0.0

        state["current_price"] = round(max(MIN_PRICE, price - PRICE_STEP), 2)
        state["total_volume"] += 1
        msg = f"📉 SUCCESS: @{actor} sold 1 share of $PRFL at ${price:.2f} (Trade PnL: ${profit_on_trade:+.2f})."

    users[actor] = user_data
    state["users"] = users
    state["price_history"].append(state["current_price"])

    save_state(state)
    generate_svg_chart(state["price_history"])
    update_readme(state)
    
    return msg

def update_readme(state):
    curr_price = state["current_price"]
    users = state["users"]
    history = state["price_history"]

    # Calculate 24h change
    prev_price = history[-2] if len(history) > 1 else curr_price
    pct_change = ((curr_price - prev_price) / prev_price) * 100 if prev_price > 0 else 0.0

    # Rule 3 & 4: Top 10 Shareholders with Profit tracking
    sorted_users = sorted(users.items(), key=lambda x: x[1]["shares"], reverse=True)
    top_10 = [u for u in sorted_users if u[1]["shares"] > 0][:10]

    leaderboard_rows = ""
    if not top_10:
        leaderboard_rows = "| - | No shareholders yet | 0 | $0.00 | $0.00 |\n"
    else:
        for idx, (username, data) in enumerate(top_10, 1):
            shares = data["shares"]
            avg_buy = data["avg_buy_price"]
            unrealized_pnl = (curr_price - avg_buy) * shares
            total_pnl = unrealized_pnl + data.get("realized_pnl", 0.0)
            
            pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
            leaderboard_rows += f"| #{idx} | @{username} | {shares} | ${avg_buy:.2f} | `{pnl_str}` |\n"

    market_md = f"""
<!-- MARKET_START -->
<div align="center">

### 📈 PROFILE EXCHANGE ($PRFL)
**Current Price:** `${curr_price:.2f}` | **24h Shift:** `{pct_change:+.1f}%` | **Total Volume:** `{state['total_volume']} Trades`

<img src="market_chart.svg?v={datetime.now().timestamp()}" alt="PRFL Stock Chart" width="100%" />

<br>

[![Buy 1 Share](https://img.shields.io/badge/🟢_BUY_1_SHARE-10B981?style=for-the-badge)](https://github.com/Sabin-Basnet/Sabin-Basnet/issues/new?title=market:%20BUY) &nbsp;&nbsp; [![Sell 1 Share](https://img.shields.io/badge/🔴_SELL_1_SHARE-EF4444?style=for-the-badge)](https://github.com/Sabin-Basnet/Sabin-Basnet/issues/new?title=market:%20SELL)

*Rules: Max 3 buys/day • Must own shares to sell • Real-time bonding curve*

<br>

#### 🐳 TOP 10 SHAREHOLDERS & PROFIT LEADERBOARD

| Rank | Investor | Shares Owned | Avg Buy Price | Total Profit/Loss |
| :---: | :--- | :---: | :---: | :---: |
{leaderboard_rows}
</div>
<!-- MARKET_END -->
"""

    try:
        with open(README_FILE, "r") as f:
            content = f.read()

        pattern = r"<!-- MARKET_START -->[\s\S]*?<!-- MARKET_END -->"
        if "<!-- MARKET_START -->" in content:
            new_content = re.sub(pattern, market_md.strip(), content)
            with open(README_FILE, "w") as f:
                f.write(new_content)
    except Exception as e:
        print(f"Error updating README: {e}")

if __name__ == "__main__":
    title_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    actor_arg = sys.argv[2] if len(sys.argv) > 2 else "anonymous"
    result = process_trade(title_arg, actor_arg)
    print(result)