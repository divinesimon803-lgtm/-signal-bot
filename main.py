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
from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# --- CONFIGURATION & SECURITY ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_PASSCODE = os.getenv("BOT_PASSCODE")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not BOT_PASSCODE:
    raise ValueError("CRITICAL SECURITY ERROR: Missing required environment variables.")

RISK_PER_TRADE_PCT = 0.015  # 1.5% strict risk profile

# --- ACCOUNT BALANCE MANAGEMENT ---
current_account_balance = 10.00  # Default micro-account baseline

# --- BOT TOGGLE CONTROL ---
bot_active = True  # Controls signal generation & keep-alive ping state

# --- DAILY LOSS LIMIT & DIVINE QUANT MATRIX ---
today_date = datetime.datetime.now(datetime.timezone.utc).date()
daily_losses_count = 0
daily_loss_limit_alert_sent = False

# --- ASSET UNIVERSE ---
WEEKDAY_ASSETS = {
    "EURUSD=X": "EURUSD",  # EUR/USD Major Pair (Mon-Fri)
    "BTC-USD": "BTCUSD"    # Bitcoin active 24/7 (UNTOUCHED)
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"   # Bitcoin active 24/7 (UNTOUCHED)
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()
active_trades = {}  # Single active trade management per asset

# --- PERSISTENT QUANTITATIVE PERFORMANCE TRACKER ---
HISTORY_FILE = "history.json"
trade_history = []  

def load_trade_history():
    global trade_history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        except Exception as e:
            logging.error(f"Error loading history: {e}")
            trade_history = []

def save_trade_history():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(trade_history, f)
    except Exception as e:
        logging.error(f"Error saving history: {e}")

load_trade_history()

def record_trade_outcome(outcome):
    global trade_history, today_date, daily_losses_count, daily_loss_limit_alert_sent
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    if now_date != today_date:
        today_date = now_date
        daily_losses_count = 0
        daily_loss_limit_alert_sent = False

    trade_history.append(outcome)
    save_trade_history()  # Always save to disk so history is never lost

    if outcome == "LOSS":
        daily_losses_count += 1

def get_quantitative_performance_metrics():
    total_trades = len(trade_history)
    if total_trades == 0:
        return 0.0, 0, 0, 0.0
    
    wins = trade_history.count("WIN")
    losses = trade_history.count("LOSS")
    win_rate = (wins / total_trades) * 100.0
    profit_factor = (wins / losses) if losses > 0 else float(wins)
    return win_rate, wins, losses, profit_factor

# --- PROFESSIONAL SESSION FILTER ---
def is_active_trading_session(ticker):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    weekday = now_utc.weekday()

    if ticker == "BTC-USD":
        return True

    if ticker == "EURUSD=X":
        if weekday >= 5:  # Weekend closed for forex
            return False
        return True

    return True

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER & PERMANENT KEEP-ALIVE ---
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return jsonify({"status": "ACTIVE" if bot_active else "PAUSED", "code": 200}), 200

def self_ping_loop():
    app_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not app_url:
        app_url = "https://signal-bot-t8ud.onrender.com/health"
    
    while True:
        try:
            requests.get(app_url, timeout=10)
        except Exception as e:
            logging.error(f"Self-ping error: {e}")
        import time
        time.sleep(180)  # Pings every 3 minutes to guarantee zero sleep

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- DYNAMIC LOT SIZING ---
def calculate_dynamic_lot(ticker, sl_pips):
    global current_account_balance
    risk_amount = current_account_balance * RISK_PER_TRADE_PCT
    pip_value = 1.0

    if sl_pips <= 0:
        return 0.01

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    
    if current_account_balance <= 10.0:
        return 0.01  
    else:
        return max(0.01, min(calculated_lot, 2.0))

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

# --- EURUSD STRATEGY ---
def get_eurusd_strategy_signal(ticker):
    h1_bias = get_h1_trend_bias(ticker)
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="3d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['z_score']):
        return None, None, None, None, None, None, None

    atr = float(c['atr14'])
    rsi = float(c['rsi14'])
    z_score = float(c['z_score'])

    sig = None
    if z_score <= -1.4 and rsi < 42 and h1_bias != "BEARISH":
        sig = "BUY"
    elif z_score >= 1.4 and rsi > 58 and h1_bias != "BULLISH":
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 0.0001  # 1 pip forex spread buffer
    sl_distance = max(atr * 1.4, 0.0012)  # Minimum 12 pips

    if sig == "BUY":
        entry = round(live_price + spread_buffer, 5)
        sl = round(entry - sl_distance, 5)
        tp = round(entry + (sl_distance * 2.0), 5)
    else:
        entry = round(live_price - spread_buffer, 5)
        sl = round(entry + sl_distance, 5)
        tp = round(entry - (sl_distance * 2.0), 5)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance * 10000)
    return sig, entry, sl, tp, rec_lot, rsi, f"EURUSD (Z:{z_score:.2f})"

