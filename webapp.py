"""webapp.py — angel-trader Web Dashboard, Auto-trade Engine & Easy Setup.

A premium web UI that sits on top of the existing angel-trader system:
  1. DASHBOARD — live P&L, open positions, trade history, signal log.
  2. AUTO-TRADE — when enabled + LIVE_TRADING=1, places orders on confirmed breaks.
  3. SETUP — web form to enter Angel One creds → auto-generates .env.

Does NOT modify server.py, signal_engine.py, or trade.py.

Usage:  python webapp.py          # opens at http://localhost:8501
"""
import asyncio
import datetime as dt
import importlib.util
import json
import logging
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
import uvicorn

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
PORT = int(os.getenv("WEBAPP_PORT", "8501"))

# Silence chatty loggers
for _n in ("SmartApi", "smartConnect", "smartapi", ""):
    logging.getLogger(_n).setLevel(logging.CRITICAL)
try:
    import logzero; logzero.loglevel(logging.CRITICAL)
except Exception:
    pass

# ---------- import server.py dynamically (like signal_engine does) ----------
_spec = importlib.util.spec_from_file_location("angelsrv", HERE / "server.py")
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)

suggest_fn = getattr(srv.suggest_option_trades, "fn", srv.suggest_option_trades)

# ---------- signal engine config (duplicated so we don't trigger its main loop) ----------
RISK          = os.getenv("RISK_PROFILE", "medium")
BUDGET        = float(os.getenv("BUDGET", "6000"))
CHG_MIN       = float(os.getenv("CHG_MIN", "0.4"))
CHARGES       = float(os.getenv("CHARGES", "50"))
POLL          = int(os.getenv("POLL_SECONDS", "30"))
SENSOR_CD     = int(os.getenv("SENSOR_CD", "240"))
EOD_WARN      = dt.time(14, 50)

RISK_PROFILES = {
    "low":    {"otm_rank": 0, "stop": 0.20, "target": 0.30, "desc": "closest-to-ATM, tight stop"},
    "medium": {"otm_rank": 0, "stop": 0.25, "target": 0.45, "desc": "near-the-move, balanced"},
    "high":   {"otm_rank": 2, "stop": 0.35, "target": 0.80, "desc": "further OTM, bigger swings"},
}
_P = RISK_PROFILES.get(RISK, RISK_PROFILES["medium"])
OTM_RANK, STOP_PCT, TARGET_PCT = _P["otm_rank"], _P["stop"], _P["target"]

# ---------- shared state ----------
_lock = threading.Lock()           # guards _state mutations
_api_lock = threading.Lock()       # serializes Angel API calls (engine thread + web handlers)
_cache = {}                        # tiny TTL cache so the 5s UI poll doesn't hammer Angel
_state = {
    "engine_running": False,
    "engine_thread": None,
    "autotrade_enabled": False,
    "signals": deque(maxlen=200),
    "trades": [],
    "last_scan": None,
    "errors": deque(maxlen=50),
    "stop_event": threading.Event(),
}


