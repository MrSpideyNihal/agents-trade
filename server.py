"""angel-trader: Angel One SmartAPI MCP server.

Live index option data, budget-fitted option candidates, and a two-step
order flow: preview_order -> (user says yes) -> place_order(confirm_code).
DRY-RUN by default. Set LIVE_TRADING=1 in .env to send real orders.
"""
import datetime as dt
import json
import math
import os
import secrets
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from SmartApi import SmartConnect

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

IST = ZoneInfo("Asia/Kolkata")
LIVE = os.getenv("LIVE_TRADING", "0") == "1"
MAX_ORDER_VALUE = float(os.getenv("MAX_ORDER_VALUE", "5000"))   # rupees per order
MAX_LOTS = int(os.getenv("MAX_LOTS", "1"))
MAX_ORDERS_PER_DAY = int(os.getenv("MAX_ORDERS_PER_DAY", "3"))
CONFIRM_TTL = int(os.getenv("CONFIRM_TTL_SECONDS", "120"))
ALLOWED = {s.strip().upper() for s in os.getenv("ALLOWED_UNDERLYINGS", "NIFTY,BANKNIFTY").split(",")}

SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SCRIP_CACHE = HERE / "scripmaster.json"
STATE_FILE = HERE / "state.json"
INDEX_TOKENS = {"NIFTY": "99926000", "BANKNIFTY": "99926009"}  # NSE index tokens

mcp = FastMCP("angel-trader")
_session = {"api": None, "at": 0.0}
_scrip = {"day": None, "rows": []}
_pending: dict[str, dict] = {}


# ---------- plumbing ----------

def api() -> SmartConnect:
    if _session["api"] and time.time() - _session["at"] < 6 * 3600:
        return _session["api"]
    s = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    r = s.generateSession(
        os.environ["ANGEL_CLIENT_CODE"],
        os.environ["ANGEL_PIN"],
        pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now(),
    )
    if not r or not r.get("status"):
        raise RuntimeError(f"Angel One login failed: {(r or {}).get('message')}")
    _session.update(api=s, at=time.time())
    return s


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def market_open() -> bool:
    n = now_ist()
    return n.weekday() < 5 and dt.time(9, 15) <= n.time() <= dt.time(15, 30)  # holidays: broker rejects


def tick(p: float) -> float:
    return round(round(p / 0.05) * 0.05, 2)


def option_rows(underlying: str) -> list[dict]:
    today = now_ist().date()
    if _scrip["day"] != today:
        fresh = SCRIP_CACHE.exists() and dt.date.fromtimestamp(SCRIP_CACHE.stat().st_mtime) == today
        if not fresh:
            SCRIP_CACHE.write_bytes(requests.get(SCRIP_URL, timeout=120).content)
        rows = json.loads(SCRIP_CACHE.read_bytes())
        _scrip["rows"] = [r for r in rows if r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "OPTIDX"]
        _scrip["day"] = today
    return [r for r in _scrip["rows"] if r["name"] == underlying]


def expiry_of(r: dict) -> dt.date:
    return dt.datetime.strptime(r["expiry"], "%d%b%Y").date()


def strike_of(r: dict) -> float:
    return float(r["strike"]) / 100  # scrip master stores strike x100


def quotes(exch: str, tokens: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(tokens), 50):  # API limit: 50 tokens per call
        r = api().getMarketData("FULL", {exch: tokens[i:i + 50]})
        for q in ((r or {}).get("data") or {}).get("fetched", []):
            out[str(q["symbolToken"])] = q
    return out


def best(q: dict, side: str) -> float | None:
    lvl = ((q.get("depth") or {}).get(side) or [{}])[0]
    p = lvl.get("price")
    return float(p) if p else None


def load_state() -> dict:
    today = now_ist().date().isoformat()
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if st.get("day") != today:
        st = {"day": today, "orders": []}
    return st


def check_underlying(u: str) -> str:
    u = u.upper()
    if u not in ALLOWED or u not in INDEX_TOKENS:
        raise ValueError(f"{u} not allowed. Allowed: {sorted(ALLOWED & INDEX_TOKENS.keys())}")
    return u


def chain(underlying: str, width: int, expiry: str | None):
    u = check_underlying(underlying)
    idx = quotes("NSE", [INDEX_TOKENS[u]])[INDEX_TOKENS[u]]
    spot = float(idx["ltp"])
    rows = option_rows(u)
    today = now_ist().date()
    exps = sorted({expiry_of(r) for r in rows if expiry_of(r) >= today})
    exp = dt.datetime.strptime(expiry, "%d%b%Y").date() if expiry else exps[0]
    rows = [r for r in rows if expiry_of(r) == exp]
    strikes = sorted({strike_of(r) for r in rows})
    atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    keep = set(strikes[max(0, i - width): i + width + 1])
    rows = [r for r in rows if strike_of(r) in keep]
    qs = quotes("NFO", [r["token"] for r in rows])
    return u, idx, spot, exp, atm, rows, qs


# ---------- read tools ----------

