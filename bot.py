"""
Supertrend Bot — M30 + lọc H1
Chỉ bắn tín hiệu khi M30 và H1 cùng chiều → giảm tín hiệu giả
"""

import os, time, threading, requests
import pandas as pd
import numpy as np
from datetime import datetime

# ══════════════════════════
#  CẤU HÌNH
# ══════════════════════════
TOKEN    = os.environ["TELEGRAM_TOKEN"]
CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
SYMBOLS  = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")

ATR_LEN  = 10
ATR_MULT = 3.0
SL_MULT  = 1.0
TP_MULTS = [1.0, 2.0, 3.0, 4.0, 5.0]

SLEEP_M30 = 30 * 60   # check mỗi 30 phút

sent_signals: dict = {}
sent_lock = threading.Lock()


# ══════════════════════════
#  BINANCE
# ══════════════════════════
def fetch(symbol, interval, limit=200):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=[
        "ts","open","high","low","close","vol",
        "cts","qav","trades","tbbav","tbqav","_"
    ])
    for c in ["open","high","low","close"]:
        df[c] = df[c].astype(float)
    return df


# ══════════════════════════
#  SUPERTREND
# ══════════════════════════
def calc_supertrend(df, period, mult):
    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values
    hl2   = (high + low) / 2

    tr = np.maximum(high - low,
         np.maximum(abs(high - np.roll(close, 1)),
                    abs(low  - np.roll(close, 1))))
    tr[0] = high[0] - low[0]

    atr = np.zeros(len(tr))
    atr[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

    up     = hl2 - mult * atr
    dn     = hl2 + mult * atr
    up_arr = up.copy()
    dn_arr = dn.copy()
    trend  = np.ones(len(close), dtype=int)

    for i in range(1, len(close)):
        up_arr[i] = max(up[i], up_arr[i-1]) if close[i-1] > up_arr[i-1] else up[i]
        dn_arr[i] = min(dn[i], dn_arr[i-1]) if close[i-1] < dn_arr[i-1] else dn[i]

        if trend[i-1] == -1 and close[i] > dn_arr[i-1]:
            trend[i] = 1
        elif trend[i-1] == 1 and close[i] < up_arr[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]

    return trend, atr


# ══════════════════════════
#  TELEGRAM
# ══════════════════════════
def send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        print(f"[Telegram ERR] {e}")


def build_msg(signal, symbol, entry, sl, tps, atr, h1_trend):
    icon    = "🟢 LONG" if signal == "BUY" else "🔴 SHORT"
    pct     = abs(entry - sl) / entry * 100
    h1_icon = "📈 H1 uptrend" if h1_trend == 1 else "📉 H1 downtrend"
    lines   = [
        f"{icon}  <b>{symbol}</b>  [M30 🔥]",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🐵 Entry:     <code>{entry:.4f}</code>",
        f"🛑 Stop Loss: <code>{sl:.4f}</code>  (-{pct:.2f}%)",
        "",
    ]
    for i, tp in enumerate(tps, 1):
        rr    = abs(tp - entry) / abs(entry - sl)
        emoji = "🎯🎯" if i >= 4 else "🎯"
        lines.append(f"{emoji} TP{i}: <code>{tp:.4f}</code>  (R{rr:.1f})")
    lines += [
        "",
        f"📊 ATR: <code>{atr:.4f}</code>",
        f"🔎 {h1_icon}  ✅ xác nhận",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    return "\n".join(lines)


# ══════════════════════════
#  CHECK 1 SYMBOL
# ══════════════════════════
def check(symbol):
    # M30 — tín hiệu chính
    df30           = fetch(symbol, "30m")
    trend30, atr30 = calc_supertrend(df30, ATR_LEN, ATR_MULT)

    curr = trend30[-1]
    prev = trend30[-2]
    buy_sig  = curr ==  1 and prev == -1
    sell_sig = curr == -1 and prev ==  1

    if not (buy_sig or sell_sig):
        return

    # H1 — bộ lọc xu hướng
    df1h          = fetch(symbol, "1h")
    trend1h, _    = calc_supertrend(df1h, ATR_LEN, ATR_MULT)
    h1_trend      = trend1h[-1]

    sig = "BUY" if buy_sig else "SELL"

    # Lọc: chỉ bắn khi H1 cùng chiều M30
    if sig == "BUY"  and h1_trend != 1:
        print(f"[FILTERED] {symbol} M30 BUY nhưng H1 downtrend — bỏ qua")
        return
    if sig == "SELL" and h1_trend != -1:
        print(f"[FILTERED] {symbol} M30 SELL nhưng H1 uptrend — bỏ qua")
        return

    # Chống spam
    key = f"{symbol}_30m"
    with sent_lock:
        if sent_signals.get(key) == sig:
            return
        sent_signals[key] = sig

    entry = df30["open"].iloc[-1]
    atr   = atr30[-1]
    sl    = (entry - SL_MULT * atr) if buy_sig else (entry + SL_MULT * atr)
    tps   = [(entry + m * atr) if buy_sig else (entry - m * atr) for m in TP_MULTS]

    msg = build_msg(sig, symbol, entry, sl, tps, atr, h1_trend)
    send(msg)
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] ✅ {sig} {symbol} M30 (H1 confirmed)")


# ══════════════════════════
#  MAIN LOOP
# ══════════════════════════
def main():
    sym_list = ", ".join(SYMBOLS)
    send(
        f"🤖 <b>Supertrend Bot started!</b>\n"
        f"📌 Symbols: <b>{sym_list}</b>\n"
        f"⏱ Khung: <b>M30</b> + lọc <b>H1</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Chỉ bắn khi M30 và H1 cùng chiều ✅"
    )
    print(f"Bot started. Symbols: {sym_list} | Sleep: {SLEEP_M30}s")

    while True:
        for sym in SYMBOLS:
            try:
                check(sym)
            except Exception as e:
                print(f"[ERR] {sym}: {e}")
            time.sleep(1)
        time.sleep(SLEEP_M30)


if __name__ == "__main__":
    main()
