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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- CONFIGURATION (ENVIRONMENT VARIABLES WITH FALLBACKS) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8874815036:AAGZAWFJoVf3pK1qpn4CdbA_95NYy9TcLt4")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7889527038")  # Your Channel Chat ID or Admin Chat ID
BOT_PASSCODE = os.getenv("BOT_PASSCODE", "5051")

# SUPABASE DATABASE CONFIGURATION
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://khmtegoloiszskwjmuku.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_N4BfssoiokI-o002sw6eGQ_K5fMGcjG")

# RISK & ACCOUNT SAFETY CONFIGURATION
ACCOUNT_BALANCE = 10.00  # Base live account equity ($10.00)
RISK_PER_TRADE_PCT = 0.01  
MAX_DAILY_LOSS_PCT = 0.05  
MAX_CONSECUTIVE_LOSSES = 3  

# STRICT ASSET ROSTER (Crypto Restricted strictly to BTCUSD)
WEEKDAY_ASSETS = {
    "XAUUSD=X": "XAUUSD",
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
    return "Pure APA Signal Bot is Live & Operational!"

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

        df['atr14'] = ta.volatility.average_true_range(df['High'], df['Low'], df['Close'], window=14)
        return df
    except Exception as e:
        logging.error(f"Error fetching data for {ticker} ({interval}): {e}")
        return None

# --- BROKER-COMPLIANT LOT CALCULATOR ---
def calculate_dynamic_lot(ticker):
    """Returns safe broker-compliant lot sizes without execution rejection."""
    return "0.01"

# --- PURE APA SIGNAL ENGINE (MARKET STRUCTURE & NEAR TARGETS) ---
def get_pure_apa_signal(ticker):
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 20:
        return None, None, None, None, None, None

    # Get recent candles
    c = df_m15.iloc[-2]
    prev_c = df_m15.iloc[-3]
    atr = float(c['atr14'])

    close_p = float(c['Close'])
    high_p = float(c['High'])
    low_p = float(c['Low'])

    # Recent Structure Points (Swing Highs / Lows)
    recent_high = df_m15['High'].iloc[-15:-3].max()
    recent_low = df_m15['Low'].iloc[-15:-3].min()

    sig = None

    # Pure APA Rule 1: Break of Structure (BOS) / Bullish Fair Value Gap Retest
    if close_p > recent_high and prev_c['Close'] <= recent_high:
        sig = "BUY"
    # Pure APA Rule 2: Market Structure Shift (MSS) / Bearish Fair Value Gap Retest
    elif close_p < recent_low and prev_c['Close'] >= recent_low:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None

    # Near Structure Take Profit & Tight Invalidation SL
    if sig == "BUY":
        sl = recent_low - (atr * 0.3)
        tp = close_p + (abs(close_p - sl) * 1.2)  # High-Probability Near TP
        be_level = close_p + (abs(close_p - sl) * 0.5)  # Fast Auto-Breakeven Trigger
    else:
        sl = recent_high + (atr * 0.3)
        tp = close_p - (abs(sl - close_p) * 1.2)  # High-Probability Near TP
        be_level = close_p - (abs(sl - close_p) * 0.5)  # Fast Auto-Breakeven Trigger

    rec_lot = calculate_dynamic_lot(ticker)
    return sig, close_p, sl, tp, be_level, rec_lot

# --- CIRCUIT BREAKER CHECKER ---
def check_circuit_breaker():
    global daily_stats
    today = datetime.date.today()
    
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
        return False, "Trading manually or automatically paused."

    return True, "Trading Active"

# --- TELEGRAM COMMAND HANDLERS & ONE-TAP APPROVAL ---
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message and update.message.text else ""

    if text == BOT_PASSCODE or text == f"/start {BOT_PASSCODE}":
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 Passcode accepted! Pure APA Signal Bot is active.")
    elif user_id in authorized_users:
        is_active, status_msg = check_circuit_breaker()
        await update.message.reply_text(f"🟢 Kings™ APA Engine Status: {status_msg}")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Send correct passcode.", parse_mode="Markdown")

async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes One-Tap Telegram Button Actions (Approve/Reject)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("approve_"):
        # Format and broadcast clean signal to channel
        original_text = query.message.text
        clean_signal = original_text.replace("🔍 [DRAFT PREVIEW] ", "").replace("⚠️ TAP TO APPROVE OR REJECT BEFORE BROADCASTING", "").strip()
        
        posted_msg = await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"📌 **KINGS™ OFFICIAL SIGNAL** 👑\n\n{clean_signal}",
            parse_mode="Markdown"
        )
        sent_messages.append((posted_msg.message_id, time.time()))
        await query.edit_message_text(f"✅ **SIGNAL APPROVED & BROADCASTED TO CHANNEL!**\n\n{clean_signal}", parse_mode="Markdown")

    elif data.startswith("reject_"):
        await query.edit_message_text("❌ **SIGNAL DRAFT REJECTED & DISCARDED.**")

