"""
TOP MOVERS SCALP BOT
--------------------
Every 15 minutes:
  1. Finds the top gainers and top losers (24h %) among liquid USDT coins.
  2. Gainers  -> looks for LONG pullback entries.
     Losers   -> looks for SHORT rally entries.
  3. Sends a Telegram alert with entry, stop loss, take profit and leverage.
  4. Watches every alerted trade and tells you when to move SL to breakeven
     and when to EXIT (take profit, stop loss, breakeven, trend flip or time).

Entry rules (15m candles, 1h trend):
  LONG : close > 1h EMA200, EMA9 > EMA21, close > VWAP,
         a recent candle dipped into EMA9, and the last candle closed green above EMA9.
  SHORT: the exact mirror.

Trade plan (your scalp plan):
  Stop  = beyond the last 3 candles' low/high (0.4% min, 3% max, otherwise skip)
  TP    = 2R  (+10% account)          Breakeven when price reaches 1R
  Leverage = 5 / stop%  (max 10x)  -> every loss is about -5% of the account
  Daily: stop new alerts after +20%, -10% or 4 trades.
"""
import os, json, time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import requests

# ================= SETTINGS (safe to change) =================
TOP_N           = 100     # 100 top gainers + 100 top losers
MIN_VOL_USD     = 5e6     # skip coins under $5M 24h volume
RISK_PCT        = 5.0     # account % lost if stop is hit
RR              = 2.0     # take profit at 2R
MAX_LEV         = 10
MIN_SL_PCT      = 0.4
MAX_SL_PCT      = 4.0
MAX_NEW_PER_RUN = 5       # max new entry alerts per scan
MAX_HOLD        = 16      # 15m candles (= 4 hours), then exit at market
DAILY_TARGET_R  = 4.0     # 4R = +20%
DAILY_STOP_R    = -2.0    # -2R = -10%
MAX_TRADES_DAY  = 10
ENFORCE_DAILY   = True    # stop new alerts once a daily limit is reached
# =============================================================

TF, TREND_TF = "15m", "1h"
STATE_FILE = "state.json"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "EUR", "AEUR",
           "PAXG", "XUSD", "USD1", "RLUSD", "BFUSD"}
BASES = [("https://fapi.binance.com", "/fapi/v1"),          # futures
         ("https://data-api.binance.vision", "/api/v3")]    # spot mirror (works from GitHub)
BASE = None


def get(path, **params):
    """GET from the first Binance endpoint that works (futures is blocked from US servers)."""
    global BASE
    order = [BASE] if BASE else BASES
    last = None
    for host, pre in order:
        try:
            r = requests.get(host + pre + path, params=params, timeout=15)
            if r.status_code == 200:
                BASE = (host, pre)
                return r.json()
            last = f"{host} -> {r.status_code}"
        except Exception as e:
            last = f"{host} -> {e}"
    raise RuntimeError(f"Binance unreachable: {last}")


def send(msg):
    print(msg, "\n---")
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def fmt(x):
    return f"{x:.6g}"


def ema(vals, n):
    k, out, e = 2 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def candles(sym, tf, limit):
    """Closed candles only: dicts with t (open ms), o, h, l, c, v."""
    now = int(time.time() * 1000)
    raw = get("/klines", symbol=sym, interval=tf, limit=limit)
    return [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]),
                 c=float(k[4]), v=float(k[5])) for k in raw if k[6] < now]


def atr(cs, n=14):
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"]))
           for p, c in zip(cs[:-1], cs[1:])]
    return sum(trs[-n:]) / min(n, len(trs))


def vwap_today(cs):
    day = datetime.fromtimestamp(cs[-1]["t"] / 1000, timezone.utc).date()
    today = [c for c in cs if datetime.fromtimestamp(c["t"] / 1000, timezone.utc).date() == day]
    pv = sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in today)
    vol = sum(c["v"] for c in today)
    return pv / vol if vol else today[-1]["c"]


