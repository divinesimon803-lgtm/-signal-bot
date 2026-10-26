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

RISK_PER_TRADE_PCT = 0.015  # 1.5% strict risk profile

# --- ACCOUNT BALANCE & STATE MANAGEMENT ---
current_account_balance = 10.00  
bot_active = True  

today_date = datetime.datetime.now(datetime.timezone.utc).date()
daily_losses_count = 0
daily_loss_limit_alert_sent = False

# --- ASSET UNIVERSE ---
WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",    
    "BTC-USD": "BTCUSD"  
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"  
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()

# --- PERSISTENT FILE STORAGE ---
HISTORY_FILE = "history.json"
TRADES_FILE = "active_trades.json"

trade_history = []  
active_trades = {}  

def load_data_from_disk():
    global trade_history, active_trades, current_account_balance, daily_losses_count, today_date
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    trade_history = data.get("history", [])
                    current_account_balance = float(data.get("balance", 10.00))
                    daily_losses_count = int(data.get("daily_losses", 0))
                    saved_date = data.get("date")
                    if saved_date:
                        parsed_date = datetime.datetime.fromisoformat(saved_date).date()
                        if parsed_date == datetime.datetime.now(datetime.timezone.utc).date():
                            today_date = parsed_date
                        else:
                            daily_losses_count = 0
        except Exception as e:
            logging.error(f"Error loading history: {e}")
            trade_history = []

    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                active_trades = json.load(f)
        except Exception as e:
            logging.error(f"Error loading active trades: {e}")
            active_trades = {}

def save_trade_history():
    try:
        data = {
            "history": trade_history,
            "balance": current_account_balance,
            "daily_losses": daily_losses_count,
            "date": today_date.isoformat()
        }
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"Error saving history: {e}")

def save_active_trades():
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(active_trades, f)
    except Exception as e:
        logging.error(f"Error saving active trades: {e}")

load_data_from_disk()

def record_trade_outcome(outcome, profit_loss_amount=0.0, ticker_label=None):
    global trade_history, today_date, daily_losses_count, daily_loss_limit_alert_sent, current_account_balance, last_signals
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    if now_date != today_date:
        today_date = now_date
        daily_losses_count = 0
        daily_loss_limit_alert_sent = False

    trade_history.append(outcome)
    current_account_balance = round(current_account_balance + profit_loss_amount, 2)
    
    if outcome == "LOSS":
        daily_losses_count += 1

    save_trade_history()

    if ticker_label:
        for tkey, tlabel in {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}.items():
            if tlabel == ticker_label:
                last_signals.pop(tkey, None)

def get_quantitative_performance_metrics():
    total_trades = len(trade_history)
    if total_trades == 0:
        return 0.0, 0, 0, 0.0
    
    wins = trade_history.count("WIN")
    losses = trade_history.count("LOSS")
    win_rate = (wins / total_trades) * 100.0
    profit_factor = (wins / losses) if losses > 0 else float(wins)
    return win_rate, wins, losses, profit_factor

# --- ECONOMIC NEWS CIRCUIT BREAKER ---
news_paused_state = False

def is_high_impact_news_window():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if now_utc.hour == 12 and 25 <= now_utc.minute <= 45:
        return True
    return False

def is_active_trading_session(ticker):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    weekday = now_utc.weekday()
    if ticker == "BTC-USD":
        return True
    if ticker == "GC=F":
        if weekday >= 5:  
            return False
        return True
    return True

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER ---
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

# --- DYNAMIC LOT SIZING ---
def calculate_dynamic_lot(ticker, sl_pips):
    global current_account_balance
    risk_amount = current_account_balance * RISK_PER_TRADE_PCT
    pip_value = 1.0 if ticker == "BTC-USD" else 0.1  

    if sl_pips <= 0:
        return 0.01

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    if current_account_balance <= 15.0:
        return 0.01  
    else:
        return max(0.01, min(calculated_lot, 5.0))

# --- DATA FETCHER & INDICATORS ---
def fetch_data(ticker, interval, period="5d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
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

        if len(close_series.dropna()) < 50:
            return None

        df['atr14'] = ta.volatility.average_true_range(high_series, low_series, close_series, window=14)
        df['ema50'] = ta.trend.ema_indicator(close_series, window=50)
        df['ema200'] = ta.trend.ema_indicator(close_series, window=200)
        df['rsi14'] = ta.momentum.rsi(close_series, window=14)
        
        rolling_mean = close_series.rolling(window=20).mean()
        rolling_std = close_series.rolling(window=20).std()
        df['z_score'] = (close_series - rolling_mean) / rolling_std

        return df
    except Exception as e:
        logging.error(f"Data fetch error for {ticker}: {e}")
        return None

def get_h1_trend_bias(ticker):
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="7d")
    if df_h1 is None or len(df_h1) < 50:
        return "NEUTRAL"
    
    latest_close = float(df_h1['Close'].iloc[-1])
    latest_ema50 = float(df_h1['ema50'].iloc[-1]) if pd.notna(df_h1['ema50'].iloc[-1]) else latest_close
    latest_ema200 = float(df_h1['ema200'].iloc[-1]) if pd.notna(df_h1['ema200'].iloc[-1]) else latest_close

    if latest_close > latest_ema50 and latest_ema50 > latest_ema200:
        return "BULLISH"
    elif latest_close < latest_ema50 and latest_ema50 < latest_ema200:
        return "BEARISH"
    return "NEUTRAL"

