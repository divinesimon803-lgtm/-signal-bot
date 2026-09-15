import asyncio
import datetime
import logging
import pandas as pd
import ta
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = "8874815036:AAF26vD-5gVypsXwLzZTNZ1AeCom3FGMZUI"
TELEGRAM_CHAT_ID = "7889527038"

SYMBOL_WEEKDAY = "GC=F"   # Gold Futures Ticker (XAUUSD)
SYMBOL_WEEKEND = "BTC-USD" # Active weekend asset option
TIMEFRAME = "15m"

last_signal = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def fetch_data(ticker):
    df = yf.download(tickers=ticker, period="5d", interval=TIMEFRAME)
    if df.empty or len(df) < 50:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df['ema50'] = ta.trend.ema_indicator(df['Close'], window=50)
    df['rsi14'] = ta.momentum.rsi(df['Close'], window=14)
    return df

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

    sl = close - 4.00 if sig == "BUY" else close + 4.00
    tp = close + 8.00 if sig == "BUY" else close - 8.00

    return sig, close, sl, tp

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🟢 APA Signal Bot is running 24/7!")

async def signal_loop(app):
    global last_signal
    while True:
        try:
            day = datetime.datetime.now().weekday()
            active_ticker = SYMBOL_WEEKDAY if day < 5 else SYMBOL_WEEKEND
            asset_label = "XAUUSD (Gold)" if day < 5 else "Weekend Market"

            sig, entry, sl, tp = get_signal(active_ticker)

            if sig and sig != last_signal:
                last_signal = sig
                emoji = "📈" if sig == "BUY" else "📉"
                msg = (
                    f"🚨 *NEW APA SIGNAL* 🚨\n\n"
                    f"Asset: *{asset_label}*\n"
                    f"Action: *{sig}* {emoji}\n"
                    f"Entry: `{entry:.2f}`\n"
                    f"SL: `{sl:.2f}`\n"
                    f"TP: `{tp:.2f}`\n\n"
                    f"💡 *Set your preferred lot size manually on your mobile MT5 app!*"
                )
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

        except Exception as e:
            logging.error(f"Loop error: {e}")

        await asyncio.sleep(60)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))

    loop = asyncio.get_event_loop()
    loop.create_task(signal_loop(app))

    print("Signal Bot active...")
    app.run_polling()
