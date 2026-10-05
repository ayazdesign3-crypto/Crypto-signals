"""
CS Cross v2 (Light) signal scanner -> Telegram  (v3: trade plan, trend filter, cap tags)
BUY  : EMA21 crosses above EMA55, and within 3 candles a green candle closes above the cloud.
SELL : EMA21 crosses below EMA55, and within 3 candles a red candle closes below the cloud.
Only CLOSED candles are used. Timeframe comes from the TIMEFRAME setting in the workflow
(1d, 4h or 1h). Scans every USDT coin (low, mid and high cap).
"""
import os
import time
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

# ------------------------------ SETTINGS ------------------------------------
TOP_N          = 1000    # max coins to scan (sorted by 24h volume). 1000 = all
MIN_VOLUME_USD = 0       # skip coins with less 24h volume than this. 0 = all coins
FAST, SLOW, TREND = 21, 55, 200
CROSS_BARS     = 3       # candles allowed after the cross for the confirming close
USE_EMA200     = False   # True = BUY only above EMA200, SELL only below (fewer, safer)
SEND_IF_NONE_DAILY = True  # daily scan still sends "no signals"; 4H/1H stay silent

# --- Trade plan (matches your TradingView script) ---
ACCOUNT_USD    = 100     # <-- your Bybit futures balance in USD (used for position size)
LEVERAGE       = 3       # max leverage
ATR_LEN        = 14
SL_ATR_MAJOR   = 1.5     # stop = ATR x this (High cap coins)
SL_ATR_ALT     = 2.0     # stop = ATR x this (Mid / Low cap coins, wider for wicks)
RISK_MAJOR     = 2.0     # % of account risked per trade (High cap)
RISK_ALT       = 1.0     # % of account risked per trade (Mid / Low cap)
TP1_R          = 1.5     # TP1 = 1.5x the stop distance  (take 50%, move stop to entry)
TP2_R          = 3.0     # TP2 = 3x the stop distance    (take the rest, or trail it)

# --- Coin size tags (by 24h volume) ---
HIGH_CAP_VOL   = 100e6   # above this = High cap
MID_CAP_VOL    = 20e6    # above this = Mid cap, below = Low cap

# --- Higher-timeframe trend filter ---
HIDE_AGAINST_TREND = False  # True = only send signals that agree with the higher timeframe
# -----------------------------------------------------------------------------

TIMEFRAME  = os.getenv("TIMEFRAME", "1d").strip().lower()
TF_MS      = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}[TIMEFRAME]
HTF        = {"1h": "4h", "4h": "1d", "1d": "1w"}[TIMEFRAME]   # higher timeframe used for trend
TG_TOKEN   = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

SOURCES = [  # tried in order; GitHub servers are in the US, where Binance futures is blocked
    {"name": "Binance Futures", "base": "https://fapi.binance.com",
     "tick": "/fapi/v1/ticker/24hr", "kl": "/fapi/v1/klines"},
    {"name": "Binance (spot data)", "base": "https://data-api.binance.vision",
     "tick": "/api/v3/ticker/24hr", "kl": "/api/v3/klines"},
]
SKIP_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "USD1",
              "XUSD", "RLUSD", "USDE", "PAXG", "WBTC", "WBETH", "BFUSD", "EURI",
              "USDS", "PYUSD", "GBP", "TRY", "BRL"}

S = requests.Session()


def get(url, **params):
    for attempt in range(3):
        r = S.get(url, params=params, timeout=20)
        if r.status_code in (403, 451):          # region blocked -> try next source
            raise PermissionError(f"blocked ({r.status_code})")
        if r.ok:
            return r.json()
        time.sleep(2 + attempt * 3)              # rate limit / hiccup -> wait and retry
    r.raise_for_status()


def pick_source():
    for src in SOURCES:
        try:
            return src, get(src["base"] + src["tick"])
        except Exception as e:
            print(f"{src['name']} not available: {e}")
    raise SystemExit("No data source reachable.")