def _cached(key, ttl, producer):
    """Return producer() but reuse a value younger than `ttl` seconds. Serialized via _api_lock."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _api_lock:
        hit = _cache.get(key)          # re-check: another thread may have just filled it
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = producer()
        _cache[key] = (time.time(), val)
        return val


def _now():
    return dt.datetime.now(IST)


def _log(kind, msg, **extra):
    entry = {
        "ts": _now().strftime("%H:%M:%S"),
        "ts_full": _now().isoformat(timespec="seconds"),
        "kind": kind,
        "msg": msg,
        **extra,
    }
    with _lock:
        _state["signals"].appendleft(entry)
    return entry


# ---------- signal engine helpers (from signal_engine.py, no modification) ----------

def _pick_strike(underlying, view):
    # Cap to the broker order-value limit so auto-trade never picks a strike it can't place
    # (fixes the trap where BUDGET > MAX_ORDER_VALUE made every auto order silently skip).
    cap = min(BUDGET, srv.MAX_ORDER_VALUE)
    res = suggest_fn(budget=cap, underlying=underlying, view=view)
    liq = [c for c in res["candidates"] if not c["illiquid"] and 0 < c["cost"] <= cap]
    if not liq:
        return None
    return liq[min(OTM_RANK, len(liq) - 1)]


def _build_card(underlying, direction, idx_ltp, level, cand):
    ask = cand["ask"]; qty = cand["lot_size"] * 1
    stop = srv.tick(ask * (1 - STOP_PCT)); target = srv.tick(ask * (1 + TARGET_PCT))
    cost = round(ask * qty, 2)
    loss = round((ask - stop) * qty + CHARGES, 2)
    gain = round((target - ask) * qty - CHARGES, 2)
    rr = round(gain / loss, 1) if loss > 0 else 0
    arrow = "🟢 BULLISH" if direction == "bullish" else "🔴 BEARISH"
    side = "above" if direction == "bullish" else "below"
    return {
        "arrow": arrow,
        "underlying": underlying,
        "direction": direction,
        "trigger": f"{underlying} broke {side} {level} ({idx_ltp}) with momentum",
        "symbol": cand["symbol"],
        "token": cand["token"],
        "ask": ask,
        "cost": cost,
        "budget": BUDGET,
        "stop": stop,
        "stop_pct": int(STOP_PCT * 100),
        "stop_loss": round((ask - stop) * qty, 2),
        "stop_loss_net": round(loss, 2),
        "target": target,
        "target_pct": int(TARGET_PCT * 100),
        "target_gain": round((target - ask) * qty, 2),
        "target_gain_net": round(gain, 2),
        "rr": rr,
        "spread_pct": cand["spread_pct"],
        "lot_size": qty,
    }


def _auto_place(card_data):
    """Attempt to place an order for the given trade card. Respects LIVE_TRADING."""
    symbol = card_data["symbol"]
    token = str(card_data["token"])
    qty = card_data["lot_size"]
    price = card_data["ask"]

    # Check guards
    st = srv.load_state()
    if len(st["orders"]) >= srv.MAX_ORDERS_PER_DAY:
        _log("warn", f"AUTO-TRADE: daily order cap ({srv.MAX_ORDERS_PER_DAY}) reached. Skipped.")
        return None
    value = round(price * qty, 2)
    if value > srv.MAX_ORDER_VALUE:
        _log("warn", f"AUTO-TRADE: order value ₹{value} > cap ₹{srv.MAX_ORDER_VALUE}. Skipped.")
        return None

    params = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "quantity": str(qty),
        "squareoff": "0",
        "stoploss": "0",
    }

    if not srv.LIVE:
        result = {"status": "DRY-RUN", "params": params, "value": value}
        _log("autotrade", f"DRY-RUN: would BUY {symbol} x{qty} @ ~{price} = ₹{value}", trade=result)
        with _lock:
            _state["trades"].append({
                "ts": _now().isoformat(timespec="seconds"),
                "type": "auto",
                "action": "BUY",
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "value": value,
                "status": "DRY-RUN",
                "direction": card_data["direction"],
            })
        st["orders"].append({"t": _now().isoformat(timespec="seconds"), **result})
        srv.STATE_FILE.write_text(json.dumps(st, indent=2))
        return result

    try:
        a = srv.api()
        resp = (
            a.placeOrderFullResponse(params)
            if hasattr(a, "placeOrderFullResponse")
            else {"data": {"orderid": a.placeOrder(params)}}
        )
        oid = ((resp or {}).get("data") or {}).get("orderid")
        if not oid:
            _log("error", f"AUTO-TRADE: Order rejected — {(resp or {}).get('message')}")
            return None
        result = {"status": "SENT", "order_id": oid, "params": params, "value": value}
        _log("autotrade", f"🚀 ORDER SENT: BUY {symbol} x{qty} @ ~{price} → order #{oid}", trade=result)
        with _lock:
            _state["trades"].append({
                "ts": _now().isoformat(timespec="seconds"),
                "type": "auto",
                "action": "BUY",
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "value": value,
                "status": "SENT",
                "order_id": oid,
                "direction": card_data["direction"],
            })
        st["orders"].append({"t": _now().isoformat(timespec="seconds"), **result})
        srv.STATE_FILE.write_text(json.dumps(st, indent=2))
        return result
    except Exception as e:
        _log("error", f"AUTO-TRADE: placement failed — {type(e).__name__}: {e}")
        return None


def _auto_exit(tok, sym, qty, price, reason):
    """Auto-exit a position at stop/target."""
    params = {
        "variety": "NORMAL",
        "tradingsymbol": sym,
        "symboltoken": str(tok),
        "transactiontype": "SELL",
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "quantity": str(qty),
        "squareoff": "0",
        "stoploss": "0",
    }
    st = srv.load_state()
    if not srv.LIVE:
        result = {"status": "DRY-RUN", "params": params, "reason": reason}
        _log("autotrade", f"DRY-RUN EXIT: {reason} — would SELL {sym} x{qty} @ ~{price}", trade=result)
        with _lock:
            _state["trades"].append({
                "ts": _now().isoformat(timespec="seconds"),
                "type": "auto-exit",
                "action": "SELL",
                "symbol": sym,
                "qty": qty,
                "price": price,
                "status": "DRY-RUN",
                "reason": reason,
            })
        st["orders"].append({"t": _now().isoformat(timespec="seconds"), **result})
        srv.STATE_FILE.write_text(json.dumps(st, indent=2))
        return result
    try:
        a = srv.api()
        resp = (
            a.placeOrderFullResponse(params)
            if hasattr(a, "placeOrderFullResponse")
            else {"data": {"orderid": a.placeOrder(params)}}
        )
        oid = ((resp or {}).get("data") or {}).get("orderid")
        if not oid:
            _log("error", f"AUTO-EXIT: reject — {(resp or {}).get('message')}")
            return None
        result = {"status": "SENT", "order_id": oid, "reason": reason}
        _log("autotrade", f"🏁 EXIT SENT: {reason} — SELL {sym} x{qty} → order #{oid}", trade=result)
        with _lock:
            _state["trades"].append({
                "ts": _now().isoformat(timespec="seconds"),
                "type": "auto-exit",
                "action": "SELL",
                "symbol": sym,
                "qty": qty,
                "price": price,
                "status": "SENT",
                "order_id": oid,
                "reason": reason,
            })
        st["orders"].append({"t": _now().isoformat(timespec="seconds"), **result})
        srv.STATE_FILE.write_text(json.dumps(st, indent=2))
        return result
    except Exception as e:
        _log("error", f"AUTO-EXIT: failed — {type(e).__name__}: {e}")
        return None


# ---------- signal engine loop (runs in a background thread) ----------

def _engine_loop():
    from collections import deque as dq
    hist = {u: dq(maxlen=4) for u in ("NIFTY", "BANKNIFTY")}
    seen_lo, seen_hi, last_alert = {}, {}, {}
    pstate = {}
    errs = 0; warned_down = False
    IDX = [srv.INDEX_TOKENS["NIFTY"], srv.INDEX_TOKENS["BANKNIFTY"]]

    _log("engine", f"ENGINE ARMED | risk={RISK} ({_P['desc']}) | budget ₹{BUDGET} | "
         f"break ≥{CHG_MIN}% | stop -{int(STOP_PCT*100)}% / target +{int(TARGET_PCT*100)}%")

    stop_ev = _state["stop_event"]
    while not stop_ev.is_set():
        now = _now()
        with _lock:
            _state["last_scan"] = now.isoformat(timespec="seconds")

        if not srv.market_open():
            _log("engine", "MARKET CLOSED. Engine paused.")
            stop_ev.wait(60)
            continue

        try:
            # serialize Angel calls with the web handlers so we don't trip rate limits
            with _api_lock:
                nse = srv.quotes("NSE", IDX)
                held = [p for p in ((srv.api().position() or {}).get("data") or [])
                        if int(p.get("netqty") or 0) != 0 and float(p.get("avgnetprice") or 0) > 0]
                hq = srv.quotes("NFO", [str(p["symboltoken"]) for p in held]) if held else {}
                _cache["positions"] = (time.time(), (held, (srv.api().rmsLimit() or {}).get("data") or {}))
                _cache["spot"] = (time.time(), nse)

            # ---------- GUARDIAN: manage open positions ----------
            live_toks = {}
            for p in held:
                tok = str(p["symboltoken"]); sym = p["tradingsymbol"]
                qty = int(p["netqty"]); entry = float(p["avgnetprice"])
                q = hq.get(tok)
                if not q:
                    continue
                ltp = float(q["ltp"]); pct = round((ltp - entry) / entry * 100, 1)
                pnl = round((ltp - entry) * qty, 2); net = round(pnl - CHARGES, 2)
                live_toks[tok] = True
                st = pstate.setdefault(tok, {"peak": pct, "fade_peak": pct, "flags": set(), "sym": sym, "entry": entry, "qty": qty})
                st["peak"] = max(st["peak"], pct); fl = st["flags"]
                autotrade_on = _state["autotrade_enabled"]

                if "enter" not in fl:
                    fl.add("enter")
                    _log("position", f"🔵 ENTERED {sym} qty {qty} @ {entry} now {ltp} ({pct:+}%)", pnl=net)
                if pct >= TARGET_PCT * 100 and "tgt" not in fl:
                    fl.add("tgt")
                    _log("target", f"🟢 TARGET {sym} {pct:+}% (net ~₹{net}) — BOOK IT", pnl=net)
                    if autotrade_on:
                        _auto_exit(tok, sym, qty, ltp, f"TARGET HIT {pct:+}%")
                if pct <= -STOP_PCT * 100 and "stp" not in fl:
                    fl.add("stp")
                    _log("stop", f"🔴 STOP {sym} {pct:+}% (net ~₹{net}) — CUT IT", pnl=net)
                    if autotrade_on:
                        _auto_exit(tok, sym, qty, ltp, f"STOP HIT {pct:+}%")
                if st["peak"] >= 20 and pct <= st["peak"] - 13 and "fade" not in fl:
                    fl.add("fade"); st["fade_peak"] = st["peak"]
                    _log("fade", f"🟠 MOM-FADE {sym} peaked +{st['peak']}% now {pct:+}% (net ~₹{net})", pnl=net)
                if st["peak"] >= st["fade_peak"] + 10 and "fade" in fl:
                    fl.discard("fade")
                if now.time() >= EOD_WARN and "eod" not in fl:
                    fl.add("eod")
                    _log("eod", f"⏰ EOD {sym} {pct:+}% (net ~₹{net}) — flatten before close", pnl=net)
                    if autotrade_on:
                        _auto_exit(tok, sym, qty, ltp, f"EOD FLATTEN {pct:+}%")

            for tok in [t for t in pstate if t not in live_toks]:
                _log("closed", f"⚪ CLOSED {pstate[tok].get('sym', tok)} — now flat, confirm result.")
                del pstate[tok]

            # ---------- SENSOR: scan for confirmed break ----------
            for u in ("NIFTY", "BANKNIFTY"):
                q = nse.get(srv.INDEX_TOKENS[u])
                if not q:
                    continue  # token missing from a partial quote response — skip this cycle
                ltp = float(q["ltp"]); hi = float(q["high"]); lo = float(q["low"]); prev = float(q["close"])
                chg = round((ltp - prev) / prev * 100, 2)
                hist[u].append(ltp)
                if u not in seen_lo:
                    seen_lo[u], seen_hi[u] = lo, hi
                    continue
                new_low = lo < seen_lo[u] - 1e-9; new_high = hi > seen_hi[u] + 1e-9
                seen_lo[u] = min(seen_lo[u], lo); seen_hi[u] = max(seen_hi[u], hi)
                mom = round(ltp - hist[u][0], 1) if len(hist[u]) >= 2 else 0.0
                direction = None
                if new_low and chg <= -CHG_MIN and mom < 0:
                    direction, level = "bearish", round(seen_lo[u])
                elif new_high and chg >= CHG_MIN and mom > 0:
                    direction, level = "bullish", round(seen_hi[u])
                if not direction:
                    continue
                key = (u, direction)
                if time.time() - last_alert.get(key, 0) < SENSOR_CD:
                    continue
                cand = _pick_strike(u, direction)
                if not cand:
                    continue
                last_alert[key] = time.time()
                cd = _build_card(u, direction, ltp, level, cand)
                _log("signal", f"{cd['arrow']} TRADE SETUP — {u}", card=cd)

                # --- Auto-trade: place order on signal ---
                if _state["autotrade_enabled"]:
                    _auto_place(cd)

            if warned_down:
                _log("engine", "✅ CONNECTION RECOVERED — engine live again.")
            errs = 0; warned_down = False

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            transient = any(k in msg for k in (
                "ConnectionError", "Timeout", "timed out", "getaddrinfo",
                "Max retries", "NameResolution", "exceeding access rate", "DataException"))
            if transient:
                errs += 1
                if errs == 5 and not warned_down:
                    warned_down = True
                    _log("warn", f"⚠️ FEED DOWN ~{errs * POLL}s — retrying; watch positions yourself.")
                stop_ev.wait(10)
            else:
                _log("error", f"Engine error: {msg}")

        stop_ev.wait(POLL)

    with _lock:
        _state["engine_running"] = False
    _log("engine", "ENGINE STOPPED.")


# ---------- API handlers ----------

def _env_configured():
    """Check if .env has real credentials (not blank)."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return False
    text = env_file.read_text()
    for key in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"):
        for line in text.splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                if not val:
                    return False
                break
        else:
            return False
    return True


