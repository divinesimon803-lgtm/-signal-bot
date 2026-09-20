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
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- CONFIGURATION (ENVIRONMENT VARIABLES WITH FALLBACKS) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8874815036:AAGZAWFJoVf3pK1qpn4CdbA_95NYy9TcLt4")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7889527038")
BOT_PASSCODE = os.getenv("BOT_PASSCODE", "5051")

# SUPABASE DATABASE CONFIGURATION
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://khmtegoloiszskwjmuku.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_N4BfssoiokI-o002sw6eGQ_K5fMGcjG")

# RISK & ACCOUNT SAFETY CONFIGURATION
ACCOUNT_BALANCE = 1000.0  # Base account equity in USD
RISK_PER_TRADE_PCT = 0.01  # Risk 1% of account balance per trade ($10.00)
MAX_DAILY_LOSS_PCT = 0.03  # Daily Circuit Breaker: Max 3% loss ($30.00)
MAX_CONSECUTIVE_LOSSES = 3  # Pause bot after 3 straight losing trades

# ASSETS ROSTER
WEEKDAY_ASSETS = {
    "XAUUSD=X": "XAUUSD",
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "JPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "CAD=X": "USDCAD",
    "NZDUSD=X": "NZDUSD",
    "CHF=X": "USDCHF",
    "BTC-USD": "BTCUSD",
    "ETH-USD": "ETHUSD"
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD",
    "ETH-USD": "ETHUSD",
    "SOL-USD": "SOLUSD",
    "XRP-USD": "XRPUSD",
    "ADA-USD": "ADAUSD"
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

# --- SYSTEM STATE TRACKERS ---
last_signals = {}
authorized_users = set()
sent_messages = []

# Risk Protection State
daily_stats = {
    "date": datetime.date.today(),
    "pnl_usd": 0.0,
    "consecutive_losses": 0,
    "trading_paused": False,
    "pause_until": None
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# --- FLASK WEB SERVER ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "APA Signal Bot is live with Dynamic Risk Sizing, Async DB Pushing & Command Center!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# --- ASYNCHRONOUS SUPABASE SIGNAL PUSHER ---
async def push_to_supabase_async(symbol, action, entry, sl, tp):
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
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as res:
                if res.status in [200, 201]:
                    logging.info(f"Successfully pushed {symbol} signal to Supabase!")
                else:
                    err_text = await res.text()
                    logging.error(f"Supabase push error: {res.status} - {err_text}")
    except Exception as e:
        logging.error(f"Failed pushing signal to Supabase: {e}")

# --- MARKET DATA FETCHING ---
def fetch_data(ticker, interval, period="7d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
        if df is None or df.empty or len(df) < 50:
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

# --- DYNAMIC RISK LOT SIZE CALCULATOR (1% RULE) ---
def calculate_dynamic_lot(ticker, entry, sl_distance):
    """Calculates position size dynamically risking exactly 1% of equity ($10 on $1000 balance)."""
    risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE_PCT
    
    if sl_distance <= 0:
        return "0.01"

    # Crypto assets
    if ticker in ["BTC-USD", "ETH-USD"]:
        units = risk_amount / sl_distance
        return f"{max(0.01, round(units, 2)):.2f}"
    elif ticker == "SOL-USD":
        units = risk_amount / sl_distance
        return f"{max(0.10, round(units, 2)):.2f}"
    elif ticker in ["XRP-USD", "ADA-USD"]:
        units = risk_amount / sl_distance
        return f"{max(10.0, round(units, 0)):.0f}"
    # Forex & Gold
    elif ticker == "XAUUSD=X":
        lot = risk_amount / (sl_distance * 100)
        return f"{max(0.01, round(lot, 2)):.2f}"
    else:
        return "0.01"

# --- SIGNAL CALCULATION WITH SPREAD/VOLATILITY FILTER ---
def get_signal(ticker):
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="30d")
    if df_h1 is None or 'ema200_h1' not in df_h1 or len(df_h1) < 2:
        return None, None, None, None, None, None

    last_h1_row = df_h1.iloc[-2]
    if pd.isna(last_h1_row['Close']) or pd.isna(last_h1_row['ema200_h1']):
        return None, None, None, None, None, None

    last_h1_close = float(last_h1_row['Close'])
    h1_ema200 = float(last_h1_row['ema200_h1'])
    h1_trend = "BULLISH" if last_h1_close > h1_ema200 else "BEARISH"

    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 3:
        return None, None, None, None, None, None

    last_closed_candle = df_m15.iloc[-2]
    required_cols = ['Close', 'High', 'Low', 'ema50', 'rsi14', 'atr14']
    if any(pd.isna(last_closed_candle[col]) for col in required_cols):
        return None, None, None, None, None, None

    close = float(last_closed_candle['Close'])
    ema50 = float(last_closed_candle['ema50'])
    rsi = float(last_closed_candle['rsi14'])
    atr = float(last_closed_candle['atr14'])

    # SPREAD / VOLATILITY FILTER
    candle_range = float(last_closed_candle['High']) - float(last_closed_candle['Low'])
    if candle_range > (atr * 3.0):
        logging.warning(f"[{ticker}] Trade skipped: Abnormal spread/volatility spike detected ({candle_range:.4f} vs ATR {atr:.4f})")
        return None, None, None, None, None, None

    sig = None
    if h1_trend == "BULLISH" and close > ema50 and rsi > 55:
        sig = "BUY"
    elif h1_trend == "BEARISH" and close < ema50 and rsi < 45:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None

    sl_distance = atr * 1.5
    tp_distance = atr * 3.0

    # Universal Broker Minimum Buffers
    if ticker in ["EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "CAD=X", "CHF=X"]:
        sl_distance = max(sl_distance, 0.0015)
        tp_distance = sl_distance * 2.0
    elif ticker == "JPY=X":
        sl_distance = max(sl_distance, 0.150)
        tp_distance = sl_distance * 2.0
    elif ticker == "XAUUSD=X":
        sl_distance = max(sl_distance, 3.50)
        tp_distance = sl_distance * 2.0
    elif ticker in ["ETH-USD", "BTC-USD", "SOL-USD", "XRP-USD", "ADA-USD"]:
        sl_distance = max(sl_distance, close * 0.015)
        tp_distance = sl_distance * 2.0

    if sig == "BUY":
        tp = close + tp_distance
        sl = close - sl_distance
        be_level = close + (atr * 1.0)
    else:
        tp = close - tp_distance
        sl = close + sl_distance
        be_level = close - (atr * 1.0)

    rec_lot = calculate_dynamic_lot(ticker, close, sl_distance)

    return sig, close, sl, tp, be_level, rec_lot

# --- CIRCUIT BREAKER CHECKER ---
def check_circuit_breaker():
    """Daily drawdown and consecutive loss protection check."""
    global daily_stats
    today = datetime.date.today()
    
    # Reset stats on new day
    if daily_stats["date"] != today:
        daily_stats = {
            "date": today,
            "pnl_usd": 0.0,
            "consecutive_losses": 0,
            "trading_paused": False,
            "pause_until": None
        }
        return True, "Trading Active"

    if daily_stats["trading_paused"]:
        if daily_stats["pause_until"] and datetime.datetime.now() < daily_stats["pause_until"]:
            return False, f"Trading paused until {daily_stats['pause_until'].strftime('%H:%M WAT')} due to Daily Risk Limit."
        elif daily_stats["pause_until"] is None:
            return False, "Trading manually paused by administrator."
        else:
            daily_stats["trading_paused"] = False

    max_loss_usd = ACCOUNT_BALANCE * MAX_DAILY_LOSS_PCT
    if abs(daily_stats["pnl_usd"]) >= max_loss_usd or daily_stats["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        daily_stats["trading_paused"] = True
        daily_stats["pause_until"] = datetime.datetime.now() + datetime.timedelta(hours=24)
        return False, "🚨 CIRCUIT BREAKER TRIGGERED: Daily Loss / Consecutive Loss Limit Reached. Bot paused for 24 hours."

    return True, "Trading Active"

# --- TELEGRAM USER AUTHORIZATION & COMMAND HANDLERS ---
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message and update.message.text else ""

    if text == BOT_PASSCODE or text == f"/start {BOT_PASSCODE}":
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 Passcode accepted! APA Risk-Engine Signal Bot is active.")
    elif user_id in authorized_users:
        is_active, status_msg = check_circuit_breaker()
        await update.message.reply_text(f"🟢 APA Bot Status: {status_msg}")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Send correct passcode.", parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Query live trading and circuit breaker stats."""
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 Unauthorized.")
        return
        
    is_active, status_reason = check_circuit_breaker()
    status_msg = (
        f"⚙️ *APA SYSTEM COMMAND CENTER*\n\n"
        f"• *Status:* {'🟢 Active' if is_active else '🔴 Paused'}\n"
        f"• *Details:* {status_reason}\n"
        f"• *Daily PnL:* `${daily_stats['pnl_usd']:.2f}`\n"
        f"• *Consecutive Losses:* `{daily_stats['consecutive_losses']}`\n"
        f"• *Account Equity:* `${ACCOUNT_BALANCE:.2f}`"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually toggle pause on the bot."""
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 Unauthorized.")
        return

    global daily_stats
    daily_stats["trading_paused"] = not daily_stats["trading_paused"]
    daily_stats["pause_until"] = None if not daily_stats["trading_paused"] else datetime.datetime.now() + datetime.timedelta(hours=24)
    
    state_str = "🔴 Bot manually PAUSED for 24 hours." if daily_stats["trading_paused"] else "🟢 Bot manually RESUMED."
    await update.message.reply_text(state_str)

# --- GLOBAL ERROR HANDLER ---
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs uncaught telegram exceptions without crashing background tasks."""
    logging.error(f"Global exception caught: {context.error}", exc_info=context.error)

# --- 24-HOUR AUTOMATIC MESSAGE CLEANUP LOOP ---
async def auto_cleanup_loop(app):
    global sent_messages
    while True:
        try:
            now_ts = time.time()
            cutoff_ts = now_ts - (24 * 3600)
            
            remaining_messages = []
            for msg_id, ts in sent_messages:
                if ts < cutoff_ts:
                    try:
                        await app.bot.delete_message(chat_id=TELEGRAM_CHAT_ID, message_id=msg_id)
                        logging.info(f"Cleaned up 24h+ old message (ID: {msg_id})")
                    except Exception as e:
                        logging.warning(f"Could not delete message {msg_id}: {e}")
                else:
                    remaining_messages.append((msg_id, ts))
            
            sent_messages = remaining_messages
        except Exception as e:
            logging.error(f"Auto-cleanup error: {e}")

        await asyncio.sleep(600)

# --- HOURLY HEARTBEAT TASK ---
async def heartbeat_loop(app):
    while True:
        try:
            wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            formatted_wat = wat_time.strftime("%I:%M %p WAT")
            
            is_active, status_msg = check_circuit_breaker()
            status_icon = "🟢" if is_active else "🔴"
            
            msg_text = f"{status_icon} *[Bot Heartbeat]* APA Signal Bot Status: {status_msg} ({formatted_wat})"
            msg = await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg_text, parse_mode="Markdown")
            
            sent_messages.append((msg.message_id, time.time()))
        except Exception as e:
            logging.error(f"Heartbeat failed: {e}")
            
        await asyncio.sleep(3600)

# --- MAIN SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals
    try:
        init_msg = await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text="🚀 *APA Signal Bot Active with Dynamic 1% Risk Sizing & Command Center!*", 
            parse_mode="Markdown"
        )
        sent_messages.append((init_msg.message_id, time.time()))
    except Exception as e:
        logging.error(f"Failed to send startup alert: {e}")

    while True:
        try:
            is_active, status_reason = check_circuit_breaker()
            if not is_active:
                logging.info(f"Signal loop paused: {status_reason}")
                await asyncio.sleep(300)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                sig, entry, sl, tp, be_level, rec_lot = get_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    
                    # Decimal Precision Formatting
                    if ticker in ["JPY=X"]:
                        dec = 3
                    elif ticker in ["EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "CAD=X", "CHF=X", "XRP-USD", "ADA-USD"]:
                        dec = 4
                    else:
                        dec = 2

                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    expires_wat = now_wat + datetime.timedelta(minutes=15)

                    time_sent_str = now_wat.strftime("%I:%M %p")
                    time_expire_str = expires_wat.strftime("%I:%M %p")

                    msg_text = (
                        f"📊 *APA SIGNAL ALERT (RISK PROTECTED)* 📊\n\n"
                        f"*Asset:* {label}\n"
                        f"*Order Type:* {sig}\n\n"
                        f"• *Entry:* `{entry:.{dec}f}`\n"
                        f"• *Stop Loss:* `{sl:.{dec}f}`\n"
                        f"• *Take Profit:* `{tp:.{dec}f}`\n"
                        f"• *Breakeven Trigger:* `{be_level:.{dec}f}` (Move SL to Entry)\n"
                        f"• *Rec. Lot Size (1% Risk):* `{rec_lot}`\n\n"
                        f"🕒 *Sent:* `{time_sent_str} WAT`\n"
                        f"⏳ *Validity:* Active until `{time_expire_str} WAT`"
                    )
                    
                    msg = await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg_text, parse_mode="Markdown")
                    sent_messages.append((msg.message_id, time.time()))
                    
                    # Async database push without blocking execution thread
                    asyncio.create_task(push_to_supabase_async(symbol=label, action=sig, entry=entry, sl=sl, tp=tp))

        except Exception as e:
            logging.error(f"Loop error caught: {e}")

        await asyncio.sleep(60)

# --- ENTRY POINT ---
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", handle_text_message))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))
    
    # Error Handler
    app.add_error_handler(global_error_handler)

    Thread(target=run_flask, daemon=True).start()

    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat_loop(app))
    asyncio.create_task(auto_cleanup_loop(app))

    print("Signal Bot active with Risk Circuit Breaker & Command Center...")

    async with app:
        await app.start()
        await app.updater.start_polling()
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
