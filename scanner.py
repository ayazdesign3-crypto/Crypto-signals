"""
Daily crypto signal bot  -  CS Cross v2 rules (Light) on DAILY candles -> Telegram
BUY  : EMA21 crosses above EMA55, and within 3 candles a green candle closes above the cloud.
SELL : EMA21 crosses below EMA55, and within 3 candles a red candle closes below the cloud.
Only closed daily candles are used.
"""
import os
import time
import requests
import pandas as pd

# ------------------------------ SETTINGS ------------------------------------
TOP_N          = 100     # number of coins to scan (highest 24h volume)
MIN_VOLUME_USD = 20e6    # skip coins with less 24h volume than this
FAST, SLOW, TREND = 21, 55, 200
CROSS_BARS     = 3
USE_EMA200     = False   # True = BUY only above EMA200, SELL only below (fewer, safer)
SEND_IF_NONE   = True    # True = still send a message when there are no signals
# -----------------------------------------------------------------------------

TG_TOKEN   = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

SOURCES = [  # tried in order; GitHub servers are in the US, where Binance futures is blocked
    {"name": "Binance Futures", "base": "https://fapi.binance.com",
     "tick": "/fapi/v1/ticker/24hr", "kl": "/fapi/v1/klines"},
    {"name": "Binance (spot data)", "base": "https://data-api.binance.vision",
     "tick": "/api/v3/ticker/24hr", "kl": "/api/v3/klines"},
]
SKIP_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "USD1",
              "XUSD", "RLUSD", "USDE", "PAXG", "WBTC", "WBETH", "BFUSD"}

S = requests.Session()


def get(url, **params):
    for attempt in range(3):
        r = S.get(url, params=params, timeout=20)
        if r.status_code in (403, 451):          # region blocked -> try next source
            raise PermissionError(f"blocked ({r.status_code})")
        if r.ok:
            return r.json()
        time.sleep(2)
    r.raise_for_status()


def pick_source():
    for src in SOURCES:
        try:
            tickers = get(src["base"] + src["tick"])
            return src, tickers
        except Exception as e:
            print(f"{src['name']} not available: {e}")
    raise SystemExit("No data source reachable.")


def coin_list(tickers):
    out = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in SKIP_BASES or base.endswith(("UP", "DOWN", "BULL", "BEAR")) or "_" in sym:
            continue
        if float(t["quoteVolume"]) >= MIN_VOLUME_USD:
            out.append((float(t["quoteVolume"]), sym))
    out.sort(reverse=True)
    return [s for _, s in out[:TOP_N]]


def daily_candles(src, symbol):
    k = get(src["base"] + src["kl"], symbol=symbol, interval="1d", limit=500)
    df = pd.DataFrame([row[:7] for row in k],
                      columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    df[["open", "close"]] = df[["open", "close"]].astype(float)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms").dt.strftime("%d %b")
    return df[df["close_time"] < time.time() * 1000].reset_index(drop=True)  # closed candles only


def pine_ema(s, length):
    """Same as TradingView ta.ema (SMA seed)."""
    out = [float("nan")] * len(s)
    if len(s) < length:
        return pd.Series(out, index=s.index)
    alpha, val = 2 / (length + 1), s.iloc[:length].mean()
    out[length - 1] = val
    for i in range(length, len(s)):
        val = alpha * s.iloc[i] + (1 - alpha) * val
        out[i] = val
    return pd.Series(out, index=s.index)


def bars_since(cond):
    out, last = [], None
    for i, c in enumerate(cond):
        if c:
            last = i
        out.append(i - last if last is not None else 10**9)
    return pd.Series(out, index=cond.index)


def signals(df):
    c, o = df["close"], df["open"]
    f, s, t = pine_ema(c, FAST), pine_ema(c, SLOW), pine_ema(c, TREND)
    up = (f > s) & (f.shift(1) <= s.shift(1))
    dn = (f < s) & (f.shift(1) >= s.shift(1))
    top, bot = pd.concat([f, s], axis=1).max(axis=1), pd.concat([f, s], axis=1).min(axis=1)
    tl = (c > t) if USE_EMA200 else True
    ts = (c < t) if USE_EMA200 else True
    long_ok = (bars_since(up) <= CROSS_BARS) & (f > s) & (c > top) & (c > o) & tl
    short_ok = (bars_since(dn) <= CROSS_BARS) & (f < s) & (c < bot) & (c < o) & ts
    return df.assign(ema200=t,
                     buy=long_ok & ~long_ok.shift(1, fill_value=False),
                     sell=short_ok & ~short_ok.shift(1, fill_value=False))


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("Telegram not set up (TG_TOKEN / TG_CHAT_ID missing).")
        return
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text}, timeout=20)
    print("Telegram:", r.status_code, r.text[:200])
    r.raise_for_status()


def main():
    src, tickers = pick_source()
    coins = coin_list(tickers)
    print(f"Source: {src['name']} - scanning {len(coins)} coins")
    buys, sells, day = [], [], ""
    for sym in coins:
        try:
            df = daily_candles(src, sym)
            if len(df) < SLOW + 5:
                continue
            r = signals(df).iloc[-1]
            day = r["date"]
            side = "above" if r["close"] > r["ema200"] else "below"
            line = f"{sym.replace('USDT', '')}  @ {r['close']:g}  ({side} EMA200)"
            if r["buy"]:
                buys.append(line)
            elif r["sell"]:
                sells.append(line)
        except Exception as e:
            print(f"skipped {sym}: {e}")
        time.sleep(0.05)

    msg = f"📊 Daily signals - candle of {day}\n({len(coins)} coins, CS Cross v2)\n"
    if buys:
        msg += "\n🟢 BUY\n" + "\n".join(buys) + "\n"
    if sells:
        msg += "\n🔴 SELL\n" + "\n".join(sells) + "\n"
    if not buys and not sells:
        msg += "\nNo new BUY or SELL signals today."
    print(msg)
    if buys or sells or SEND_IF_NONE:
        send_telegram(msg)


if __name__ == "__main__":
    main()
