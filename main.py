import asyncio
import datetime
import json
import logging
import os
import pandas as pd
import requests
import ta
import yfinance as yf
from flask import Flask, jsonify
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --- CONFIGURATION & SECURITY ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_PASSCODE = os.getenv("BOT_PASSCODE")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", TELEGRAM_CHAT_ID)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not BOT_PASSCODE:
    raise ValueError("CRITICAL SECURITY ERROR: Missing required environment variables.")

RISK_PER_TRADE_PCT = 0.01  # Strict 1% risk profile per trade (Jim Simons math sizing)

# --- ACCOUNT BALANCE & STATE MANAGEMENT ---
current_account_balance = 20.00  # Starting base capital ($20)
bot_active = True  

today_date = datetime.datetime.now(datetime.timezone.utc).date()

# --- ASSET UNIVERSE (The 6 Core Pairs) ---
WEEKDAY_ASSETS = {
    "BTC-USD": "BTCUSD",
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "JPY=X": "USDJPY",
    "GC=F": "XAUUSD",
    "ETH-USD": "ETHUSD"
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD",
    "ETH-USD": "ETHUSD"
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signal_timestamps = {}
authorized_users = set()

# --- PERSISTENT FILE STORAGE ---
HISTORY_FILE = "history.json"
TRADES_FILE = "active_trades.json"

trade_history = []  
active_signals = {}  

def load_data_from_disk():
    global trade_history, active_signals, current_account_balance, today_date
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    trade_history = data.get("history", [])
                    current_account_balance = float(data.get("balance", 20.00))
        except Exception as e:
            logging.error(f"Error loading history: {e}")
            trade_history = []

    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                active_signals = json.load(f)
        except Exception as e:
            logging.error(f"Error loading active signals: {e}")
            active_signals = {}

def save_trade_history():
    try:
        data = {
            "history": trade_history,
            "balance": current_account_balance,
            "date": today_date.isoformat()
        }
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"Error saving history: {e}")

load_data_from_disk()

def record_trade_outcome(outcome, pnl_amount=0.0, ticker_label=None):
    global trade_history, today_date, current_account_balance
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    if now_date != today_date:
        today_date = now_date

    trade_record = {
        "outcome": outcome,
        "pnl": pnl_amount,
        "ticker": ticker_label,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    trade_history.append(trade_record)
    current_account_balance += pnl_amount
    save_trade_history()

def get_quantitative_performance_metrics():
    total_trades = len(trade_history)
    if total_trades == 0:
        return 0.0, 0, 0, 0.0
    
    wins = sum(1 for t in trade_history if t.get("outcome") == "WIN")
    losses = sum(1 for t in trade_history if t.get("outcome") == "LOSS")
    win_rate = (wins / total_trades) * 100.0

    gross_profit = sum(t.get("pnl", 0.0) for t in trade_history if t.get("pnl", 0.0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0.0) for t in trade_history if t.get("pnl", 0.0) < 0))
    
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
    return win_rate, wins, losses, profit_factor

# --- FLASK WEB SERVER FOR 24/7 HOSTING ---
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return jsonify({"status": "ACTIVE" if bot_active else "PAUSED", "code": 200}), 200

def self_ping_loop():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "https://signal-bot-t8ud.onrender.com/health")
    while True:
        try:
            requests.get(app_url, timeout=10)
        except Exception as e:
            logging.error(f"Self-ping error: {e}")
        import time
        time.sleep(180) 

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- MATHEMATICAL POSITION SIZING (1% Risk) ---
def calculate_dynamic_lot(ticker, sl_distance):
    global current_account_balance
    risk_dollar_amount = current_account_balance * RISK_PER_TRADE_PCT
    
    if sl_distance <= 0:
        return None

    if "USD" in ticker and "EUR" not in ticker and "GBP" not in ticker:
        risk_per_lot = sl_distance * 1.0
    else:
        sl_pips = sl_distance * 10000
        risk_per_lot = sl_pips * 10.0

    if risk_per_lot <= 0:
        return None

    calculated_lot = risk_dollar_amount / risk_per_lot
    min_lot = 0.01

    if calculated_lot < min_lot:
        return min_lot
        
    return round(calculated_lot, 2)