# --- BITCOIN STRATEGY ---
def get_bitcoin_strategy_signal(ticker):
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="2d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['z_score']):
        return None, None, None, None, None, None, None

    atr = float(c['atr14'])
    rsi = float(c['rsi14'])
    z_score = float(c['z_score'])

    sig = None
    if z_score <= -1.6 and rsi < 40:
        sig = "BUY"
    elif z_score >= 1.6 and rsi > 60:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 10.0
    sl_distance = max(atr * 1.6, 40.0) 

    if sig == "BUY":
        entry = round(live_price + spread_buffer, 2)
        sl = round(entry - sl_distance, 2)
        tp = round(entry + (sl_distance * 2.0), 2)
    else:
        entry = round(live_price - spread_buffer, 2)
        sl = round(entry + sl_distance, 2)
        tp = round(entry - (sl_distance * 2.0), 2)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, rec_lot, rsi, f"BTC (Z:{z_score:.2f})"

def get_strategy_signal(ticker):
    if ticker == "EURUSD=X":
        return get_eurusd_strategy_signal(ticker)
    elif ticker == "BTC-USD":
        return get_bitcoin_strategy_signal(ticker)
    return None, None, None, None, None, None, None

# --- TELEGRAM COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text(
            f"🔓 **Bot Online**\nBalance: `${current_account_balance:.2f}`", 
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = False
    await update.message.reply_text("🌙 **Bot OFF**", parse_mode="Markdown")

