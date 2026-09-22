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
from telegram.request import HTTPXRequest
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- CONFIGURATION (ENVIRONMENT VARIABLES WITH FALLBACKS) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8874815036:AAGZAWFJoVf3pK1qpn4CdbA_95NYy9TcLt4")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7889527038") # Admin Personal Chat ID for Draft Approvals
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "-1003723594631") # Kings™ Private Channel ID
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
    return "Institutional Pure APA Engine is Live & Operational!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# --- NEWS & BANK HOLIDAY GUARD API ---
async def check_news_blackout():
    """Checks if today is a major US/UK bank holiday or blackout day using Nager.Date API."""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    year = datetime.date.today().year
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://date.nager.at/api/v3/PublicHolidays/{year}/US", timeout=aiohttp.ClientTimeout(total=4)) as res:
                if res.status == 200:
                    holidays = await res.json()
                    for h in holidays:
                        if h.get("date") == today_str:
                            logging.info(f"News Guard: US Bank Holiday Detected ({h.get('name')}). Pausing signal generation.")
                            return True
    except Exception as e:
        logging.error(f"News Guard check error: {e}")
    return False

# --- SESSION KILLZONE TIME FILTER ---
def is_in_session_killzone(ticker):
    """Restricts Forex & Gold signals strictly to London and New York Session Open hours."""
    if ticker == "BTC-USD":
        return True  # Crypto trades 24/7

    now_utc = datetime.datetime.now(datetime.timezone.utc).time()
    
    # London Killzone: 07:00 UTC - 11:00 UTC (8:00 AM - 12:00 PM WAT)
    london_start = datetime.time(7, 0)
    london_end = datetime.time(11, 0)

    # New York Killzone: 12:00 UTC - 16:00 UTC (1:00 PM - 5:00 PM WAT)
    ny_start = datetime.time(12, 0)
    ny_end = datetime.time(16, 0)

    in_london = london_start <= now_utc <= london_end
    in_ny = ny_start <= now_utc <= ny_end

    return in_london or in_ny

# --- ASYNCHRONOUS SUPABASE SIGNAL PUSHER ---
async def push_to_supabase_async(symbol, action, entry, sl, tp1, tp2):
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
        "tp": float(tp1),
        "tp2": float(tp2)
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
        df['ema200'] = ta.trend.ema_indicator(df['Close'], window=200)
        return df
    except Exception as e:
        logging.error(f"Error fetching data for {ticker} ({interval}): {e}")
        return None

# --- BROKER-COMPLIANT LOT CALCULATOR ---
def calculate_dynamic_lot(ticker):
    return "0.01"

# --- HIGHER TIMEFRAME (H1) TREND DETERMINATION ---
def get_h1_trend_bias(ticker):
    """Fetches H1 data to establish higher timeframe directional bias."""
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="14d")
    if df_h1 is None or len(df_h1) < 20:
        return "NEUTRAL"
    
    latest_close = float(df_h1['Close'].iloc[-1])
    latest_ema200 = float(df_h1['ema200'].iloc[-1]) if 'ema200' in df_h1 else latest_close

    h1_recent_high = df_h1['High'].iloc[-10:-1].max()
    h1_recent_low = df_h1['Low'].iloc[-10:-1].min()

    if latest_close > latest_ema200 and latest_close > h1_recent_high * 0.998:
        return "BULLISH"
    elif latest_close < latest_ema200 and latest_close < h1_recent_low * 1.002:
        return "BEARISH"
    
    return "NEUTRAL"

# --- INSTITUTIONAL PURE APA SIGNAL ENGINE ---
def get_pure_apa_signal(ticker):
    # Step 1: Session Time Filter Check
    if not is_in_session_killzone(ticker):
        return None, None, None, None, None, None, None

    # Step 2: Higher Timeframe Bias Check
    h1_bias = get_h1_trend_bias(ticker)

    # Step 3: Fetch M15 Data for Execution Entry
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 20:
        return None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    prev_c = df_m15.iloc[-3]
    atr = float(c['atr14'])

    close_p = float(c['Close'])

    recent_high = df_m15['High'].iloc[-15:-3].max()
    recent_low = df_m15['Low'].iloc[-15:-3].min()

    sig = None

    # Pure APA Rule 1: Break of Structure (BOS) / FVG Retest aligned with H1 BULLISH Bias
    if close_p > recent_high and prev_c['Close'] <= recent_high:
        if h1_bias in ["BULLISH", "NEUTRAL"]:
            sig = "BUY"

    # Pure APA Rule 2: Market Structure Shift (MSS) / FVG Retest aligned with H1 BEARISH Bias
    elif close_p < recent_low and prev_c['Close'] >= recent_low:
        if h1_bias in ["BEARISH", "NEUTRAL"]:
            sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None

    # Multi-Target Structure (TP1: Quick Near TP | TP2: Runner Target)
    risk_distance = abs(close_p - (recent_low if sig == "BUY" else recent_high)) + (atr * 0.3)

    if sig == "BUY":
        sl = close_p - risk_distance
        tp1 = close_p + (risk_distance * 1.0)  # Near Target (High Probability)
        tp2 = close_p + (risk_distance * 2.0)  # Extended Runner Target
        be_level = close_p + (risk_distance * 0.5)  # Fast Auto-Breakeven Trigger
    else:
        sl = close_p + risk_distance
        tp1 = close_p - (risk_distance * 1.0)  # Near Target (High Probability)
        tp2 = close_p - (risk_distance * 2.0)  # Extended Runner Target
        be_level = close_p - (risk_distance * 0.5)  # Fast Auto-Breakeven Trigger

    rec_lot = calculate_dynamic_lot(ticker)
    return sig, close_p, sl, tp1, tp2, be_level, rec_lot

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
        await update.message.reply_text("🔓 Passcode accepted! Institutional APA Signal Engine is active.")
    elif user_id in authorized_users:
        is_active, status_msg = check_circuit_breaker()
        await update.message.reply_text(f"🟢 Kings™ Institutional Engine Status: {status_msg}")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Send correct passcode in direct messages.", parse_mode="Markdown")

