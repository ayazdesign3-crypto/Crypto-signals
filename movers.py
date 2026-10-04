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
TOP_N           = 50      # 50 top gainers + 50 top losers = 100 coins