def pick_coins(tickers):
    coins = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in SKIP_BASES or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        vol = float(t.get("quoteVolume") or 0)
        if vol <= 0 or vol < MIN_VOLUME_USD:     # vol 0 = halted / delisted
            continue
        coins.append((sym, vol))
    coins.sort(key=lambda x: -x[1])
    return coins[:TOP_N]


def candles(src, symbol, interval=None):
    interval = interval or TIMEFRAME
    rows = get(src["base"] + src["kl"], symbol=symbol, interval=interval, limit=500)
    now = int(time.time() * 1000)
    rows = [r for r in rows if int(r[6]) < now]  # closed candles only
    if interval == TIMEFRAME and (not rows or now - int(rows[-1][6]) > 2 * TF_MS):
        return None                              # stale = not trading any more
    if not rows:
        return None
    df = pd.DataFrame(rows).iloc[:, [0, 1, 2, 3, 4]]
    df.columns = ["t", "open", "high", "low", "close"]
    return df.astype(float)


def atr(df):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / ATR_LEN, adjust=False).mean()


def htf_trend(src, sym):
    """UP / DOWN from EMA21 vs EMA55 on the higher timeframe, None if not enough data."""
    try:
        d = candles(src, sym, HTF)
        if d is None or len(d) < SLOW:
            return None
        c = d["close"]
        return "UP" if c.ewm(span=FAST, adjust=False).mean().iat[-1] > \
                       c.ewm(span=SLOW, adjust=False).mean().iat[-1] else "DOWN"
    except Exception:
        return None


def signals(df):
    c, o = df["close"], df["open"]
    f = c.ewm(span=FAST, adjust=False).mean()
    s = c.ewm(span=SLOW, adjust=False).mean()
    tr = c.ewm(span=TREND, adjust=False).mean()
    up, dn = f > s, f < s
    cross_up = up & ~up.shift(1, fill_value=False)
    cross_dn = dn & ~dn.shift(1, fill_value=False)
    top, bot = pd.concat([f, s], axis=1).max(axis=1), pd.concat([f, s], axis=1).min(axis=1)

    buy, sell = [False] * len(df), [False] * len(df)
    since_up = since_dn = 99
    done_up = done_dn = True
    for i in range(len(df)):
        if cross_up.iat[i]:
            since_up, done_up = 0, False
        elif since_up < 99:
            since_up += 1
        if cross_dn.iat[i]:
            since_dn, done_dn = 0, False
        elif since_dn < 99:
            since_dn += 1
        if (not done_up and up.iat[i] and since_up < CROSS_BARS
                and c.iat[i] > top.iat[i] and c.iat[i] > o.iat[i]):
            buy[i], done_up = True, True
        if (not done_dn and dn.iat[i] and since_dn < CROSS_BARS
                and c.iat[i] < bot.iat[i] and c.iat[i] < o.iat[i]):
            sell[i], done_dn = True, True

    df = df.assign(buy=buy, sell=sell, ema200=tr, atr=atr(df))
    df["above200"] = df["close"] > df["ema200"]
    df["has200"] = len(df) >= TREND
    return df


def check(src, sym, vol):
    try:
        df = candles(src, sym)
        if df is None or len(df) < SLOW + CROSS_BARS + 5:
            return None
        last = signals(df).iloc[-1]
        side = "BUY" if last["buy"] else "SELL" if last["sell"] else None
        if not side:
            return None
        trend = ("above" if last["above200"] else "below") if last["has200"] else "n/a"
        if USE_EMA200 and last["has200"]:
            if (side == "BUY") != bool(last["above200"]):
                return None
        htf = htf_trend(src, sym)
        with_trend = htf is None or (htf == "UP") == (side == "BUY")
        if HIDE_AGAINST_TREND and not with_trend:
            return None
        return dict(side=side, sym=sym, price=last["close"], ema200=trend, atr=last["atr"],
                    vol=vol, htf=htf, with_trend=with_trend)
    except Exception as e:
        print(f"{sym}: {e}")
        return None


def fmt_price(p):
    return f"{p:,.2f}" if p >= 100 else f"{p:.4f}" if p >= 1 else f"{p:.6g}"