async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("approve_"):
        original_text = query.message.text
        
        # Clean formatting for public channel broadcast
        clean_signal = (
            original_text
            .replace("🔍 [DRAFT PREVIEW] ", "")
            .replace("⚠️ TAP TO APPROVE OR REJECT BEFORE BROADCASTING", "")
            .strip()
        )
        
        try:
            # Direct Broadcast to Public/Private Kings™ Channel
            posted_msg = await context.bot.send_message(
                chat_id=CHANNEL_CHAT_ID,
                text=f"📌 **KINGS™ OFFICIAL SIGNAL** 👑\n\n{clean_signal}",
                parse_mode="Markdown"
            )
            sent_messages.append((posted_msg.message_id, time.time()))
            
            # Edit draft message in admin chat to confirm action
            await query.edit_message_text(f"✅ **SIGNAL BROADCASTED TO KINGS™ CHANNEL!**\n\n{clean_signal}", parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Broadcast error: {e}")
            await query.edit_message_text(
                f"⚠️ **BROADCAST FAILED!**\n\n"
                f"Please ensure the bot is added as an **Administrator** in your channel with 'Post Messages' rights.\n\n"
                f"Current `CHANNEL_CHAT_ID`: `{CHANNEL_CHAT_ID}`\n"
                f"Error details: `{e}`", 
                parse_mode="Markdown"
            )

    elif data.startswith("reject_"):
        await query.edit_message_text("❌ **SIGNAL DRAFT DISCARDED.**")

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
                        await app.bot.delete_message(chat_id=CHANNEL_CHAT_ID, message_id=msg_id)
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
            
            msg_text = f"{status_icon} *[Bot Heartbeat]* Institutional Engine Active: {status_msg} ({formatted_wat})"
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

            # Check for News/Holiday Blackout
            is_blackout = await check_news_blackout()
            if is_blackout:
                await asyncio.sleep(1800)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                sig, entry, sl, tp1, tp2, be_level, rec_lot = get_pure_apa_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    
                    dec = 3 if ticker == "JPY=X" else (2 if ticker in ["BTC-USD", "XAUUSD=X"] else 4)

                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    expires_wat = now_wat + datetime.timedelta(minutes=15)

                    time_sent_str = now_wat.strftime("%I:%M %p")
                    time_expire_str = expires_wat.strftime("%I:%M %p")

                    draft_text = (
                        f"🔍 [DRAFT PREVIEW] 📊 **INSTITUTIONAL APA ALERT** 📊\n\n"
                        f"**Asset:** {label}\n"
                        f"**Order Type:** {sig}\n\n"
                        f"• **Entry:** `{entry:.{dec}f}`\n"
                        f"• **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"• **Take Profit 1 (Near):** `{tp1:.{dec}f}`\n"
                        f"• **Take Profit 2 (Runner):** `{tp2:.{dec}f}`\n"
                        f"• **Breakeven Trigger:** `{be_level:.{dec}f}` (Move SL to Entry)\n"
                        f"• **Recommended Lot:** `{rec_lot}`\n\n"
                        f"🕒 **Sent:** `{time_sent_str} WAT`\n"
                        f"⏳ **Validity:** Active until `{time_expire_str} WAT`\n\n"
                        f"⚠️ TAP TO APPROVE OR REJECT BEFORE BROADCASTING"
                    )

                    # Subtle & Minimal Inline Buttons
                    keyboard = [
                        [
                            InlineKeyboardButton("🚀", callback_data=f"approve_{label}"),
                            InlineKeyboardButton("❌", callback_data=f"reject_{label}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    # Send draft to authorized admin chat
                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(chat_id=target_user, text=draft_text, reply_markup=reply_markup, parse_mode="Markdown")

                    asyncio.create_task(push_to_supabase_async(symbol=label, action=sig, entry=entry, sl=sl, tp1=tp1, tp2=tp2))

        except Exception as e:
            logging.error(f"Signal Loop error: {e}")

        await asyncio.sleep(60)

# --- ENTRY POINT ---
async def main():
    # Increase network request timeouts for Render cold-starts
    t_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0
    )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).build()

    app.add_handler(CommandHandler("start", handle_text_message))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))
    app.add_handler(CallbackQueryHandler(handle_button_click))

    Thread(target=run_flask, daemon=True).start()

    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat_loop(app))
    asyncio.create_task(auto_cleanup_loop(app))

    print("Institutional Pure APA Engine Active & Ready...")

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