# ---------------- universe ----------------
def movers():
    rows = []
    for t in get("/ticker/24hr"):
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLES or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        vol = float(t.get("quoteVolume", 0))
        if vol < MIN_VOL_USD:
            continue
        rows.append(dict(sym=s, chg=float(t["priceChangePercent"]), vol=vol))
    rows.sort(key=lambda r: r["chg"], reverse=True)
    gainers = [dict(r, side="LONG") for r in rows[:TOP_N] if r["chg"] > 0]
    losers = [dict(r, side="SHORT") for r in rows[::-1][:TOP_N] if r["chg"] < 0]
    return gainers + losers


# ---------------- entry check ----------------
def check_entry(m):
    try:
        cs = candles(m["sym"], TF, 300)
        h1 = candles(m["sym"], TREND_TF, 250)
    except Exception as e:
        print("skip", m["sym"], e)
        return None
    if len(cs) < 60 or len(h1) < 200:
        return None
    closes = [c["c"] for c in cs]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    e200h = ema([c["c"] for c in h1], 200)[-1]
    vw, a = vwap_today(cs), atr(cs)
    last, recent = cs[-1], cs[-3:]
    entry = last["c"]

    if m["side"] == "LONG":
        ok = (entry > e200h and e9[-1] > e21[-1] and entry > vw
              and any(c["l"] <= e9[-3 + i] for i, c in enumerate(recent))
              and last["c"] > last["o"] and last["c"] > e9[-1])
        sl = min(c["l"] for c in recent) - 0.1 * a
        sl_pct = (entry - sl) / entry * 100
    else:
        ok = (entry < e200h and e9[-1] < e21[-1] and entry < vw
              and any(c["h"] >= e9[-3 + i] for i, c in enumerate(recent))
              and last["c"] < last["o"] and last["c"] < e9[-1])
        sl = max(c["h"] for c in recent) + 0.1 * a
        sl_pct = (sl - entry) / entry * 100
    if not ok or sl_pct > MAX_SL_PCT:
        return None
    if sl_pct < MIN_SL_PCT:
        sl_pct = MIN_SL_PCT
        sl = entry * (1 - sl_pct / 100) if m["side"] == "LONG" else entry * (1 + sl_pct / 100)
    d = 1 if m["side"] == "LONG" else -1
    risk = abs(entry - sl)
    lev = max(1.0, min(MAX_LEV, int(RISK_PCT / sl_pct * 2) / 2))
    return dict(sym=m["sym"], side=m["side"], chg=m["chg"], vol=m["vol"],
                entry=entry, sl0=sl, stop=sl, tp=entry + d * RR * risk,
                be=entry + d * risk, sl_pct=round(sl_pct, 2), lev=lev,
                opened=last["t"], last_seen=last["t"], be_moved=False)


# ---------------- exit tracking ----------------
def manage(trade):
    """Returns (still_open, messages, result_R or None)."""
    try:
        cs = candles(trade["sym"], TF, 300)
    except Exception as e:
        print("skip manage", trade["sym"], e)
        return True, [], None
    closes = [c["c"] for c in cs]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    long_ = trade["side"] == "LONG"
    risk = abs(trade["entry"] - trade["sl0"])
    msgs = []
    name = f'{trade["sym"]} {trade["side"]}'

    for i, c in enumerate(cs):
        if c["t"] <= trade["last_seen"]:
            continue
        trade["last_seen"] = c["t"]
        hit_stop = c["l"] <= trade["stop"] if long_ else c["h"] >= trade["stop"]
        hit_tp = c["h"] >= trade["tp"] if long_ else c["l"] <= trade["tp"]
        hit_be = c["h"] >= trade["be"] if long_ else c["l"] <= trade["be"]
        if hit_stop:                                   # stop checked first (worst case)
            if trade["be_moved"]:
                msgs.append(f"⚪ EXIT {name} at breakeven {fmt(trade['stop'])}\nResult: 0R (fees only)")
                return False, msgs, 0.0
            msgs.append(f"❌ STOP LOSS {name} at {fmt(trade['stop'])}\nResult: -1R (~-{RISK_PCT:g}%)")
            return False, msgs, -1.0
        if hit_tp:
            msgs.append(f"✅ TAKE PROFIT {name} at {fmt(trade['tp'])}\nResult: +{RR:g}R (~+{RR*RISK_PCT:g}%)")
            return False, msgs, RR
        if hit_be and not trade["be_moved"]:
            trade["be_moved"], trade["stop"] = True, trade["entry"]
            msgs.append(f"🔒 {name} reached 1R — MOVE STOP to entry {fmt(trade['entry'])}")
        flip = (e9[i] < e21[i]) if long_ else (e9[i] > e21[i])
        held = sum(1 for c2 in cs[:i + 1] if c2["t"] > trade["opened"])
        if flip or held >= MAX_HOLD:
            r = (c["c"] - trade["entry"]) / risk * (1 if long_ else -1)
            why = "trend flipped (EMA9/21 cross)" if flip else "4h time limit"
            msgs.append(f"🚪 EXIT NOW {name} at ~{fmt(c['c'])} — {why}\nResult: {r:+.1f}R (~{r*RISK_PCT:+.0f}%)")
            return False, msgs, round(r, 2)
    return True, msgs, None


