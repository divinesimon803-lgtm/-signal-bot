import asyncio
import datetime
import logging
import os
import pandas as pd
import ta
import yfinance as yf
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = "8874815036:AAGZAWFJoVf3pK1qpn4CdbA_95NYy9TcLt4"
TELEGRAM_CHAT_ID = "7889527038"
BOT_PASSCODE = "5051"

WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD (Gold)",
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "BTC-USD": "Bitcoin"
}

WEEKEND_ASSETS = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum"
}

TIMEFRAME = "15m"
last_signals = {}
authorized_users = set()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "APA Signal Bot is live!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

def fetch_data(ticker):
    try:
        df = yf.download(tickers=ticker, period="5d", interval=TIMEFRAME, progress=False)
        if df.empty or len(df) < 50:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['ema50'] = ta.trend.ema_indicator(df['Close'], window=50)
        df['rsi14'] = ta.momentum.rsi(df['Close'], window=14)
        return df
    except Exception as e:
        logging.error(f"Error fetching {ticker}: {e}")
        return None

def get_signal(ticker):
    df = fetch_data(ticker)
    if df is None:
        return None, None, None, None

    last = df.iloc[-2]
    close = float(last['Close'])
    ema = float(last['ema50'])
    rsi = float(last['rsi14'])

    sig = None
    if close > ema and rsi > 55:
        sig = "BUY"
    elif close < ema and rsi < 45:
        sig = "SELL"

    if not sig:
        return None, None, None, None

    # CUSTOM FAST-CLOSING TP & SL PER ASSET TYPE
    if ticker in ["EURUSD=X", "GBPUSD=X"]:
        # Forex Scalping Targets: 12 Pips TP, 8 Pips SL (Fast Exits)
        tp_distance = 0.0012
        sl_distance = 0.0008
        tp = close + tp_distance if sig == "BUY" else close - tp_distance
        sl = close - sl_distance if sig == "BUY" else close + sl_distance

    elif ticker == "GC=F":
        # Gold Scalping Targets: $2.50 TP, $1.50 SL
        tp_distance = 2.50
        sl_distance = 1.50
        tp = close + tp_distance if sig == "BUY" else close - tp_distance
        sl = close - sl_distance if sig == "BUY" else close + sl_distance

    else:
        # Crypto Targets (BTC/ETH): Keep percentages fast & unchanged
        sl = close * 0.998 if sig == "BUY" else close * 1.002
        tp = close * 1.004 if sig == "BUY" else close * 0.996

    return sig, close, sl, tp

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

async def signal_loop(app):
    global last_signals
    while True:
        try:
            day = datetime.datetime.now().weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                sig, entry, sl, tp = get_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    emoji = "📈" if sig == "BUY" else "📉"
                    
                    # Decimals formatted: 5 decimal places for Forex, 2 for Gold/Crypto
                    dec = 5 if ticker in ["EURUSD=X", "GBPUSD=X"] else 2
                    
                    msg = (
                        f"🚨 *NEW APA SIGNAL* 🚨\n\n"
                        f"Asset: *{label}*\n"
                        f"Action: *{sig}* {emoji}\n\n"
                        f"Entry: `{entry:.{dec}f}`\n\n"
                        f"SL:\n`{sl:.{dec}f}`\n\n"
                        f"TP:\n`{tp:.{dec}f}`"
                    )
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

        except Exception as e:
            logging.error(f"Loop error: {e}")

        await asyncio.sleep(60)

async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", handle_text_message))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))

    Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(signal_loop(app))

    print("Signal Bot active...")
    
    async with app:
        await app.start()
        await app.updater.start_polling()
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