# --- STRATEGIES ---
def get_xauusd_strategy_signal(ticker):
    h1_bias = get_h1_trend_bias(ticker)
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="3d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['z_score']):
        return None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    rsi = float(c['rsi14'])
    z_score = float(c['z_score'])

    sig = None
    if z_score <= -1.8 and rsi < 35 and h1_bias != "BEARISH":
        sig = "BUY"
    elif z_score >= 1.8 and rsi > 65 and h1_bias != "BULLISH":
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    candle_time = pd.to_datetime(df_m15.index[-1])
    
    sl_distance = max(atr * 1.0, 1.50)  
    tp_distance = sl_distance * 2.5     

    if sig == "BUY":
        entry = round(live_price, 2)
        sl = round(entry - sl_distance, 2)
        tp = round(entry + tp_distance, 2)  
    else:
        entry = round(live_price, 2)
        sl = round(entry + sl_distance, 2)
        tp = round(entry - tp_distance, 2)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, rec_lot, rsi, f"XAUUSD (Z:{z_score:.2f})", candle_time

def get_bitcoin_strategy_signal(ticker):
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="2d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['z_score']):
        return None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    rsi = float(c['rsi14'])
    z_score = float(c['z_score'])

    sig = None
    if z_score <= -1.8 and rsi < 35:
        sig = "BUY"
    elif z_score >= 1.8 and rsi > 65:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    candle_time = pd.to_datetime(df_m15.index[-1])
    
    sl_distance = max(atr * 1.0, 25.0) 
    tp_distance = sl_distance * 2.5

    if sig == "BUY":
        entry = round(live_price, 2)
        sl = round(entry - sl_distance, 2)
        tp = round(entry + tp_distance, 2)
    else:
        entry = round(live_price, 2)
        sl = round(entry + sl_distance, 2)
        tp = round(entry - tp_distance, 2)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, rec_lot, rsi, f"BTC (Z:{z_score:.2f})", candle_time

def get_strategy_signal(ticker):
    if ticker == "GC=F":
        return get_xauusd_strategy_signal(ticker)
    elif ticker == "BTC-USD":
        return get_bitcoin_strategy_signal(ticker)
    return None, None, None, None, None, None, None, None

# --- TELEGRAM BUTTON HANDLER ---
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_to_channel":
        original_text = query.message.text
        channel_text = original_text
        if "• Lot:" in channel_text:
            lines = channel_text.split("\n")
            new_lines = []
            for line in lines:
                if line.startswith("• Lot:"):
                    new_lines.append("• Lot: Use your own calculated lot size!")
                else:
                    new_lines.append(line)
            channel_text = "\n".join(new_lines)

        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID, 
                text=channel_text, 
                parse_mode="Markdown"
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ **Published to Channel.**", parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Channel post error: {e}")
            await query.message.reply_text(f"❌ Broadcast Failed: {e}", parse_mode="Markdown")

# --- COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text(
            f"🔓 **Divine™️ // System Active**\nBalance: `${current_account_balance:.2f}`", 
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = False
    await update.message.reply_text("🌙 **Divine™️ // System Offline**", parse_mode="Markdown")

async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = True
    await update.message.reply_text("☀️ **Divine™️ // System Online**", parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trades, last_signals
    if update.effective_user.id not in authorized_users:
        return
    
    args = context.args
    if args and args[0].upper() in ["WIN", "LOSS"]:
        outcome = args[0].upper()
        pnl = float(args[1]) if len(args) > 1 else 0.0
        record_trade_outcome(outcome, profit_loss_amount=pnl)
        active_trades.clear()
        save_active_trades()
        last_signals.clear()
        await update.message.reply_text(f"🧹 **Record Updated:** `{outcome}` | Balance: `${current_account_balance:.2f}`", parse_mode="Markdown")
    else:
        active_trades.clear()
        save_active_trades()
        last_signals.clear()
        await update.message.reply_text("🧹 **Active Trades Cleared & Signals Reset.**", parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_account_balance
    if update.effective_user.id not in authorized_users:
        return
    args = context.args
    if args:
        try:
            current_account_balance = float(args[0])
            save_trade_history()
            await update.message.reply_text(f"💰 **Balance Saved:** `${current_account_balance:.2f}`", parse_mode="Markdown")
            return
        except ValueError:
            pass
    await update.message.reply_text(f"💰 **Current Balance:** `${current_account_balance:.2f}`", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    state = "Offline" if not bot_active else ("Limit Reached (2/2 Losses)" if daily_losses_count >= 2 else "Active")
    win_rate, wins, losses, pf = get_quantitative_performance_metrics()
    text = f"📊 **Divine™️ Status:** `{state}`\n• Win Rate: `{win_rate:.1f}%` ({wins}W/{losses}L)\n• Daily Losses: `{daily_losses_count}/2`\n• Balance: `${current_account_balance:.2f}`\n"

    if not active_trades:
        text += "No active position."
    else:
        for label, trade in active_trades.items():
            text += f"\n📌 **{label} ({trade['type']})**\n• Entry: `{trade['entry']}`\n• SL: `{trade['sl']}` | TP: `{trade['tp']}`"
    
    await update.message.reply_text(text, parse_mode="Markdown")

# --- LOOPS ---
async def hourly_heartbeat_loop(app):
    while True:
        try:
            await asyncio.sleep(3600) 
            if not bot_active:
                continue
            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            if target_user and authorized_users:
                win_rate, wins, losses, _ = get_quantitative_
