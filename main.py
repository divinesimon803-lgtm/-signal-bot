import asyncio
import datetime
import logging
import os
import pandas as pd
import requests
import ta
import yfinance as yf
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_TOKEN = "8874815036:AAGZAWFJoVf3pK1qpn4CdbA_95NYy9TcLt4"
TELEGRAM_CHAT_ID = "7889527038"
BOT_PASSCODE = "5051"

# SUPABASE DATABASE CONFIGURATION
SUPABASE_URL = "https://khmtegoloiszskwjmuku.supabase.co"
SUPABASE_KEY = "sb_publishable_N4BfssoiokI-o002sw6eGQ_K5fMGcjG"

WEEKDAY_ASSETS = {
    "XAUUSD=X": "XAUUSD (Gold)",
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "BTC-USD": "Bitcoin"
}

WEEKEND_ASSETS = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum"
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# --- FLASK WEB SERVER ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "APA Signal Bot is live and scanning with WAT Time & Expiry Alerts!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# --- SUPABASE SIGNAL PUSHER ---
def push_to_supabase(symbol, action, entry, sl, tp):
    url = f"{SUPABASE_URL}/rest/v1/signals"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    payload = {
        "symbol": symbol,
        "action": action,
        "entry": float(entry),
        "sl": float(sl),
        "tp": float(tp)
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=5)
        if res.status_code in [200, 201]:
            logging.info(f"Successfully pushed {symbol} signal to Supabase!")
        else:
            logging.error(f"Supabase push error: {res.status_code} - {res.text}")
    except Exception as e:
        logging.error(f"Failed pushing signal to Supabase: {e}")

# --- MARKET DATA FETCHING ---
def fetch_data(ticker, interval, period="7d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
        if df.empty or len(df) < 50:
            return None
            
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if interval == TIMEFRAME_H1:
            df['ema200_h1'] = ta.trend.ema_indicator(df['Close'], window=200)
        elif interval == TIMEFRAME_M15:
            df['ema50'] = ta.trend.ema_indicator(df['Close'], window=50)
            df['rsi14'] = ta.momentum.rsi(df['Close'], window=14)
            df['atr14'] = ta.volatility.average_true_range(df['High'], df['Low'], df['Close'], window=14)

        return df
    except Exception as e:
        logging.error(f"Error fetching data for {ticker} ({interval}): {e}")
        return None

# --- SIGNAL CALCULATION WITH CONFLUENCE & ATR ---
def get_signal(ticker):
    # 1. Fetch H1 Data for Macro Trend Filter
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="30d")
    if df_h1 is None or 'ema200_h1' not in df_h1 or len(df_h1) < 2:
        return None, None, None, None

    last_h1_close = float(df_h1.iloc[-2]['Close'])
    h1_ema200 = float(df_h1.iloc[-2]['ema200_h1'])

    h1_trend = "BULLISH" if last_h1_close > h1_ema200 else "BEARISH"

    # 2. Fetch M15 Data for Entry Trigger
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 2:
        return None, None, None, None

    last_closed_candle = df_m15.iloc[-2]
    close = float(last_closed_candle['Close'])
    ema50 = float(last_closed_candle['ema50'])
    rsi = float(last_closed_candle['rsi14'])
    atr = float(last_closed_candle['atr14'])

    sig = None

    # Strict Confluence: H1 Trend + M15 EMA50 + M15 RSI Filter
    if h1_trend == "BULLISH" and close > ema50 and rsi > 55:
        sig = "BUY"
    elif h1_trend == "BEARISH" and close < ema50 and rsi < 45:
        sig = "SELL"

    if not sig:
        return None, None, None, None

    # Dynamic ATR Volatility Risk Management (1:2 Risk-Reward Ratio)
    sl_distance = atr * 1.5
    tp_distance = atr * 3.0

    if sig == "BUY":
        tp = close + tp_distance
        sl = close - sl_distance
    else:
        tp = close - tp_distance
        sl = close + sl_distance

    return sig, close, sl, tp

# --- TELEGRAM USER AUTHORIZATION ---
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text == BOT_PASSCODE or text == f"/start {BOT_PASSCODE}":
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 Passcode accepted! APA Signal Bot is active.")
    elif user_id in authorized_users:
        await update.message.reply_text("🟢 APA Signal Bot is actively monitoring markets!")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Please send the correct authorization code.", parse_mode="Markdown")

# --- HOURLY HEARTBEAT TASK ---
async def heartbeat_loop(app):
    while True:
        try:
            # Shift UTC to WAT (West Africa Time = UTC + 1)
            wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            formatted_wat = wat_time.strftime("%I:%M %p WAT")
            
            msg = f"🟢 *[Bot Heartbeat]* APA Signal Bot is active & scanning M15/H1 trends. ({formatted_wat})"
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Heartbeat failed: {e}")
        await asyncio.sleep(3600)

# --- MAIN SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text="🚀 *APA Signal Bot initialized with WAT Time & Expiry Alerts!*", 
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Failed to send startup alert: {e}")

    while True:
        try:
            day = datetime.datetime.now().weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                sig, entry, sl, tp = get_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    emoji = "📈" if sig == "BUY" else "📉"
                    dec = 5 if ticker in ["EURUSD=X", "GBPUSD=X"] else 2

                    # Calculate exact WAT Time (UTC+1) and Expiry Time (+15 Mins)
                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    expires_wat = now_wat + datetime.timedelta(minutes=15)

                    time_sent_str = now_wat.strftime("%I:%M %p")
                    time_expire_str = expires_wat.strftime("%I:%M %p")

                    msg = (
                        f"🚨 *NEW APA SIGNAL* 🚨\n\n"
                        f"Asset: *{label}*\n"
                        f"Action: *{sig}* {emoji}\n"
                        f"Recommended Lot: `0.01` (STRICT)\n\n"
                        f"Entry: `{entry:.{dec}f}`\n\n"
                        f"SL:\n`{sl:.{dec}f}`\n\n"
                        f"TP:\n`{tp:.{dec}f}`\n\n"
                        f"🕒 *Sent:* `{time_sent_str} WAT`\n"
                        f"⏳ *Valid Until:* `{time_expire_str} WAT`\n"
                        f"⚠️ *EXPIRED IF PAST `{time_expire_str}`! DO NOT ENTER!*"
                    )
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
                    
                    # Push directly to Supabase Database
                    push_to_supabase(symbol=label, action=sig, entry=entry, sl=sl, tp=tp)

        except Exception as e:
            logging.error(f"Loop error caught: {e}")

        await asyncio.sleep(60)

# --- ENTRY POINT ---
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_text_message))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))

    Thread(target=run_flask, daemon=True).start()

    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat_loop(app))

    print("Signal Bot active...")

    async with app:
        await app.start()
        await app.updater.start_polling()
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