async def api_status(request):
    now = _now()
    with _lock:
        trades = list(_state["trades"])
    return JSONResponse({
        "time_ist": now.strftime("%H:%M:%S"),
        "date": now.strftime("%a %d %b %Y"),
        "market_open": srv.market_open(),
        "engine_running": _state["engine_running"],
        "autotrade_enabled": _state["autotrade_enabled"],
        "last_scan": _state["last_scan"],
        "mode": "LIVE" if srv.LIVE else "DRY-RUN",
        "env_configured": _env_configured(),
        "risk": RISK,
        "budget": BUDGET,
        "trades_today": len(trades),
        "max_trades": srv.MAX_ORDERS_PER_DAY,
    })


async def api_signals(request):
    with _lock:
        sigs = list(_state["signals"])[:100]
    return JSONResponse({"signals": sigs})


async def api_trades(request):
    with _lock:
        trades = list(_state["trades"])
    # Also load from state.json for persistence
    try:
        st = srv.load_state()
        persisted = st.get("orders", [])
    except Exception:
        persisted = []
    return JSONResponse({"trades": trades, "persisted_orders": persisted})


async def api_positions(request):
    try:
        def _producer():
            a = srv.api()
            return ((a.position() or {}).get("data") or [], (a.rmsLimit() or {}).get("data") or {})
        pos, funds = _cached("positions", 4, _producer)
        active = []
        total_pnl = 0
        for p in pos:
            qty = int(p.get("netqty") or 0)
            if qty == 0:
                continue
            entry = float(p.get("avgnetprice") or 0)
            ltp = float(p.get("ltp") or 0)
            pnl = round((ltp - entry) * qty, 2)
            net = round(pnl - CHARGES, 2)
            total_pnl += net
            active.append({
                "symbol": p.get("tradingsymbol"),
                "token": p.get("symboltoken"),
                "qty": qty,
                "entry": entry,
                "ltp": ltp,
                "pnl": pnl,
                "net_pnl": net,
                "pct": round((ltp - entry) / entry * 100, 1) if entry > 0 else 0,
            })
        return JSONResponse({
            "positions": active,
            "total_pnl": round(total_pnl, 2),
            "cash": funds.get("availablecash"),
            "net_worth": funds.get("net"),
        })
    except Exception as e:
        return JSONResponse({"positions": [], "total_pnl": 0, "error": str(e)})


