"""trade.py - command-line buy / sell / exit for Angel One index options.

Built on top of server.py's helpers and guardrails (same MAX_ORDER_VALUE, MAX_LOTS,
market-hours and "SELL only closes a long" rules). Two-step by design:

  * Without --yes  -> PREVIEW only. Nothing is sent. Shows exactly what would go.
  * With --yes but no --live (or LIVE_TRADING!=1) -> DRY-RUN. Prints the order, sends nothing.
  * With --yes AND --live AND LIVE_TRADING=1 -> sends a REAL order to the broker.

Examples:
  python trade.py buy  --symbol NIFTY27OCT2622250PE --amount 5000           # preview
  python trade.py buy  --symbol NIFTY27OCT2622250PE --amount 5000 --yes --live
  python trade.py exit --symbol NIFTY27OCT2622250PE --yes --live            # close whole long
  python trade.py sell --token 51319 --lots 1 --yes --live                  # close 1 lot
  python trade.py positions                                                 # list open longs
"""
import argparse
import math
import sys
import importlib.util
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("angelsrv", HERE / "server.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def find_row(symbol: str | None, token: str | None) -> dict:
    """Resolve an allowed index-option row by exact symbol or token."""
    allowed = m.ALLOWED & m.INDEX_TOKENS.keys()
    for u in allowed:
        for r in m.option_rows(u):
            if (token and str(r["token"]) == str(token)) or (symbol and r["symbol"].upper() == symbol.upper()):
                return r
    raise SystemExit(f"Not an allowed index option: {symbol or token}. Allowed underlyings: {sorted(allowed)}")


def held_qty(token: str) -> int:
    pos = (m.api().position() or {}).get("data") or []
    return sum(int(p.get("netqty") or 0) for p in pos if str(p.get("symboltoken")) == str(token))


def send(params: dict, yes: bool, live: bool) -> None:
    """Final guard + placement. Sends to broker ONLY when yes & live & LIVE_TRADING=1."""
    really_live = yes and live and m.LIVE
    if not yes:
        print("\nPREVIEW ONLY - nothing sent. Re-run with --yes (dry-run) or --yes --live (real).")
        return
    if not really_live:
        reason = "no --live flag" if not live else "LIVE_TRADING != 1 in .env"
        print(f"\nDRY-RUN ({reason}) - nothing sent. would_send:")
        for k, v in params.items():
            print(f"    {k}: {v}")
        return
    a = m.api()
    resp = a.placeOrderFullResponse(params) if hasattr(a, "placeOrderFullResponse") else {"data": {"orderid": a.placeOrder(params)}}
    oid = ((resp or {}).get("data") or {}).get("orderid")
    if not oid:
        raise SystemExit(f"Order REJECTED: {(resp or {}).get('message')}")
    print(f"\nSENT [LIVE]  order_id={oid}  (SENT != filled - verify with: python trade.py positions)")


def build_params(row: dict, side: str, qty: int, price: float, product: str) -> dict:
    return {"variety": "NORMAL", "tradingsymbol": row["symbol"], "symboltoken": str(row["token"]),
            "transactiontype": side, "exchange": "NFO", "ordertype": "LIMIT", "producttype": product,
            "duration": "DAY", "price": str(price), "quantity": str(qty), "squareoff": "0", "stoploss": "0"}


def preflight() -> None:
    if not m.market_open():
        raise SystemExit("Market closed (09:15-15:30 IST, Mon-Fri). No orders.")
    if len(m.load_state()["orders"]) >= m.MAX_ORDERS_PER_DAY:
        raise SystemExit(f"Daily order cap reached ({m.MAX_ORDERS_PER_DAY}) in server state.json.")


def cmd_buy(args) -> None:
    preflight()
    row = find_row(args.symbol, args.token)
    lot = int(row["lotsize"])
    q = m.quotes("NFO", [str(row["token"])])[str(row["token"])]
    ltp = float(q["ltp"])
    ask = m.best(q, "sell") or ltp
    price = m.tick(args.limit if args.limit else ask)
    cap = min(args.amount, m.MAX_ORDER_VALUE)
    lots = min(m.MAX_LOTS, math.floor(cap / (price * lot))) if price > 0 else 0
    if lots < 1:
        raise SystemExit(f"Amount Rs{args.amount} too small: 1 lot of {row['symbol']} costs ~Rs{price*lot:.0f} (cap Rs{m.MAX_ORDER_VALUE}).")
    qty = lots * lot
    value = round(price * qty, 2)
    if value > m.MAX_ORDER_VALUE:
        raise SystemExit(f"Order value {value} > MAX_ORDER_VALUE {m.MAX_ORDER_VALUE}.")
    cash = float(((m.api().rmsLimit() or {}).get("data") or {}).get("availablecash") or 0)
    if value > cash:
        raise SystemExit(f"Order value {value} > available cash {cash:.2f}.")
    print(f"BUY  {row['symbol']}  (token {row['token']})")
    print(f"  mode {'LIVE' if m.LIVE else 'DRY-RUN(env)'} | lots {lots} x {lot} = qty {qty} | LTP {ltp} ask {ask}")
    print(f"  LIMIT {price} | value/max-loss Rs{value} | product {args.product}")
    if abs(price - ltp) / ltp > 0.05:
        print(f"  WARNING: limit {price} is >5% from LTP {ltp}.")
    send(build_params(row, "BUY", qty, price, args.product), args.yes, args.live)


def cmd_sell(args, close_all: bool) -> None:
    preflight()
    row = find_row(args.symbol, args.token)
    lot = int(row["lotsize"])
    have = held_qty(str(row["token"]))
    if have <= 0:
        raise SystemExit(f"No open long in {row['symbol']} to close (held {have}). SELL only closes longs.")
    qty = have if close_all else min(have, args.lots * lot)
    if qty <= 0:
        raise SystemExit(f"Nothing to sell (held {have}, requested {args.lots} lots).")
    q = m.quotes("NFO", [str(row["token"])])[str(row["token"])]
    ltp = float(q["ltp"])
    bid = m.best(q, "buy") or ltp
    price = m.tick(args.limit if args.limit else bid)
    print(f"SELL {row['symbol']}  (token {row['token']})  [{'EXIT all' if close_all else 'partial'}]")
    print(f"  mode {'LIVE' if m.LIVE else 'DRY-RUN(env)'} | closing qty {qty} of held {have} | LTP {ltp} bid {bid}")
    print(f"  LIMIT {price} | value Rs{round(price*qty,2)} | product {args.product}")
    send(build_params(row, "SELL", qty, price, args.product), args.yes, args.live)


def cmd_positions(args) -> None:
    a = m.api()
    pos = (a.position() or {}).get("data") or []
    funds = (a.rmsLimit() or {}).get("data") or {}
    print(f"cash {funds.get('availablecash')} | net {funds.get('net')} | mode {'LIVE' if m.LIVE else 'DRY-RUN'}")
    rows = [p for p in pos if int(p.get("netqty") or 0) != 0]
    if not rows:
        print("No open positions (flat).")
        return
    for p in rows:
        print(f"  {p.get('tradingsymbol')} qty {p.get('netqty')} avg {p.get('avgnetprice')} ltp {p.get('ltp')} pnl {p.get('pnl')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Angel One index-option buy/sell/exit CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("buy", "sell", "exit"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", help="e.g. NIFTY27OCT2622250PE")
        p.add_argument("--token", help="symbol token, e.g. 51319")
        p.add_argument("--amount", type=float, default=0, help="(buy) rupees to spend; sized to lots within caps")
        p.add_argument("--lots", type=int, default=1, help="(sell) lots to close; ignored by exit")
        p.add_argument("--limit", type=float, default=None, help="override limit price (default best ask/bid)")
        p.add_argument("--product", default="INTRADAY", choices=["INTRADAY", "CARRYFORWARD"])
        p.add_argument("--yes", action="store_true", help="execute (dry-run unless --live too)")
        p.add_argument("--live", action="store_true", help="send a REAL order (needs LIVE_TRADING=1)")
    sub.add_parser("positions")
    args = ap.parse_args()
    if args.cmd in ("buy", "sell", "exit") and not (args.symbol or args.token):
        raise SystemExit("Provide --symbol or --token.")
    if args.cmd == "buy":
        cmd_buy(args)
    elif args.cmd == "sell":
        cmd_sell(args, close_all=False)
    elif args.cmd == "exit":
        cmd_sell(args, close_all=True)
    elif args.cmd == "positions":
        cmd_positions(args)


if __name__ == "__main__":
    main()
