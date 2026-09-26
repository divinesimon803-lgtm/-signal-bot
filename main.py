import asyncio
import datetime
import logging
import os
import time
import aiohttp
import pandas as pd
import ta
import yfinance as yf
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- CONFIGURATION & SECURITY ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_PASSCODE = os.getenv("BOT_PASSCODE")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not BOT_PASSCODE:
    raise ValueError("CRITICAL SECURITY ERROR: Missing required environment variables.")

RISK_PER_TRADE_PCT = 0.015  # 1.5% risk profile
DEFAULT_ACCOUNT_BALANCE = 1000.0

WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "JPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "CAD=X": "USDCAD",
    "NZDUSD=X": "NZDUSD",
    "CHF=X": "USDCHF",
    "BTC-USD": "BTCUSD"
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()
active_trades = {}  # Tracks ongoing trades for live management

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER (24/7 Live Status) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Strict Institutional 24/7 Trading Engine is Live & Monitoring."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- DYNAMIC LOT SIZING ---
def calculate_dynamic_lot(ticker, sl_pips, account_balance=DEFAULT_ACCOUNT_BALANCE):
    risk_amount = account_balance * RISK_PER_TRADE_PCT
    pip_value = 10.0
    if "JPY" in ticker:
        pip_value = 6.5
    elif ticker in ["GC=F", "BTC-USD"]:
        pip_value = 1.0

    if sl_pips <= 0:
        return 0.02

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    return max(0.01, min(calculated_lot, 0.05 if account_balance < 500.0 else 3.0))

# --- DATA FETCHER ---
def fetch_data(ticker, interval, period="10d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        df.columns = [str(col).capitalize() for col in df.columns]
        required_cols = ['High', 'Low', 'Close']
        if not all(col in df.columns for col in required_cols):
            return None

        high_series = pd.to_numeric(df['High'].squeeze(), errors='coerce')
        low_series = pd.to_numeric(df['Low'].squeeze(), errors='coerce')
        close_series = pd.to_numeric(df['Close'].squeeze(), errors='coerce')

        if len(close_series.dropna()) < 30:
            return None

        df['atr14'] = ta.volatility.average_true_range(high_series, low_series, close_series, window=14)
        df['ema50'] = ta.trend.ema_indicator(close_series, window=50)
        df['ema200'] = ta.trend.ema_indicator(close_series, window=200)
        df['rsi14'] = ta.momentum.rsi(close_series, window=14)

        return df
    except Exception as e:
        logging.error(f"Data fetch error for {ticker}: {e}")
        return None

def get_h1_trend_bias(ticker):
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="10d")
    if df_h1 is None or len(df_h1) < 30:
        return "NEUTRAL"
    
    latest_ema50 = float(df_h1['ema50'].iloc[-1]) if pd.notna(df_h1['ema50'].iloc[-1]) else 0.0
    latest_ema200 = float(df_h1['ema200'].iloc[-1]) if pd.notna(df_h1['ema200'].iloc[-1]) else 0.0

    if latest_ema50 > latest_ema200:
        return "BULLISH"
    elif latest_ema50 < latest_ema200:
        return "BEARISH"
    return "NEUTRAL"

# --- STRICT TRADING STRATEGY ---
def get_strategy_signal(ticker):
    h1_bias = get_h1_trend_bias(ticker)
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 20:
        return [None] * 11

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['ema200']):
        return [None] * 11

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    
    recent_high = float(df_m15['High'].iloc[-10:-2].max())
    recent_low = float(df_m15['Low'].iloc[-10:-2].min())

    spread_buffer = 0.0002 if "JPY" not in ticker and "GC=F" not in ticker and "BTC-USD" not in ticker else (0.02 if "JPY" in ticker else 1.0)

    sig = None
    if h1_bias == "BULLISH" and close_p > recent_high and (40 <= rsi <= 65):
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p < recent_low and (35 <= rsi <= 60):
        sig = "SELL"

    if not sig:
        return [None] * 11

    sl_distance = atr * 1.5
    tp_distance = sl_distance * 2.0

    if sig == "BUY":
        entry = close_p + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        partial_target = entry + (sl_distance * 1.0)
        be_level = entry + (sl_distance * 1.5)
    else:
        entry = close_p - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        partial_target = entry - (sl_distance * 1.0)
        be_level = entry - (sl_distance * 1.5)

    sl_pips = sl_distance * 10000 if "JPY" not in ticker else sl_distance * 100
    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    
    return sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, h1_bias

# --- TELEGRAM COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Strict Trading Bot Active.** Ready to secure wins.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    await update.message.reply_text(f"📊 **Active Positions Monitored:** {len(active_trades)}", parse_mode="Markdown")