# ---------------- main ----------------
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"open": [], "day": "", "R": 0.0, "trades": 0, "wins": 0, "losses": 0, "halted": False}


def main():
    st = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != today:
        st.update(day=today, R=0.0, trades=0, wins=0, losses=0, halted=False)

    # 1) manage open trades
    still = []
    for tr in st["open"]:
        is_open, msgs, res = manage(tr)
        for m in msgs:
            send(m)
        if is_open:
            still.append(tr)
        elif res is not None:
            st["R"] = round(st["R"] + res, 2)
            st["wins"] += res > 0
            st["losses"] += res < 0
            send(f"📊 Today (UTC): {st['wins']}W {st['losses']}L | {st['R']:+.1f}R (~{st['R']*RISK_PCT:+.0f}%)")
    st["open"] = still

    # 2) daily limits
    limit = None
    if st["R"] >= DAILY_TARGET_R: limit = "🎯 Daily target reached (+20%). No more entries today."
    elif st["R"] <= DAILY_STOP_R: limit = "🛑 Daily max loss reached (-10%). Stop trading today."
    elif st["trades"] >= MAX_TRADES_DAY: limit = "⏸ Max trades taken today. No more entries."
    if ENFORCE_DAILY and limit:
        if not st["halted"]:
            send(limit)
            st["halted"] = True
        json.dump(st, open(STATE_FILE, "w"), indent=1)
        return

    # 3) scan movers for new entries
    busy = {t["sym"] for t in st["open"]}
    universe = [m for m in movers() if m["sym"] not in busy]
    print(f"Scanning {len(universe)} movers via {BASE[0]}")
    with ThreadPoolExecutor(8) as ex:
        found = [s for s in ex.map(check_entry, universe) if s]
    found.sort(key=lambda s: s["vol"], reverse=True)
    room = MAX_TRADES_DAY - st["trades"] if ENFORCE_DAILY else MAX_NEW_PER_RUN
    for s in found[:max(0, min(MAX_NEW_PER_RUN, room))]:
        icon = "🟢 LONG" if s["side"] == "LONG" else "🔴 SHORT"
        send(f"{icon} {s['sym']}  ({s['chg']:+.1f}% 24h, ${s['vol']/1e6:.0f}M vol)\n"
             f"Entry: {fmt(s['entry'])}\n"
             f"Stop loss: {fmt(s['sl0'])}  (-{s['sl_pct']}%)\n"
             f"Take profit: {fmt(s['tp'])}  (+{s['sl_pct']*RR:.2f}%)\n"
             f"Breakeven at: {fmt(s['be'])}\n"
             f"Leverage: {s['lev']:g}x isolated  → win ~+{RR*RISK_PCT:g}%, loss ~-{RISK_PCT:g}%\n"
             f"Use a LIMIT order. Set SL/TP immediately.")
        st["open"].append(s)
        st["trades"] += 1
    json.dump(st, open(STATE_FILE, "w"), indent=1)


if __name__ == "__main__":
    main()