async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = True
    await update.message.reply_text("☀️ **Bot ON**", parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trades
    if update.effective_user.id not in authorized_users:
        return
    
    args = context.args
    if args and args[0].upper() in ["WIN", "LOSS"]:
        outcome = args[0].upper()
        record_trade_outcome(outcome)
        active_trades.clear()
        await update.message.reply_text(f"🧹 Recorded: **{outcome}**", parse_mode="Markdown")
    else:
        active_trades.clear()
        await update.message.reply_text("🧹 Trades Cleared.", parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_account_balance
    if update.effective_user.id not in authorized_users:
        return
    args = context.args
    if args:
        try:
            current_account_balance = float(args[0])
            await update.message.reply_text(f"✅ Balance: `${current_account_balance:.2f}`", parse_mode="Markdown")
            return
        except ValueError:
            pass
    await update.message.reply_text(f"💰 Balance: `${current_account_balance:.2f}`", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    if not bot_active:
        state = "Off"
    elif daily_losses_count >= 3:
        state = "Limit (3/3)"
    else:
        state = "Active"

    win_rate, wins, losses, pf = get_quantitative_performance_metrics()
    text = f"📊 **Status:** `{state}`\n• Win Rate: `{win_rate:.1f}%` ({wins}W/{losses}L)\n• Balance: `${current_account_balance:.2f}`\n"

    if not active_trades:
        text += "No active trade."
    else:
        for label, trade in active_trades.items():
            text += f"\n📌 **{label} ({trade['type']})**\n• Entry: `{trade['entry']}`\n• SL: `{trade['sl']}` | TP: `{trade['tp']}`"
    
    await update.message.reply_text(text, parse_mode="Markdown")

# --- 2-HOUR HEARTBEAT LOOP ---
async def hourly_heartbeat_loop(app):
    while True:
        try:
            await asyncio.sleep(7200) # 2 Hours
            if not bot_active:
                continue
            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            if target_user and authorized_users:
                win_rate, wins, losses, _ = get_quantitative_performance_metrics()
                await app.bot.send_message(
                    chat_id=target_user, 
                    text=f"🟢 **Heartbeat** | `{win_rate:.1f}%` ({wins}W/{losses}L)", 
                    parse_mode="Markdown"
                )
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")
            await asyncio.sleep(60)

# --- INSTANT REAL-TIME GUIDANCE & MANAGEMENT LOOP ---
async def live_chart_guidance_loop(app):
    global active_trades
    while True:
        try:
            await asyncio.sleep(3)  # Fast 3-second check for instant alerts
            if not active_trades:
                continue

            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID

            for label, trade in list(active_trades.items()):
                y_ticker = "EURUSD=X" if label == "EURUSD" else "BTC-USD"
                df_live = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="2d")
                if df_live is None or len(df_live) < 10:
                    continue

                current_price = float(df_live['Close'].iloc[-1])
                trade_type = trade["type"]
                tp = trade["tp"]
                sl = trade["sl"]
                entry = trade["entry"]

                # INSTANT TP HIT ALERT
                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    record_trade_outcome("WIN")
                    win_rate, wins, losses, _ = get_quantitative_performance_metrics()
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=f"🎯 **TP HIT: {label}** | Win Record: `{wins}W/{losses}L` (`{win_rate:.1f}%`)", 
                            parse_mode="Markdown"
                        )
                    active_trades.pop(label, None)
                    continue

                # INSTANT SL HIT ALERT
                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    record_trade_outcome("LOSS")
                    win_rate, wins, losses, _ = get_quantitative_performance_metrics()
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=f"🛑 **SL HIT: {label}** | Win Record: `{wins}W/{losses}L` (`{win_rate:.1f}%`)", 
                            parse_mode="Markdown"
                        )
                    active_trades.pop(label, None)
                    continue

                # BREAK-EVEN / RISK-FREE NOTIFICATION CHECK
                if not trade.get("breakeven_alerted", False):
                    if trade_type == "BUY":
                        halfway_target = entry + (tp - entry) * 0.5
                        if current_price >= halfway_target:
                            trade["breakeven_alerted"] = True
                            if target_user and authorized_users:
                                await app.bot.send_message(
                                    chat_id=target_user,
                                    text=f"🔒 **Break-Even Alert: {label}** | Move your SL to entry price (`{entry}`) to make this trade 100% risk-free!",
                                    parse_mode="Markdown"
                                )
                    elif trade_type == "SELL":
                        halfway_target = entry - (entry - tp) * 0.5
                        if current_price <= halfway_target:
                            trade["breakeven_alerted"] = True
                            if target_user and authorized_users:
                                await app.bot.send_message(
                                    chat_id=target_user,
                                    text=f"🔒 **Break-Even Alert: {label}** | Move your SL to entry price (`{entry}`) to make this trade 100% risk-free!",
                                    parse_mode="Markdown"
                                )

                # INVALIDATION STALL ALERT
                current_z = float(df_live['z_score'].iloc[-1]) if 'z_score' in df_live.columns and pd.notna(df_live['z_score'].iloc[-1]) else 0.0
                if (trade_type == "BUY" and current_z > 1.0) or (trade_type == "SELL" and current_z < -1.0):
                    if target_user and authorized_users and not trade.get("invalidation_alerted", False):
                        trade["invalidation_alerted"] = True
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=f"⚠️ **Stall: {label}** | Close manually.", 
                            parse_mode="Markdown"
                        )
                    active_trades.pop(label, None)
                    continue

        except Exception as e:
            logging.error(f"Guidance error: {e}")
            await asyncio.sleep(5)

# --- SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals, active_trades, today_date, daily_losses_count, daily_loss_limit_alert_sent

    while True:
        try:
            now_date = datetime.datetime.now(datetime.timezone.utc).date()
            if now_date != today_date:
                today_date = now_date
                daily_losses_count = 0
                daily_loss_limit_alert_sent = False
                last_signals.clear()

            if not bot_active:
                await asyncio.sleep(10)
                continue

            if daily_losses_count >= 3:
                await asyncio.sleep(60)
                continue

            all_assets = {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}

            for ticker, label in all_assets.items():
                if label in active_trades:
                    continue  

                if not is_active_trading_session(ticker):
                    continue

                sig, entry, sl, tp, rec_lot, rsi, strategy_name = get_strategy_signal(ticker)
                if sig is None:
                    continue

                if last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    signal_text = (
                        f"💎 **SIGNAL: {label}** {dir_icon} `{sig}`\n"
                        f"• Entry: `{entry}`\n"
                        f"• SL: `{sl}` | TP: `{tp}`\n"
                        f"• Lot: `{rec_lot}`"
                    )

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(chat_id=target_user, text=signal_text, parse_mode="Markdown")

                    active_trades[label] = {
                        "type": sig,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "timestamp": datetime.datetime.now(datetime.timezone.utc),
                        "breakeven_alerted": False,
                        "invalidation_alerted": False
                    }
        except Exception as e:
            logging.error(f"Signal loop error: {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(10)

async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(live_chart_guidance_loop(app))
    asyncio.create_task(hourly_heartbeat_loop(app))

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("off", off_command))
    app.add_handler(CommandHandler("on", on_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("status", status_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    ping_thread = Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    logging.info("Divine Quantitative Sniper Engine Running...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
