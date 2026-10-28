"""signal_engine.py — disciplined break-detector + position guardian for NIFTY/BANKNIFTY
index options. Runs as a background monitor on ONE Angel session.

What it does (read-only — never places or cancels orders):
  * SCANS both indices for a CONFIRMED momentum break of the day's high/low.
  * On a break, emits a full TRADE SETUP card for Claude to vet: direction, a strike
    that fits BUDGET, entry, stop (-STOP_PCT), target (+TARGET_PCT), cost and R:R.
  * Once a position exists, GUARDS it — pings Claude at decision moments (entered,
    momentum-fade, near-target, near-stop, EOD) with NET-of-charges P&L.

Design choices baked in from real trading:
  * Confirmed break = new day extreme + move >= CHG_MIN + momentum still pushing
    (filters the wick-and-reject whipsaws).
  * Thinks in NET (after ~CHARGES round-trip), never gross.
  * Silent on transient network/rate blips; warns only on a sustained outage.
  * SmartApi's chatty logging is silenced so only real signals reach the chat.

Usage:  python signal_engine.py        (run under a background monitor; re-arm ~every 30 min)
Config is the block right below — tune BUDGET / thresholds to taste.
"""
import sys, time, logging, datetime as dt, importlib.util
from collections import deque
from pathlib import Path

# ---------- config ----------
RISK          = "medium"  # low | medium | high — how the engine sizes risk/reward
BUDGET        = 6000      # rupees you'll spend per trade (picks a strike that fits)
CHG_MIN       = 0.4       # min |day move %| to call a break a real move
CHARGES       = 50        # est. round-trip brokerage+taxes; P&L is reported NET
POLL          = 30        # seconds between scans
SENSOR_CD     = 240       # seconds before re-alerting the same underlying+direction
EOD_WARN      = dt.time(14, 50)   # expiry-day flatten nudge (harmless on non-expiry)

# Risk profiles — pick the strike and the stop/target per appetite.
#   otm_rank : which candidate from the ATM-outward list (0 = closest-to-money that fits).
#   stop/target : option SL / TP as a fraction of entry.
# "medium" is the user's own style: enter near-the-move with a real stop.
RISK_PROFILES = {
    "low":    {"otm_rank": 0, "stop": 0.20, "target": 0.30, "desc": "closest-to-ATM, tight stop, modest target — most reliable"},
    "medium": {"otm_rank": 0, "stop": 0.25, "target": 0.45, "desc": "near-the-move, real stop — balanced (default)"},
    "high":   {"otm_rank": 2, "stop": 0.35, "target": 0.80, "desc": "further OTM, cheaper, bigger swings — lower hit-rate"},
}
_P = RISK_PROFILES[RISK]
OTM_RANK, STOP_PCT, TARGET_PCT = _P["otm_rank"], _P["stop"], _P["target"]
# ----------------------------

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
try:
    import logzero; logzero.loglevel(logging.CRITICAL)
except Exception:
    pass
for _n in ("SmartApi", "smartConnect", "smartapi", ""):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("angelsrv", HERE / "server.py")
m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(m)
suggest = getattr(m.suggest_option_trades, "fn", m.suggest_option_trades)

def emit(s): print(f"[{m.now_ist().strftime('%H:%M:%S')}] {s}", flush=True)
def transient(e):
    msg = f"{type(e).__name__}: {e}"
    return any(k in msg for k in ("ConnectionError", "Timeout", "timed out", "getaddrinfo",
                                  "Max retries", "NameResolution", "exceeding access rate", "DataException"))

def pick_strike(underlying, view):
    """Liquid option aligned with `view`, within BUDGET, at the risk profile's OTM rank.
    Candidates come ATM-outward, so rank 0 = closest to the money that still fits."""
    res = suggest(budget=BUDGET, underlying=underlying, view=view)
    liq = [c for c in res["candidates"] if not c["illiquid"] and 0 < c["cost"] <= BUDGET]
    if not liq:
        return None
    return liq[min(OTM_RANK, len(liq) - 1)]

def card(underlying, direction, idx_ltp, level, cand, lot_mult):
    """Build a TRADE SETUP card string with entry/stop/target/R:R, net of charges."""
    ask = cand["ask"]; qty = cand["lot_size"] * 1
    stop = m.tick(ask * (1 - STOP_PCT)); target = m.tick(ask * (1 + TARGET_PCT))
    cost = round(ask * qty, 2)
    loss = round((ask - stop) * qty + CHARGES, 2); gain = round((target - ask) * qty - CHARGES, 2)
    rr = round(gain / loss, 1) if loss > 0 else 0
    arrow = "🟢 BULLISH" if direction == "bullish" else "🔴 BEARISH"
    side = "above" if direction == "bullish" else "below"
    return ("\n".join([
        f"{arrow} TRADE SETUP — {underlying} (for CLAUDE to vet)",
        f"  Trigger : {underlying} broke {side} {level} ({idx_ltp}) with momentum",
        f"  Buy     : {cand['symbol']} tok {cand['token']} @ ~{ask} → ₹{cost} (fits ₹{BUDGET})",
        f"  Stop    : {stop} (-{int(STOP_PCT*100)}%) → exit ≈ -₹{round((ask-stop)*qty)} (net ~-₹{round(loss)})",
        f"  Target  : {target} (+{int(TARGET_PCT*100)}%) → exit ≈ +₹{round((target-ask)*qty)} (net ~+₹{round(gain)})",
        f"  R:R     : 1:{rr}  | spread {cand['spread_pct']}% | place BUY 1 lot in web console",
    ]))