@mcp.tool()
def get_spot(underlying: str = "NIFTY") -> dict:
    """Live index level, day open/high/low and previous close."""
    u = check_underlying(underlying)
    q = quotes("NSE", [INDEX_TOKENS[u]])[INDEX_TOKENS[u]]
    prev = float(q["close"])
    return {"underlying": u, "ltp": q["ltp"], "open": q["open"], "high": q["high"], "low": q["low"],
            "prev_close": prev, "change_pct": round((float(q["ltp"]) - prev) / prev * 100, 2),
            "market_open": market_open(), "time_ist": now_ist().strftime("%H:%M:%S")}


@mcp.tool()
def get_option_chain(underlying: str = "NIFTY", strikes_each_side: int = 5, expiry: str | None = None) -> dict:
    """Option chain around ATM. expiry like '28OCT2026'; default nearest expiry."""
    u, _, spot, exp, atm, rows, qs = chain(underlying, strikes_each_side, expiry)
    table = {}
    for r in rows:
        q = qs.get(r["token"], {})
        table.setdefault(strike_of(r), {})[r["symbol"][-2:]] = {
            "token": r["token"], "symbol": r["symbol"], "ltp": q.get("ltp"),
            "bid": best(q, "buy"), "ask": best(q, "sell"), "oi": q.get("opnInterest"), "volume": q.get("tradeVolume")}
    return {"underlying": u, "spot": spot, "atm": atm, "expiry": exp.isoformat(),
            "lot_size": int(rows[0]["lotsize"]) if rows else None,
            "chain": [{"strike": k, **v} for k, v in sorted(table.items())]}


@mcp.tool()
def suggest_option_trades(budget: float, underlying: str = "NIFTY", view: str = "auto", expiry: str | None = None) -> dict:
    """Option BUY candidates whose full lot cost fits the budget (and server caps).
    view: 'bullish' (CE), 'bearish' (PE) or 'auto' (both). Returns data only - not a prediction."""
    u, idx, spot, exp, atm, rows, qs = chain(underlying, 6, expiry)
    cap = min(budget, MAX_ORDER_VALUE)
    want = {"bullish": {"CE"}, "bearish": {"PE"}}.get(view.lower(), {"CE", "PE"})
    out = []
    for r in rows:
        kind = r["symbol"][-2:]
        q = qs.get(r["token"])
        if kind not in want or not q or not q.get("ltp"):
            continue
        ltp, lot = float(q["ltp"]), int(r["lotsize"])
        ask = best(q, "sell") or ltp
        bid = best(q, "buy") or ltp
        lots = min(MAX_LOTS, math.floor(cap / (ask * lot))) if ask > 0 else 0
        if lots < 1:
            continue
        k = strike_of(r)
        out.append({
            "symbol": r["symbol"], "token": r["token"], "type": kind, "strike": k,
            "moneyness": "ATM" if k == atm else ("ITM" if (k < spot) == (kind == "CE") else "OTM"),
            "ltp": ltp, "bid": bid, "ask": ask,
            "spread_pct": round((ask - bid) / ltp * 100, 2),
            "oi": q.get("opnInterest"), "volume": q.get("tradeVolume"),
            "lot_size": lot, "lots": lots, "cost": round(ask * lot * lots, 2),
            "max_loss": round(ask * lot * lots, 2),
            "default_stop": tick(ask * 0.70), "default_target": tick(ask * 1.50),
            "illiquid": (ask - bid) / ltp > 0.03 or not q.get("tradeVolume"),
        })
    out.sort(key=lambda c: (c["illiquid"], abs(c["strike"] - atm)))
    days = (exp - now_ist().date()).days
    return {"underlying": u, "spot": spot, "atm": atm, "expiry": exp.isoformat(), "days_to_expiry": days,
            "day_change_pct": round((spot - float(idx["close"])) / float(idx["close"]) * 100, 2),
            "budget_used_for_sizing": cap, "candidates": out[:8],
            "notes": ["Stop/target are mechanical defaults (-30%/+50% of premium), not analysis.",
                      "Max loss on a bought option = full premium paid.",
                      "Expiry-day premiums decay fast." if days <= 1 else "Theta decay accelerates near expiry."]}


@mcp.tool()
def get_account() -> dict:
    """Available cash, open positions and today's orders."""
    a = api()
    funds = (a.rmsLimit() or {}).get("data") or {}
    pos = (a.position() or {}).get("data") or []
    book = (a.orderBook() or {}).get("data") or []
    return {"available_cash": funds.get("availablecash"), "net": funds.get("net"),
            "positions": [{k: p.get(k) for k in ("tradingsymbol", "symboltoken", "netqty", "avgnetprice", "ltp", "pnl")} for p in pos],
            "orders": [{k: o.get(k) for k in ("orderid", "tradingsymbol", "transactiontype", "quantity", "price", "status", "text")} for o in book],
            "mode": "LIVE" if LIVE else "DRY-RUN", "orders_placed_today": len(load_state()["orders"])}


# ---------- order tools ----------