async def api_engine_start(request):
    if _state["engine_running"]:
        return JSONResponse({"ok": False, "msg": "Engine already running."})
    if not _env_configured():
        return JSONResponse({"ok": False, "msg": "Configure your Angel One credentials first (Setup tab)."})
    _state["stop_event"] = threading.Event()
    t = threading.Thread(target=_engine_loop, daemon=True, name="signal-engine")
    with _lock:
        _state["engine_running"] = True
        _state["engine_thread"] = t
    t.start()
    return JSONResponse({"ok": True, "msg": "Engine started."})


async def api_engine_stop(request):
    if not _state["engine_running"]:
        return JSONResponse({"ok": False, "msg": "Engine is not running."})
    _state["stop_event"].set()
    return JSONResponse({"ok": True, "msg": "Engine stopping..."})


async def api_autotrade_toggle(request):
    body = await request.json()
    enabled = body.get("enabled", False)
    if enabled and not srv.LIVE:
        # Allow enabling in dry-run too — but warn
        pass
    _state["autotrade_enabled"] = enabled
    _log("config", f"AUTO-TRADE {'ENABLED' if enabled else 'DISABLED'} (mode: {'LIVE' if srv.LIVE else 'DRY-RUN'})")
    return JSONResponse({"ok": True, "autotrade_enabled": enabled, "mode": "LIVE" if srv.LIVE else "DRY-RUN"})