def cap_tag(v):
    return "🟦 High cap" if v >= HIGH_CAP_VOL else "🟨 Mid cap" if v >= MID_CAP_VOL else "🟥 Low cap"


def trade_plan(r):
    high = r["vol"] >= HIGH_CAP_VOL
    k, risk = (SL_ATR_MAJOR, RISK_MAJOR) if high else (SL_ATR_ALT, RISK_ALT)
    e, dist = r["price"], k * r["atr"]
    d = 1 if r["side"] == "BUY" else -1
    sl, tp1, tp2 = e - d * dist, e + d * dist * TP1_R, e + d * dist * TP2_R
    sl_pct = dist / e * 100
    size = min(ACCOUNT_USD * risk / sl_pct, ACCOUNT_USD * LEVERAGE)   # position value in USD
    lev = max(1, min(LEVERAGE, size / ACCOUNT_USD))
    loss = size * sl_pct / 100
    return (f"Entry {fmt_price(e)}\n"
            f"SL  {fmt_price(sl)}  (-{sl_pct:.1f}%)\n"
            f"TP1 {fmt_price(tp1)}  (+{sl_pct*TP1_R:.1f}%)  close 50%, move SL to entry\n"
            f"TP2 {fmt_price(tp2)}  (+{sl_pct*TP2_R:.1f}%)  close rest or trail\n"
            f"Size ${size:,.0f} at {lev:.1f}x  (margin ${size/lev:,.0f}, risk ${loss:,.2f} = {risk:g}%)"
            + ("\n⚠️ Stop is wide - consider skipping" if sl_pct > 12 else ""))


def fmt_vol(v):
    return f"${v/1e9:.1f}B" if v >= 1e9 else f"${v/1e6:.0f}M" if v >= 1e6 else f"${v/1e3:.0f}K"


def send_telegram(msg):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("No Telegram token/chat id set - message not sent.")
        return
    chunks, cur = [], ""
    for block in msg.split("\n\n"):             # Telegram limit is 4096 chars per message;
        if cur and len(cur) + len(block) + 2 > 3800:   # split between signals, never inside one
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    chunks.append(cur)
    for ch in chunks:
        r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   data={"chat_id": TG_CHAT_ID, "text": ch, "disable_web_page_preview": "true"}, timeout=20)
        print("Telegram:", r.status_code)
        time.sleep(1)


def main():
    src, tickers = pick_source()
    coins = pick_coins(tickers)
    vols = dict(coins)
    print(f"Source: {src['name']} | timeframe {TIMEFRAME} | scanning {len(coins)} coins")

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = [r for r in ex.map(lambda x: check(src, x[0], x[1]), coins) if r]
    results.sort(key=lambda r: (not r["with_trend"], -r["vol"]))   # with-trend + biggest first

    buys = [r for r in results if r["side"] == "BUY"]
    sells = [r for r in results if r["side"] == "SELL"]

    def block(r):
        icon = "🟢 BUY" if r["side"] == "BUY" else "🔴 SELL"
        base = r["sym"][:-4]
        trend = ("✅ with " if r["with_trend"] else "⚠️ against ") + f"{HTF.upper()} trend" \
            if r["htf"] else "HTF trend n/a"
        return (f"{icon}  {base}   {cap_tag(r['vol'])} ({fmt_vol(r['vol'])})\n"
                f"{trend} | EMA200 {r['ema200']}\n"
                f"{trade_plan(r)}\n"
                f"https://www.bybit.com/trade/usdt/{base}USDT")

    label = {"1d": "Daily", "4h": "4H", "1h": "1H"}[TIMEFRAME]
    parts = [f"CS Cross v2 - {label} signals\n{len(coins)} coins scanned | "
             f"{len(buys)} BUY, {len(sells)} SELL"]
    parts += [block(r) for r in buys + sells]
    if not results:
        parts.append("No signals on the last closed candle.")
    parts.append("Check the chart on Bybit before entering. Low cap = more fakeouts.")
    msg = "\n\n".join(parts)
    print(msg)

    if buys or sells or (TIMEFRAME == "1d" and SEND_IF_NONE_DAILY):
        send_telegram(msg)


if __name__ == "__main__":
    main()