@mcp.tool()
def preview_order(token: str, side: str = "BUY", lots: int = 1, limit_price: float | None = None,
                  product: str = "INTRADAY") -> dict:
    """Validate an index option order and return a summary + confirm_code.
    Nothing is sent. Show the summary to the user and only call place_order after they say yes.
    side BUY opens a long option; SELL only closes an existing long. product: INTRADAY or CARRYFORWARD."""
    side, product = side.upper(), product.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if product not in ("INTRADAY", "CARRYFORWARD"):
        raise ValueError("product must be INTRADAY or CARRYFORWARD")
    if not 1 <= lots <= MAX_LOTS:
        raise ValueError(f"lots must be 1..{MAX_LOTS} (MAX_LOTS)")
    if not market_open():
        raise ValueError("Market closed (09:15-15:30 IST, Mon-Fri).")
    if len(load_state()["orders"]) >= MAX_ORDERS_PER_DAY:
        raise ValueError(f"Daily order cap reached ({MAX_ORDERS_PER_DAY}).")

    row = next((r for u in ALLOWED & INDEX_TOKENS.keys() for r in option_rows(u) if r["token"] == str(token)), None)
    if not row:
        raise ValueError("Token is not an allowed index option.")
    lot = int(row["lotsize"])
    qty = lots * lot
    q = quotes("NFO", [row["token"]])[row["token"]]
    ltp = float(q["ltp"])
    ref = (best(q, "sell") if side == "BUY" else best(q, "buy")) or ltp
    price = tick(limit_price if limit_price else ref)
    warnings = []
    if abs(price - ltp) / ltp > 0.05:
        warnings.append(f"Limit {price} is >5% away from LTP {ltp}.")

    if side == "SELL":
        held = sum(int(p.get("netqty") or 0) for p in ((api().position() or {}).get("data") or [])
                   if str(p.get("symboltoken")) == row["token"])
        if held < qty:
            raise ValueError(f"SELL only closes longs. Held {held}, requested {qty}. No naked selling.")
    else:
        value = price * qty
        if value > MAX_ORDER_VALUE:
            raise ValueError(f"Order value {value:.2f} > MAX_ORDER_VALUE {MAX_ORDER_VALUE}.")
        cash = float(((api().rmsLimit() or {}).get("data") or {}).get("availablecash") or 0)
        if value > cash:
            raise ValueError(f"Order value {value:.2f} > available cash {cash:.2f}.")

    code = secrets.token_hex(3).upper()
    _pending[code] = {
        "expires": time.time() + CONFIRM_TTL,
        "params": {"variety": "NORMAL", "tradingsymbol": row["symbol"], "symboltoken": row["token"],
                   "transactiontype": side, "exchange": "NFO", "ordertype": "LIMIT", "producttype": product,
                   "duration": "DAY", "price": str(price), "quantity": str(qty),
                   "squareoff": "0", "stoploss": "0"},
    }
    return {"confirm_code": code, "expires_in_s": CONFIRM_TTL, "mode": "LIVE" if LIVE else "DRY-RUN",
            "symbol": row["symbol"], "side": side, "lots": lots, "quantity": qty, "limit_price": price,
            "ltp": ltp, "value": round(price * qty, 2), "product": product,
            "max_loss_if_buy": round(price * qty, 2) if side == "BUY" else None, "warnings": warnings}


@mcp.tool()
def place_order(confirm_code: str) -> dict:
    """Send the exact order from preview_order. Only call after the user explicitly approved
    that preview in chat. Codes are single-use and expire."""
    p = _pending.pop(confirm_code.upper(), None)
    if not p:
        raise ValueError("Unknown or already-used confirm_code. Run preview_order again.")
    if time.time() > p["expires"]:
        raise ValueError("confirm_code expired. Prices moved - run preview_order again.")
    if not market_open():
        raise ValueError("Market closed.")
    st = load_state()
    if len(st["orders"]) >= MAX_ORDERS_PER_DAY:
        raise ValueError(f"Daily order cap reached ({MAX_ORDERS_PER_DAY}).")

    params = p["params"]
    if not LIVE:
        result = {"status": "DRY-RUN", "would_send": params}
    else:
        a = api()
        resp = a.placeOrderFullResponse(params) if hasattr(a, "placeOrderFullResponse") else {"data": {"orderid": a.placeOrder(params)}}
        oid = ((resp or {}).get("data") or {}).get("orderid")
        if not oid:
            raise RuntimeError(f"Order rejected: {(resp or {}).get('message')}")
        result = {"status": "SENT", "order_id": oid, "params": params,
                  "note": "SENT != filled. Check get_account for fill status."}
    st["orders"].append({"t": now_ist().isoformat(timespec="seconds"), **result})
    STATE_FILE.write_text(json.dumps(st, indent=2))
    return result


@mcp.tool()
def cancel_order(order_id: str) -> dict:
    """Cancel an open order by id."""
    if not LIVE:
        return {"status": "DRY-RUN", "would_cancel": order_id}
    return api().cancelOrder(order_id, "NORMAL") or {}


if __name__ == "__main__":
    mcp.run()