async def api_setup(request):
    """Save .env credentials from the setup form."""
    body = await request.json()
    lines = []
    for key in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"):
        val = body.get(key, "").strip()
        if not val:
            return JSONResponse({"ok": False, "msg": f"Missing {key}"}, status_code=400)
        lines.append(f"{key}={val}")

    lines.append("")
    lines.append(f"LIVE_TRADING={body.get('LIVE_TRADING', '0')}")
    lines.append(f"MAX_ORDER_VALUE={body.get('MAX_ORDER_VALUE', '5000')}")
    lines.append(f"MAX_LOTS={body.get('MAX_LOTS', '1')}")
    lines.append(f"MAX_ORDERS_PER_DAY={body.get('MAX_ORDERS_PER_DAY', '3')}")
    lines.append(f"CONFIRM_TTL_SECONDS={body.get('CONFIRM_TTL_SECONDS', '120')}")
    lines.append(f"ALLOWED_UNDERLYINGS={body.get('ALLOWED_UNDERLYINGS', 'NIFTY,BANKNIFTY')}")
    lines.append("")

    env_path = HERE / ".env"
    env_path.write_text("\n".join(lines), encoding="utf-8")

    # Reload env vars in the current process
    from dotenv import load_dotenv
    load_dotenv(env_path, override=True)

    _log("config", "✅ .env saved — restart webapp.py to apply credential changes fully.")
    return JSONResponse({"ok": True, "msg": ".env saved! Restart the webapp to apply credential changes."})


