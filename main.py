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

RISK_PER_TRADE_PCT = 0.01  # Strict 1% risk profile

# --- ACCOUNT BALANCE & STATE MANAGEMENT ---
current_account_balance = 10.00  
bot_active = True  

today_date = datetime.datetime.now(datetime.timezone.utc).date()
daily_losses_count = 0
daily_loss_limit_alert_sent = False
MAX_DAILY_LOSSES = 3  

# --- ASSET UNIVERSE ---
WEEKDAY_ASSETS = {
    "EURUSD=X": "EURUSD",    
    "BTC-USD": "BTCUSD"  
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"  
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signal_timestamps = {}
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

def record_trade_outcome(outcome, ticker_label=None):
    global trade_history, today_date, daily_losses_count, daily_loss_limit_alert_sent
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    if now_date != today_date:
        today_date = now_date
        daily_losses_count = 0
        daily_loss_limit_alert_sent = False

    trade_history.append(outcome)
    if outcome == "LOSS":
        daily_losses_count += 1

    save_trade_history()

def get_quantitative_performance_metrics():
    total_trades = len(trade_history)
    if total_trades == 0:
        return 0.0, 0, 0, 0.0
    
    wins = trade_history.count("WIN")
    losses = trade_history.count("LOSS")
    win_rate = (wins / total_trades) * 100.0
    profit_factor = (wins / losses) if losses > 0 else float(wins)
    return win_rate, wins, losses, profit_factor

# --- INSTITUTIONAL SESSION FILTER ---
def is_prime_trading_session(ticker):
    """Only trade during active London & New York session hours (07:00 to 21:00 UTC)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    weekday = now_utc.weekday()
    
    if ticker == "EURUSD=X" and weekday >= 5:
        return False
        
    if 7 <= now_utc.hour < 21:
        return True
        
    return False

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

# --- MATHEMATICALLY TRUE RISK-BASED LOT CALCULATION ---
def calculate_dynamic_lot(ticker, sl_distance):
    global current_account_balance
    risk_dollar_amount = current_account_balance * RISK_PER_TRADE_PCT
    
    if sl_distance <= 0:
        return 0.01

    if ticker == "BTC-USD":
        pip_value = 1.0
        risk_per_lot = sl_distance * pip_value
    else:
        pip_value = 0.1 
        sl_pips = sl_distance * 10000
        risk_per_lot = sl_pips * pip_value

    if risk_per_lot <= 0:
        return 0.01

    calculated_lot = risk_dollar_amount / risk_per_lot
    final_lot = round(calculated_lot, 2)
    return max(0.01, min(final_lot, 0.05))

# --- DATA FETCHER & INDICATORS (TREND-PULLBACK SUITE) ---
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
        df['ema20'] = ta.trend.ema_indicator(close_series, window=20)
        df['ema50'] = ta.trend.ema_indicator(close_series, window=50)
        df['ema200'] = ta.trend.ema_indicator(close_series, window=200)
        df['rsi14'] = ta.momentum.rsi(close_series, window=14)

        return df
    except Exception as e:
        logging.error(f"Data fetch error for {ticker}: {e}")
        return None

def get_h1_trend_bias(ticker):
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="30d")
    if df_h1 is None or len(df_h1) < 200:
        return "NEUTRAL"
    
    latest_close = float(df_h1['Close'].iloc[-2])
    latest_ema50 = float(df_h1['ema50'].iloc[-2]) if pd.notna(df_h1['ema50'].iloc[-2]) else latest_close
    latest_ema200 = float(df_h1['ema200'].iloc[-2]) if pd.notna(df_h1['ema200'].iloc[-2]) else latest_close

    if latest_close > latest_ema50 and latest_ema50 > latest_ema200:
        return "BULLISH"
    elif latest_close < latest_ema50 and latest_ema50 < latest_ema200:
        return "BEARISH"
    return "NEUTRAL"

# --- NEW TREND-PULLBACK STRATEGY LOGIC ---
def get_strategy_signal(ticker):
    h1_bias = get_h1_trend_bias(ticker)
    if h1_bias == "NEUTRAL":
        return None, None, None, None, None, None, None, None

    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None

    # Use completed candle [-2] to avoid repainting
    c = df_m15.iloc[-2]
    prev_c = df_m15.iloc[-3]
    
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema20']) or pd.isna(c['ema50']):
        return None, None, None, None, None, None, None, None

    close_p = float(c['Close'])
    ema20 = float(c['ema20'])
    rsi = float(c['rsi14'])
    atr = float(c['atr14'])
    candle_time = pd.to_datetime(df_m15.index[-2])

    sig = None
    if h1_bias == "BULLISH":
        if float(prev_c['Close']) <= float(prev_c['ema20']) and close_p > ema20 and rsi > 40:
            sig = "BUY"
    elif h1_bias == "BEARISH":
        if float(prev_c['Close']) >= float(prev_c['ema20']) and close_p < ema20 and rsi < 60:
            sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None

    if ticker == "EURUSD=X":
        entry = round(close_p, 4)
        if sig == "BUY":
            sl = round(entry - (atr * 1.5), 4)
            tp = round(entry + (atr * 2.5), 4)
        else:
            sl = round(entry + (atr * 1.5), 4)
            tp = round(entry - (atr * 2.5), 4)
        sl_distance = abs(entry - sl)
    else: # BTC
        entry = round(close_p, 2)
        if sig == "BUY":
            sl = round(entry - (atr * 1.5), 2)
            tp = round(entry + (atr * 2.5), 2)
        else:
            sl = round(entry + (atr * 1.5), 2)
            tp = round(entry - (atr * 2.5), 2)
        sl_distance = abs(entry - sl)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, rec_lot, rsi, f"{ticker} Trend-Pullback", candle_time

# --- TELEGRAM BUTTON HANDLER ---
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
            f"🔓 **Divine™️ // Trend-Pullback Engine Active**\nBalance: `${current_account_balance:.2f}`", 
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
    global active_trades, last_signal_timestamps
    if update.effective_user.id not in authorized_users:
        return
    
    args = context.args
    if args and args[0].upper() in ["WIN", "LOSS"]:
        outcome = args[0].upper()
        record_trade_outcome(outcome)
        active_trades.clear()
        save_active_trades()
        last_signal_timestamps.clear()
        await update.message.reply_text(f"🧹 **Record Updated Manually:** `{outcome}`", parse_mode="Markdown")
    else:
        active_trades.clear()
        save_active_trades()
        last_signal_timestamps.clear()
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
            await update.message.reply_text(f"💰 **Balance Updated:** `${current_account_balance:.2f}`", parse_mode="Markdown")
            return
        except ValueError:
            pass
    await update.message.reply_text(f"💰 **Current Balance:** `${current_account_balance:.2f}`", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    state = "Offline" if not bot_active else (f"Limit Reached ({daily_losses_count}/{MAX_DAILY_LOSSES} Losses)" if daily_losses_count >= MAX_DAILY_LOSSES else "Active")
    win_rate, wins, losses, pf = get_quantitative_performance_metrics()
    text = f"📊 **Divine™️ Status:** `{state}`\n• Win Rate: `{win_rate:.1f}%` ({wins}W/{losses}L)\n• Daily Losses: `{daily_losses_count}/{MAX_DAILY_LOSSES}`\n• Balance Set: `${current_account_balance:.2f}`\n"

    if not active_trades:
        text += "No active positions tracked."
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
                win_rate, wins, losses, _ = get_quantitative_performance_metrics()
                await app.bot.send_message(
                    chat_id=target_user, 
                    text=f"🟢 **Divine™️ Heartbeat** | {win_rate:.1f}% ({wins}W/{losses}L) | Balance: `${current_account_balance:.2f}`", 
                    parse_mode="Markdown"
                )
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")
            await asyncio.sleep(60)

# --- ENHANCED LIVE GUIDANCE WITH BREAKEVEN & REVERSAL ALERTS ---
async def live_chart_guidance_loop(app):
    global active_trades
    while True:
        try:
            await asyncio.sleep(15)  
            if not active_trades:
                continue

            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID

            for label, trade in list(active_trades.items()):
                y_ticker = "EURUSD=X" if label == "EURUSD" else "BTC-USD"
                df_live = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="2d")
                if df_live is None or len(df_live) < 5:
                    continue

                current_price = float(df_live['Close'].iloc[-1])
                current_ema20 = float(df_live['ema20'].iloc[-1])
                trade_type = trade["type"]
                tp = trade["tp"]
                sl = trade["sl"]
                entry = trade["entry"]

                # 1. Check Take Profit Hit
                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    record_trade_outcome("WIN", ticker_label=label)
                    win_rate, wins, losses, _ = get_quantitative_performance_metrics()
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=f"🎯 **Divine™️ // TP Hit: {label}**\n• Record: `{wins}W/{losses}L` (`{win_rate:.1f}%`)\n• Update your MT5 record manually.", 
                            parse_mode="Markdown"
                        )
                    active_trades.pop(label, None)
                    save_active_trades()
                    continue

                # 2. Check Stop Loss Hit
                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    record_trade_outcome("LOSS", ticker_label=label)
                    win_rate, wins, losses, _ = get_quantitative_performance_metrics()
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=f"🛑 **Divine™️ // SL Hit: {label}**\n• Record: `{wins}W/{losses}L` (`{win_rate:.1f}%`)\n• Update your MT5 record manually.", 
                            parse_mode="Markdown"
                        )
                    active_trades.pop(label, None)
                    save_active_trades()
                    continue

                # 3. RULE-BASED BREAKEVEN / MILESTONE ALERT (+50% distance to TP)
                if not trade.get("be_notified", False):
                    if trade_type == "BUY":
                        halfway_target = entry + ((tp - entry) * 0.5)
                        if current_price >= halfway_target:
                            trade["be_notified"] = True
                            save_active_trades()
                            if target_user and authorized_users:
                                await app.bot.send_message(
                                    chat_id=target_user,
                                    text=f"🛡️ **Divine™️ // Lock Profit Alert: {label} (BUY)**\n• Price has reached halfway to TP (`{current_price}`).\n• *Rule Suggestion:* Consider moving your MT5 Stop-Loss to Entry (`{entry}`) to lock in a risk-free trade!",
                                    parse_mode="Markdown"
                                )
                    elif trade_type == "SELL":
                        halfway_target = entry - ((entry - tp) * 0.5)
                        if current_price <= halfway_target:
                            trade["be_notified"] = True
                            save_active_trades()
                            if target_user and authorized_users:
                                await app.bot.send_message(
                                    chat_id=target_user,
                                    text=f"🛡️ **Divine™️ // Lock Profit Alert: {label} (SELL)**\n• Price has reached halfway to TP (`{current_price}`).\n• *Rule Suggestion:* Consider moving your MT5 Stop-Loss to Entry (`{entry}`) to lock in a risk-free trade!",
                                    parse_mode="Markdown"
                                )

                # 4. RULE-BASED REVERSAL WARNING (Price crosses back over EMA20 against trade direction)
                if not trade.get("reversal_warned", False):
                    if trade_type == "BUY" and current_price < current_ema20:
                        trade["reversal_warned"] = True
                        save_active_trades()
                        if target_user and authorized_users:
                            await app.bot.send_message(
                                chat_id=target_user,
                                text=f"⚠️ **Divine™️ // Reversal Warning: {label} (BUY)**\n• Price (`{current_price}`) has dropped back below the EMA20 (`{current_ema20:.2f}`).\n• *Rule Suggestion:* Trend momentum may be fading. Consider closing manually if you want to secure current profit.",
                                parse_mode="Markdown"
                            )
                    elif trade_type == "SELL" and current_price > current_ema20:
                        trade["reversal_warned"] = True
                        save_active_trades()
                        if target_user and authorized_users:
                            await app.bot.send_message(
                                chat_id=target_user,
                                text=f"⚠️ **Divine™️ // Reversal Warning: {label} (SELL)**\n• Price (`{current_price}`) has risen back above the EMA20 (`{current_ema20:.2f}`).\n• *Rule Suggestion:* Trend momentum may be fading. Consider closing manually if you want to secure current profit.",
                                parse_mode="Markdown"
                            )

        except Exception as e:
            logging.error(f"Guidance error: {e}")
            await asyncio.sleep(10)

async def signal_loop(app):
    global last_signal_timestamps, active_trades, today_date, daily_losses_count, daily_loss_limit_alert_sent

    while True:
        try:
            now_date = datetime.datetime.now(datetime.timezone.utc).date()
            if now_date != today_date:
                today_date = now_date
                daily_losses_count = 0
                daily_loss_limit_alert_sent = False
                last_signal_timestamps.clear()
                save_trade_history()

            if not bot_active:
                await asyncio.sleep(10)
                continue

            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            
            if daily_losses_count >= MAX_DAILY_LOSSES:
                if not daily_loss_limit_alert_sent:
                    daily_loss_limit_alert_sent = True
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user,
                            text=f"🛑 **Divine™️ // Circuit Breaker:** Daily loss limit reached ({MAX_DAILY_LOSSES}/{MAX_DAILY_LOSSES}). Pausing execution.",
                            parse_mode="Markdown"
                        )
                await asyncio.sleep(60)
                continue

            all_assets = {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}

            for ticker, label in all_assets.items():
                if label in active_trades:
                    continue  

                if not is_prime_trading_session(ticker):
                    continue

                sig, entry, sl, tp, rec_lot, rsi, strategy_name, candle_time = get_strategy_signal(ticker)
                if sig is None:
                    continue

                candle_str = str(candle_time)
                if last_signal_timestamps.get(ticker) != candle_str:
                    last_signal_timestamps[ticker] = candle_str
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    now_utc = pd.Timestamp.now(tz="UTC")
                    is_expired = False
                    if pd.notna(candle_time):
                        if candle_time.tzinfo is None:
                            candle_time = candle_time.tz_localize("UTC")
                        else:
                            candle_time = candle_time.tz_convert("UTC")
                        
                        if (now_utc - candle_time).total_seconds() > 1800:
                            is_expired = True

                    if is_expired:
                        signal_text = f"⚠️ **Divine™️ // {label} {dir_icon} {sig} [EXPIRED]**"
                    else:
                        signal_text = (
                            f"💎 **Divine™️ // {label} {dir_icon} {sig}**\n"
                            f"• Entry: `{entry}`\n"
                            f"• SL: `{sl}`\n"
                            f"• TP: `{tp}`\n"
                            f"• Risk-Calculated Lot: `{rec_lot}`"
                        )

                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Post to Channel", callback_data="post_to_channel")]])

                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=signal_text, 
                            parse_mode="Markdown",
                            reply_markup=keyboard
                        )

                    if not is_expired:
                        active_trades[label] = {
                            "type": sig,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "be_notified": False,
                            "reversal_warned": False,
                            "timestamp": str(datetime.datetime.now(datetime.timezone.utc))
                        }
                        save_active_trades()
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
    app.add_handler(CallbackQueryHandler(button_click_handler))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    ping_thread = Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    logging.info("Divine™️ Trend-Pullback Engine Running 24/7 with Smart Alerts...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