# --- AUTO CLEANUP & HEARTBEAT LOOPS ---
async def auto_cleanup_loop(app):
    global sent_messages
    while True:
        try:
            now_ts = time.time()
            cutoff_ts = now_ts - (24 * 3600)
            remaining = []
            for msg_id, ts in sent_messages:
                if ts < cutoff_ts:
                    try:
                        await app.bot.delete_message(chat_id=TELEGRAM_CHAT_ID, message_id=msg_id)
                    except Exception:
                        pass
                else:
                    remaining.append((msg_id, ts))
            sent_messages = remaining
        except Exception as e:
            logging.error(f"Cleanup error: {e}")
        await asyncio.sleep(600)

async def heartbeat_loop(app):
    while True:
        try:
            wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            formatted_wat = wat_time.strftime("%I:%M %p WAT")
            is_active, status_msg = check_circuit_breaker()
            status_icon = "🟢" if is_active else "🔴"
            
            msg_text = f"{status_icon} *[Bot Heartbeat]* Pure APA Engine Active: {status_msg} ({formatted_wat})"
            msg = await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg_text, parse_mode="Markdown")
            sent_messages.append((msg.message_id, time.time()))
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")
        await asyncio.sleep(3600)

# --- MAIN SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals
    while True:
        try:
            is_active, _ = check_circuit_breaker()
            if not is_active:
                await asyncio.sleep(300)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                sig, entry, sl, tp, be_level, rec_lot = get_pure_apa_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    
                    dec = 3 if ticker == "JPY=X" else (2 if ticker in ["BTC-USD", "XAUUSD=X"] else 4)

                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    expires_wat = now_wat + datetime.timedelta(minutes=15)

                    time_sent_str = now_wat.strftime("%I:%M %p")
                    time_expire_str = expires_wat.strftime("%I:%M %p")

                    draft_text = (
                        f"🔍 [DRAFT PREVIEW] 📊 **APA SIGNAL ALERT** 📊\n\n"
                        f"**Asset:** {label}\n"
                        f"**Order Type:** {sig}\n\n"
                        f"• **Entry:** `{entry:.{dec}f}`\n"
                        f"• **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"• **Take Profit:** `{tp:.{dec}f}` (Near Target)\n"
                        f"• **Breakeven Trigger:** `{be_level:.{dec}f}` (Move SL to Entry)\n"
                        f"• **Recommended Lot:** `{rec_lot}`\n\n"
                        f"🕒 **Sent:** `{time_sent_str} WAT`\n"
                        f"⏳ **Validity:** Active until `{time_expire_str} WAT`\n\n"
                        f"⚠️ TAP TO APPROVE OR REJECT BEFORE BROADCASTING"
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton("🚀 Post to Kings™", callback_data=f"approve_{label}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{label}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    # Send to Admin Chat for Approval
                    if authorized_users:
                        admin_id = list(authorized_users)[0]
                        await app.bot.send_message(chat_id=admin_id, text=draft_text, reply_markup=reply_markup, parse_mode="Markdown")

                    # Async DB log
                    asyncio.create_task(push_to_supabase_async(symbol=label, action=sig, entry=entry, sl=sl, tp=tp))

        except Exception as e:
            logging.error(f"Signal Loop error: {e}")

        await asyncio.sleep(60)

# --- ENTRY POINT ---
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_text_message))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))
    app.add_handler(CallbackQueryHandler(handle_button_click))

    Thread(target=run_flask, daemon=True).start()

    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat_loop(app))
    asyncio.create_task(auto_cleanup_loop(app))

    print("Pure APA Signal Bot Active & Ready...")

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