async def api_test_connection(request):
    """Test Angel One connection with current .env creds."""
    try:
        # Force a fresh login
        srv._session["api"] = None
        srv._session["at"] = 0.0
        a = srv.api()
        funds = (a.rmsLimit() or {}).get("data") or {}
        return JSONResponse({
            "ok": True,
            "msg": "Connected to Angel One!",
            "cash": funds.get("availablecash"),
            "client": os.getenv("ANGEL_CLIENT_CODE", ""),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"Connection failed: {type(e).__name__}: {e}"})


async def api_config(request):
    """Return current config (no secrets)."""
    env_file = HERE / ".env"
    has_env = env_file.exists()
    return JSONResponse({
        "has_env": has_env,
        "configured": _env_configured(),
        "LIVE_TRADING": os.getenv("LIVE_TRADING", "0"),
        "MAX_ORDER_VALUE": os.getenv("MAX_ORDER_VALUE", "5000"),
        "MAX_LOTS": os.getenv("MAX_LOTS", "1"),
        "MAX_ORDERS_PER_DAY": os.getenv("MAX_ORDERS_PER_DAY", "3"),
        "CONFIRM_TTL_SECONDS": os.getenv("CONFIRM_TTL_SECONDS", "120"),
        "ALLOWED_UNDERLYINGS": os.getenv("ALLOWED_UNDERLYINGS", "NIFTY,BANKNIFTY"),
        "ANGEL_CLIENT_CODE": os.getenv("ANGEL_CLIENT_CODE", ""),
        # Mask secrets
        "ANGEL_API_KEY": ("*" * 8 + os.getenv("ANGEL_API_KEY", "")[-4:]) if os.getenv("ANGEL_API_KEY") else "",
        "ANGEL_PIN": "****" if os.getenv("ANGEL_PIN") else "",
        "ANGEL_TOTP_SECRET": ("*" * 8 + os.getenv("ANGEL_TOTP_SECRET", "")[-4:]) if os.getenv("ANGEL_TOTP_SECRET") else "",
    })


