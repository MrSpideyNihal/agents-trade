# angel-trader

A disciplined, **Claude-assisted intraday options trading system** for NIFTY/BANKNIFTY —
live index data, break-detection with full trade cards (entry/stop/target/R:R), position
management, and hard discipline rules. Claude finds and manages the trade; **you place every
order yourself.** Claude never sends a live order.

Built and refined over real trading days. The planned trades were green; the one red day came
from chasing. This system exists so that doesn't happen again.

---

## What's in the box

| File | Role |
|---|---|
| `server.py` | Angel SmartAPI layer: live quotes, option chain, budget-fitted candidates, and a guarded preview→place order flow (MCP server). |
| `SKILL.md` | The methodology for Claude: the workflow, the TRADE CARD format, and the **hard rules** (2 trades/day, confirmed break only, no revenge/zero-hero). Upload as a skill. |
| `signal_engine.py` | Background monitor: scans both indices for a **confirmed break**, emits a full trade card, and once you're in a position, pings Claude at decision moments with **net** P&L. Read-only. |
| `trade.py` | CLI to size / validate / dry-run an order (buy/sell/exit). Not used for live orders. |
| `.env` | Your Angel API creds + risk caps. Never commit it. |

## The hard rules (the actual edge)

1. **Max 2 trades/day.** After trade 2, done — win or lose.
2. **Proper setup only** — entry, stop, target decided *before* you buy.
3. **Confirmed break only** — a candle *closes* past the level with momentum. No wicks, no front-running.
4. **No rush / no FOMO / no revenge.** Never fund up or buy a cheap far-OTM "zero-hero" to win a loss back.
5. **Respect the stop, book the target.** No averaging, no flipping, no "a bit more."
6. **Don't fight a divergence** (NIFTY vs BANKNIFTY disagreeing).
7. **Claude never places live orders** — you click buy/sell in the Angel web console yourself.

> The edge is discipline, not prediction. Most of the day, the answer is **no trade.**

## Setup (PowerShell, inside the angel-trader folder)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env   # fill ANGEL_* creds; keep LIVE_TRADING=0 until you trust it
```

Add to `mcpServers` in `claude_desktop_config.json` (replace `<FOLDER>`):

```json
"angel-trader": {
  "command": "<FOLDER>\\.venv\\Scripts\\python.exe",
  "args": ["<FOLDER>\\server.py"]
}
```

Restart Claude Desktop and upload `SKILL.md` as a skill (zip the folder containing it).

## How to trade with it (a session)

1. **"Start the day"** — Claude scans the market, notes the opening range, arms the engine:
   ```
   python signal_engine.py        # run under a background monitor; re-arm ~every 30 min
   ```
2. **Wait.** Mid-range = no trade. Claude pre-stages the strike tickets for both directions.
3. **A break fires** → the engine emits a TRADE CARD; Claude vets it and gives you the GO:
   ```
   🟢 TRADE SETUP — NIFTY BULLISH
   Trigger : NIFTY closed above 22691 with momentum ✓
   Buy     : NIFTY03NOV2622900CE tok 53836 @ ~80 → ₹5200 (fits cash)
   Stop    : 60 (-25%) → exit ≈ -₹1300 net
   Target  : 115 (+45%) → exit ≈ +₹2275 net
   R:R     : 1:1.7 | place BUY 1 lot in web console
   ```
4. **You place it** in the Angel web console (browser), set the stop as an SL order.
5. **Claude manages the exit** — pings you to book the target or cut the stop.
6. **After:** confirm the net result, tick the trade count. Trade 2 done → stop for the day.

## Why orders are manual

Angel SmartAPI blocks order placement from an unregistered IP (`AG7002`), and a hotspot hands
you a rotating IP — so API orders keep getting rejected. The **web console uses your browser
login (no IP check)**, so you place there. `trade.py` stays your sizing/validation tool.

Also: **charges ≈ ₹50 per round-trip** — always think net, never scalp tiny moves.

## Server-side guards (on the MCP order path)

| Guard | Default |
|---|---|
| Dry-run unless `LIVE_TRADING=1` | on |
| Max order value | ₹5000 |
| Max lots | 1 |
| Max orders/day | 3 |
| Underlyings | NIFTY, BANKNIFTY index options only |
| SELL | only closes an existing long (no writing) |
| Market hours | 09:15–15:30 IST, Mon–Fri |

Run DRY-RUN first. This is rule-based tooling, **not financial advice** — every decision is yours.