# --- PURE MATHEMATICAL DATA FETCHER & INDICATORS ---
def fetch_data(ticker, interval, period="60d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False, timeout=15)
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        df.columns = [str(col).capitalize() for col in df.columns]
        required_cols = ['High', 'Low', 'Close', 'Open']
        if not all(col in df.columns for col in required_cols):
            return None

        high_series = pd.to_numeric(df['High'].squeeze(), errors='coerce')
        low_series = pd.to_numeric(df['Low'].squeeze(), errors='coerce')
        close_series = pd.to_numeric(df['Close'].squeeze(), errors='coerce')

        if len(close_series.dropna()) < 200:
            return None

        # Pure Statistical & Volatility Metrics
        df['atr14'] = ta.volatility.average_true_range(high_series, low_series, close_series, window=14)
        df['sma50'] = ta.trend.sma_indicator(close_series, window=50)
        df['sma200'] = ta.trend.sma_indicator(close_series, window=200)
        
        # Rolling Z-Score Calculation (Statistical Mean Reversion Math)
        rolling_mean = close_series.rolling(window=50).mean()
        rolling_std = close_series.rolling(window=50).std()
        df['z_score'] = (close_series - rolling_mean) / rolling_std

        return df
    except Exception as e:
        logging.error(f"Mathematical fetch error for {ticker}: {e}")
        return None

# --- QUANTITATIVE SIGNAL CORE ---
def get_strategy_signal(ticker):
    try:
        df = fetch_data(ticker, interval=TIMEFRAME_M15, period="10d")
        if df is None or len(df) < 50:
            return None, None, None, None, None, None, 0.0

        c = df.iloc[-2]
        close_p = float(c['Close'])
        z_score = float(c['z_score']) if pd.notna(c['z_score']) else 0.0
        atr = float(c['atr14']) if pd.notna(c['atr14']) else 0.0
        candle_time = pd.to_datetime(df.index[-2])

        sig = None
        # Statistical Z-Score Thresholds (> 2.0 standard deviations deviation triggers mean reversion)
        if z_score < -2.0:
            sig = "BUY"
        elif z_score > 2.0:
            sig = "SELL"

        if not sig:
            return None, None, None, None, None, None, 0.0

        # Dynamic Stop Loss and Take Profit based on ATR Volatility
        if "EUR" in ticker or "GBP" in ticker or "USD" in ticker:
            entry = round(close_p, 4)
            sl_dist = atr * 1.5
            if sig == "BUY":
                sl = round(entry - sl_dist, 4)
                tp = round(entry + (sl_dist * 2.0), 4)
            else:
                sl = round(entry + sl_dist, 4)
                tp = round(entry - (sl_dist * 2.0), 4)
        else:
            entry = round(close_p, 2)
            sl_dist = max(atr * 1.5, 50.0)
            if sig == "BUY":
                sl = round(entry - sl_dist, 2)
                tp = round(entry + (sl_dist * 2.0), 2)
            else:
                sl = round(entry + sl_dist, 2)
                tp = round(entry - (sl_dist * 2.0), 2)

        rec_lot = calculate_dynamic_lot(ticker, sl_dist)
        if rec_lot is None:
            return None, None, None, None, None, None, 0.0

        quality_score = min(abs(z_score) * 35.0, 99.0)
        return sig, entry, sl, tp, rec_lot, candle_time, quality_score
    except Exception as e:
        logging.error(f"Signal calculation error for {ticker}: {e}")
        return None, None, None, None, None, None, 0.0

# --- TELEGRAM INTERACTION ---
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_to_channel":
        original_text = query.message.text
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID, 
                text=original_text, 
                parse_mode="Markdown"
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ **Published signal to Channel.**", parse_mode="Markdown")
        except Exception as e:
            await query.message.reply_text(f"❌ Failed: {e}", parse_mode="Markdown")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text(
            f"🔓 **Aura Quantum Demo Engine Active**\nBalance: `${current_account_balance:.2f}`\n\n*Rule:* Pure mathematical Z-score mean reversion active.", 
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    win_rate, wins, losses, profit_factor = get_quantitative_performance_metrics()
    text = (
        f"📊 **Aura Quantum Status:** `Active`\n"
        f"• Win Rate: `{win_rate:.1f}%` ({wins}W/{losses}L)\n"
        f"• Profit Factor: `{profit_factor:.2f}`\n"
        f"• Balance: `${current_account_balance:.2f}`\n"
        f"• Circuit Breakers: `Disabled (Simons Law of Large Numbers Active)`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    args = context.args
    if args and len(args) >= 2 and args[0].upper() in ["WIN", "LOSS"]:
        outcome = args[0].upper()
        try:
            pnl_val = float(args[1])
            record_trade_outcome(outcome, pnl_amount=pnl_val)
            await update.message.reply_text(f"🧹 **Logged Trade:** `{outcome}` | PnL: `${pnl_val:.2f}` | Balance: `${current_account_balance:.2f}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ Format: `/clear WIN 0.50`", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Format: `/clear WIN [pnl]` or `/clear LOSS [pnl]`", parse_mode="Markdown")

# --- BACKGROUND SIGNAL LOOP ---
async def signal_loop(app):
    global last_signal_timestamps
    while True:
        try:
            if not bot_active:
                await asyncio.sleep(10)
                continue

            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            all_assets = {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}

            for ticker, label in all_assets.items():
                sig, entry, sl, tp, rec_lot, candle_time, quality_score = get_strategy_signal(ticker)
                if sig is None:
                    continue

                candle_str = str(candle_time)
                if last_signal_timestamps.get(ticker) != candle_str:
                    last_signal_timestamps[ticker] = candle_str
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    signal_text = (
                        f"⚡ **Aura Quantum Signal [Score: {quality_score:.0f}]**\n"
                        f"{label} {dir_icon} {sig}\n"
                        f"Entry: `{entry}`\n"
                        f"SL: `{sl}`\n"
                        f"TP: `{tp}`\n"
                        f"Rec Lot: `{rec_lot}` (1% Risk)"
                    )

                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Post to Channel", callback_data="post_to_channel")]])

                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=signal_text, 
                            parse_mode="Markdown",
                            reply_markup=keyboard
                        )
        except Exception as e:
            logging.error(f"Signal loop error: {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(15)

async def post_init(app):
    asyncio.create_task(signal_loop(app))

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CallbackQueryHandler(button_click_handler))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    ping_thread = Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    logging.info("Aura Quantum Demo Engine Running...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