async def api_spot(request):
    """Quick spot data for both indices (cached ~4s so the UI poll doesn't hammer Angel)."""
    try:
        IDX = [srv.INDEX_TOKENS["NIFTY"], srv.INDEX_TOKENS["BANKNIFTY"]]
        nse = _cached("spot", 4, lambda: srv.quotes("NSE", IDX))
        out = {}
        for u in ("NIFTY", "BANKNIFTY"):
            q = nse.get(srv.INDEX_TOKENS[u])
            if q:
                prev = float(q["close"])
                ltp = float(q["ltp"])
                out[u] = {
                    "ltp": ltp,
                    "open": float(q["open"]),
                    "high": float(q["high"]),
                    "low": float(q["low"]),
                    "prev_close": prev,
                    "change": round(ltp - prev, 2),
                    "change_pct": round((ltp - prev) / prev * 100, 2),
                }
        return JSONResponse({"ok": True, "indices": out})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------- HTML page ----------

async def dashboard_page(request):
    html_path = HERE / "static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------- Starlette app ----------

routes = [
    Route("/", dashboard_page),
    Route("/api/status", api_status),
    Route("/api/signals", api_signals),
    Route("/api/trades", api_trades),
    Route("/api/positions", api_positions),
    Route("/api/spot", api_spot),
    Route("/api/engine/start", api_engine_start, methods=["POST"]),
    Route("/api/engine/stop", api_engine_stop, methods=["POST"]),
    Route("/api/autotrade", api_autotrade_toggle, methods=["POST"]),
    Route("/api/setup", api_setup, methods=["POST"]),
    Route("/api/test-connection", api_test_connection, methods=["POST"]),
    Route("/api/config", api_config),
]

app = Starlette(
    debug=False,
    routes=routes,
    # Restrict to this machine's own origin — the dashboard can start the engine,
    # toggle auto-trade and read positions, so we don't let arbitrary sites call it.
    middleware=[
        Middleware(CORSMiddleware,
                   allow_origins=[f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"]),
    ],
)


if __name__ == "__main__":
    # Bind to localhost only — this page has your credential setup form and trade controls;
    # it must NOT be reachable from other devices on the network. Change deliberately if you
    # really need LAN access, and put auth in front of it first.
    host = os.getenv("WEBAPP_HOST", "127.0.0.1")
    print(f"\n  angel-trader dashboard → http://127.0.0.1:{PORT}\n")
    uvicorn.run(app, host=host, port=PORT, log_level="warning")