# --- REAL-TIME 24/7 LIVE CHART MONITORING & GUIDANCE LOOP ---
async def live_chart_guidance_loop(app):
    global active_trades
    while True:
        await asyncio.sleep(20) # Scans live chart adjustments every 20 seconds
        if not active_trades:
            continue

        target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID

        for label, trade in list(active_trades.items()):
            try:
                y_ticker = next((k for k, v in WEEKDAY_ASSETS.items() if v == label), "BTC-USD")
                df_live = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="2d")
                if df_live is None or len(df_live) < 10:
                    continue

                current_price = float(df_live['Close'].iloc[-1])
                current_rsi = float(df_live['rsi14'].iloc[-1]) if pd.notna(df_live['rsi14'].iloc[-1]) else 50.0
                current_ema50 = float(df_live['ema50'].iloc[-1]) if pd.notna(df_live['ema50'].iloc[-1]) else 0.0
                current_ema200 = float(df_live['ema200'].iloc[-1]) if pd.notna(df_live['ema200'].iloc[-1]) else 0.0
                
                trade_type = trade["type"]
                entry = trade["entry"]
                sl = trade["sl"]
                tp = trade["tp"]
                be_level = trade["be_level"]
                dec = 3 if "JPY" in y_ticker else (2 if y_ticker in ["BTC-USD", "GC=F"] else 4)

                # 1. Check TP Hit
                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    msg = f"🎯 **[TARGET REACHED] - {label}**\nPrice hit Take Profit at `{tp:.{dec}f}`. Secure your profits!"
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # 2. Check SL Hit
                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    msg = f"🛑 **[STOP LOSS HIT] - {label}**\nPrice triggered stop loss at `{sl:.{dec}f}`."
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # 3. Trend Reversal Detection (Manual Early Close / Cut Loss Alert)
                # Triggers if momentum flips aggressively against the position (e.g. RSI overbought/oversold cross + EMA crossover shift)
                reversal_detected = False
                reversal_reason = ""
                if trade_type == "BUY":
                    if current_ema50 < current_ema200 and current_price < current_ema50:
                        reversal_detected = True
                        reversal_reason = "15m EMA50 crossed below EMA200 and price dropped below trend support."
                    elif current_rsi > 75 and current_price < float(df_live['Close'].iloc[-2]):
                        reversal_detected = True
                        reversal_reason = "RSI reached extreme overbought territory and momentum is rolling over."
                else: # SELL trade
                    if current_ema50 > current_ema200 and current_price > current_ema50:
                        reversal_detected = True
                        reversal_reason = "15m EMA50 crossed above EMA200 and price pushed above trend resistance."
                    elif current_rsi < 25 and current_price > float(df_live['Close'].iloc[-2]):
                        reversal_detected = True
                        reversal_reason = "RSI reached extreme oversold territory and downward momentum is reversing."

                if reversal_detected and not trade.get("reversal_alerted", False):
                    trade["reversal_alerted"] = True
                    msg = (
                        f"⚠️ **[MANUAL EXIT / REVERSAL WARNING] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🔄 **Trend Shift Detected:** {reversal_reason}\n"
                        f"📊 **Current Price:** `{current_price:.{dec}f}` | **RSI:** `{current_rsi:.1f}`\n\n"
                        f"👉 **Recommendation:** Consider **closing this trade manually** right now to protect capital or lock in remaining profits before a hard reversal occurs."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # 4. Strict Guidance: Modify to Breakeven
                if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                    trade["be_hit"] = True
                    msg = (
                        f"🛡️ **[STRICT GUIDANCE: MODIFY SL] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 Market reached target zone (`{current_price:.{dec}f}`).\n"
                        f"👉 **Action Required:** Immediately modify your Stop Loss to Entry (`{entry:.{dec}f}`) to lock in a risk-free trade."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

            except Exception as e:
                logging.error(f"Guidance loop error for {label}: {e}")

# --- SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals, active_trades
    while True:
        try:
            if len(active_trades) >= 3:
                await asyncio.sleep(60)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                if label in active_trades:
                    continue

                sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, h1_bias = get_strategy_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dec = 3 if "JPY" in ticker else (2 if ticker in ["BTC-USD", "GC=F"] else 4)
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    signal_text = (
                        f"🚨 **STRICT TRADE SIGNAL**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Asset:** `{label}` | **Bias:** `{h1_bias}`\n"
                        f"📈 **Direction:** {dir_icon} **{sig}**\n\n"
                        f"🔹 **Entry:** `{entry:.{dec}f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"🎯 **Take Profit:** `{tp:.{dec}f}`\n"
                        f"⚖️ **Lot Size:** `{rec_lot}`\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"💡 *Bot monitors live charts 24/7 for breakeven triggers and trend reversals.*"
                    )

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(chat_id=target_user, text=signal_text, parse_mode="Markdown")

                    active_trades[label] = {
                        "type": sig,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "partial_target": partial_target,
                        "be_level": be_level,
                        "be_hit": False,
                        "reversal_alerted": False
                    }
        except Exception as e:
            logging.error(f"Signal loop error: {e}")

        await asyncio.sleep(45)

async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(live_chart_guidance_loop(app))

# --- MAIN ENTRY ---
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Strict Trading Engine Running 24/7...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