# ---------- state ----------
hist = {u: deque(maxlen=4) for u in ("NIFTY", "BANKNIFTY")}
seen_lo, seen_hi, last_alert = {}, {}, {}
pstate = {}          # token -> guardian state
errs = 0; warned_down = False
IDX = [m.INDEX_TOKENS["NIFTY"], m.INDEX_TOKENS["BANKNIFTY"]]
emit(f"SIGNAL ENGINE ARMED | risk={RISK} ({_P['desc']}) | budget ₹{BUDGET} | break move≥{CHG_MIN}% | stop -{int(STOP_PCT*100)}% / target +{int(TARGET_PCT*100)}% | read-only")

while True:
    now = m.now_ist()
    if not m.market_open():
        emit("MARKET CLOSED. Engine stopping."); break
    try:
        nse = m.quotes("NSE", IDX)
        # ---------- GUARDIAN: manage any open position ----------
        held = [p for p in ((m.api().position() or {}).get("data") or [])
                if int(p.get("netqty") or 0) != 0 and float(p.get("avgnetprice") or 0) > 0]
        hq = m.quotes("NFO", [str(p["symboltoken"]) for p in held]) if held else {}
        live = {}
        for p in held:
            tok = str(p["symboltoken"]); sym = p["tradingsymbol"]; qty = int(p["netqty"]); entry = float(p["avgnetprice"])
            q = hq.get(tok)
            if not q: continue
            ltp = float(q["ltp"]); pct = round((ltp - entry) / entry * 100, 1)
            pnl = round((ltp - entry) * qty, 2); net = round(pnl - CHARGES, 2)
            live[tok] = True
            st = pstate.setdefault(tok, {"peak": pct, "fade_peak": pct, "flags": set()})
            st["peak"] = max(st["peak"], pct); fl = st["flags"]
            if "enter" not in fl:
                fl.add("enter"); emit(f"🔵 ENTERED {sym} qty {qty} @ {entry} now {ltp} ({pct:+}%) — set stop & target.")
            if pct >= TARGET_PCT * 100 and "tgt" not in fl:
                fl.add("tgt"); emit(f"🟢 TARGET {sym} {pct:+}% (gross ₹{pnl}, net ~₹{net}) — BOOK IT.")
            if pct <= -STOP_PCT * 100 and "stp" not in fl:
                fl.add("stp"); emit(f"🔴 STOP {sym} {pct:+}% (net ~₹{net}) — CUT IT.")
            if st["peak"] >= 20 and pct <= st["peak"] - 13 and "fade" not in fl:
                fl.add("fade"); st["fade_peak"] = st["peak"]
                emit(f"🟠 MOM-FADE {sym} peaked +{st['peak']}% now {pct:+}% (net ~₹{net}) — trail or book.")
            if st["peak"] >= st["fade_peak"] + 10 and "fade" in fl: fl.discard("fade")
            if now.time() >= EOD_WARN and "eod" not in fl:
                fl.add("eod"); emit(f"⏰ EOD {sym} {pct:+}% (net ~₹{net}) — flatten before close.")
        for tok in [t for t in pstate if t not in live]:
            emit(f"⚪ CLOSED {pstate[tok].get('sym', tok)} — now flat, confirm result."); del pstate[tok]
        # ---------- SENSOR: scan for a confirmed break ----------
        for u in ("NIFTY", "BANKNIFTY"):
            q = nse[m.INDEX_TOKENS[u]]
            ltp = float(q["ltp"]); hi = float(q["high"]); lo = float(q["low"]); prev = float(q["close"])
            chg = round((ltp - prev) / prev * 100, 2); hist[u].append(ltp)
            if u not in seen_lo: seen_lo[u], seen_hi[u] = lo, hi; continue
            new_low = lo < seen_lo[u] - 1e-9; new_high = hi > seen_hi[u] + 1e-9
            seen_lo[u] = min(seen_lo[u], lo); seen_hi[u] = max(seen_hi[u], hi)
            mom = round(ltp - hist[u][0], 1) if len(hist[u]) >= 2 else 0.0
            direction = None
            if new_low and chg <= -CHG_MIN and mom < 0: direction, level = "bearish", round(seen_lo[u])
            elif new_high and chg >= CHG_MIN and mom > 0: direction, level = "bullish", round(seen_hi[u])
            if not direction: continue
            key = (u, direction)
            if time.time() - last_alert.get(key, 0) < SENSOR_CD: continue
            cand = pick_strike(u, direction)
            if not cand: continue
            last_alert[key] = time.time()
            emit(card(u, direction, ltp, level, cand, 1))
        if warned_down: emit("✅ CONNECTION RECOVERED — engine live again.")
        errs = 0; warned_down = False
    except Exception as e:
        if transient(e):
            errs += 1
            if errs == 5 and not warned_down:
                warned_down = True
                emit(f"⚠️ FEED DOWN ~{errs*POLL}s (network/rate). Retrying; watch any live position yourself.")
            time.sleep(10)
        else:
            emit(f"engine error (continuing): {type(e).__name__}: {e}")
    time.sleep(POLL)
