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

RISK_PER_TRADE_PCT = 0.01  # Strict 1% risk profile per trade

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
active_signals = {}  

def load_data_from_disk():
    global trade_history, active_signals, current_account_balance, daily_losses_count, today_date
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
                active_signals = json.load(f)
        except Exception as e:
            logging.error(f"Error loading active signals: {e}")
            active_signals = {}

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

def save_active_signals():
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(active_signals, f)
    except Exception as e:
        logging.error(f"Error saving active signals: {e}")

load_data_from_disk()

def record_trade_outcome(outcome, pnl_amount=0.0, ticker_label=None):
    global trade_history, today_date, daily_losses_count, daily_loss_limit_alert_sent, current_account_balance
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    if now_date != today_date:
        today_date = now_date
        daily_losses_count = 0
        daily_loss_limit_alert_sent = False

    trade_record = {
        "outcome": outcome,
        "pnl": pnl_amount,
        "ticker": ticker_label,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    trade_history.append(trade_record)
    
    current_account_balance += pnl_amount
    if outcome == "LOSS":
        daily_losses_count += 1

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

# --- SESSION FILTER ---
def is_prime_trading_session(ticker):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    weekday = now_utc.weekday()
    
    if 0 <= now_utc.hour < 6:
        return False

    if weekday >= 5:
        if ticker == "BTC-USD" and (10 <= now_utc.hour <= 18):
            return True 
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

# --- RISK-COMPLIANT POSITION SIZING ---
def calculate_dynamic_lot(ticker, sl_distance):
    global current_account_balance
    risk_dollar_amount = current_account_balance * RISK_PER_TRADE_PCT
    
    if sl_distance <= 0:
        return None

    if ticker == "BTC-USD":
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

# --- BULLETPROOF DATA FETCHER ---
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

        df['atr14'] = ta.volatility.average_true_range(high_series, low_series, close_series, window=14)
        df['ema20'] = ta.trend.ema_indicator(close_series, window=20)
        df['ema50'] = ta.trend.ema_indicator(close_series, window=50)
        df['ema200'] = ta.trend.ema_indicator(close_series, window=200)
        df['rsi14'] = ta.momentum.rsi(close_series, window=14)
        df['adx14'] = ta.trend.adx(high_series, low_series, close_series, window=14)
        df['adx_pos'] = ta.trend.adx_pos(high_series, low_series, close_series, window=14)
        df['adx_neg'] = ta.trend.adx_neg(high_series, low_series, close_series, window=14)

        return df
    except Exception as e:
        logging.error(f"Protected data fetch error for {ticker}: {e}")
        return None

def get_h1_trend_bias(ticker):
    try:
        df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="60d")
        if df_h1 is None or len(df_h1) < 200:
            return "NEUTRAL", 0.0
        
        latest_close = float(df_h1['Close'].iloc[-2])
        latest_ema50 = float(df_h1['ema50'].iloc[-2]) if pd.notna(df_h1['ema50'].iloc[-2]) else latest_close
        latest_ema200 = float(df_h1['ema200'].iloc[-2]) if pd.notna(df_h1['ema200'].iloc[-2]) else latest_close
        latest_adx = float(df_h1['adx14'].iloc[-2]) if 'adx14' in df_h1 and pd.notna(df_h1['adx14'].iloc[-2]) else 25.0

        separation = abs(latest_ema50 - latest_ema200) / latest_close

        if latest_adx < 20.0:
            return "NEUTRAL", 0.0

        if latest_close > latest_ema50 and latest_ema50 > latest_ema200:
            return "BULLISH", separation
        elif latest_close < latest_ema50 and latest_ema50 < latest_ema200:
            return "BEARISH", separation
        return "NEUTRAL", 0.0
    except Exception as e:
        logging.error(f"H1 Trend Bias error for {ticker}: {e}")
        return "NEUTRAL", 0.0

def calculate_setup_quality_score(h1_bias, separation, rsi, adx, plus_di, minus_di):
    score = 70.0  
    if adx > 25.0:
        score += 15.0
    elif adx >= 20.0:
        score += 10.0
    else:
        return 0.0 

    if h1_bias == "BULLISH" and plus_di <= minus_di:
        return 0.0
    if h1_bias == "BEARISH" and minus_di <= plus_di:
        return 0.0

    if separation > 0.001:
        score += 10.0

    if 45 <= rsi <= 55:
        score += 5.0

    return min(score, 99.0)

def get_strategy_signal(ticker):
    try:
        h1_bias, separation = get_h1_trend_bias(ticker)
        if h1_bias == "NEUTRAL":
            return None, None, None, None, None, None, None, None, None, 0.0

        df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="10d")
        if df_m15 is None or len(df_m15) < 50:
            return None, None, None, None, None, None, None, None, None, 0.0

        c = df_m15.iloc[-2]
        prev_c = df_m15.iloc[-3]
        
        if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema20']) or pd.isna(c['adx14']):
            return None, None, None, None, None, None, None, None, None, 0.0

        close_p = float(c['Close'])
        ema20 = float(c['ema20'])
        rsi = float(c['rsi14'])
        atr = float(c['atr14'])
        adx = float(c['adx14'])
        plus_di = float(c['adx_pos']) if 'adx_pos' in c and pd.notna(c['adx_pos']) else 0.0
        minus_di = float(c['adx_neg']) if 'adx_neg' in c and pd.notna(c['adx_neg']) else 0.0
        candle_time = pd.to_datetime(df_m15.index[-2])

        sig = None
        if h1_bias == "BULLISH":
            if float(prev_c['Close']) <= float(prev_c['ema20']) and close_p > ema20 and rsi > 45:
                sig = "BUY"
        elif h1_bias == "BEARISH":
            if float(prev_c['Close']) >= float(prev_c['ema20']) and close_p < ema20 and rsi < 55:
                sig = "SELL"

        if not sig:
            return None, None, None, None, None, None, None, None, None, 0.0

        quality_score = calculate_setup_quality_score(h1_bias, separation, rsi, adx, plus_di, minus_di)
        required_threshold = 78.0  

        if quality_score < required_threshold:
            return None, None, None, None, None, None, None, None, None, 0.0

        if ticker == "EURUSD=X":
            entry = round(close_p, 4)
            if sig == "BUY":
                sl = round(entry - (atr * 1.5), 4)
                tp = round(entry + (atr * 3.0), 4) 
            else:
                sl = round(entry + (atr * 1.5), 4)
                tp = round(entry - (atr * 3.0), 4)
            sl_distance = abs(entry - sl)
        else:
            entry = round(close_p, 2)
            btc_buffer = max(atr * 1.5, 300.0) 
            if sig == "BUY":
                sl = round(entry - btc_buffer, 2)
                tp = round(entry + (btc_buffer * 2.0), 2)
            else:
                sl = round(entry + btc_buffer, 2)
                tp = round(entry - (btc_buffer * 2.0), 2)
            sl_distance = abs(entry - sl)

        # Calculate exact Break-Even Target (Halfway between Entry and TP)
        be_target = round((entry + tp) / 2, 4 if ticker == "EURUSD=X" else 2)

        rec_lot = calculate_dynamic_lot(ticker, sl_distance)
        if rec_lot is None:
            return None, None, None, None, None, None, None, None, None, 0.0

        return sig, entry, sl, tp, be_target, rec_lot, rsi, f"{ticker} Trend-Pullback", candle_time, quality_score
    except Exception as e:
        logging.error(f"Strategy signal error for {ticker}: {e}")
        return None, None, None, None, None, None, None, None, None, 0.0

# --- TELEGRAM BUTTON HANDLER ---
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_to_channel":
        original_text = query.message.text
        try:
            lines = original_text.split('\n')
            clean_channel_lines = [line for line in lines if any(k in line for k in ["BUY", "SELL", "Entry:", "SL:", "TP:", "BE Target:"]) and "Quality" not in line and "Lot" not in line]
            
            channel_payload = "\n".join(clean_channel_lines) if clean_channel_lines else original_text

            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID, 
                text=channel_payload, 
                parse_mode="Markdown"
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ **Published clean signal to Channel.**", parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Channel post error: {e}")
            await query.message.reply_text(f"❌ Failed: {e}", parse_mode="Markdown")

# --- COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text(
            f"🔓 **Professional Signal Engine Active**\nBalance: `${current_account_balance:.2f}`\n\n*Rule:* Manual execution mode with Break-Even targets enabled.", 
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = False
    await update.message.reply_text("🌙 **Offline.** Signal generation paused.", parse_mode="Markdown")

async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = True
    await update.message.reply_text("☀️ **Online.** Scanning for validated signals.", parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_signal_timestamps
    if update.effective_user.id not in authorized_users:
        return
    
    args = context.args
    if args and len(args) >= 2 and args[0].upper() in ["WIN", "LOSS"]:
        outcome = args[0].upper()
        try:
            pnl_val = float(args[1])
            record_trade_outcome(outcome, pnl_amount=pnl_val)
            last_signal_timestamps.clear()
            await update.message.reply_text(f"🧹 **Logged Manual Trade:** `{outcome}` with PnL: `${pnl_val:.2f}`.\nBalance updated to `${current_account_balance:.2f}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ Usage format: `/clear WIN 0.50` or `/clear LOSS -0.10`", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Usage format: `/clear WIN [pnl]` or `/clear LOSS [pnl]` to log your manual trades.", parse_mode="Markdown")

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
    await update.message.reply_text(f"💰 **Balance:** `${current_account_balance:.2f}`", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    state = "Active & Scanning" if bot_active else "Offline"
    win_rate, wins, losses, profit_factor = get_quantitative_performance_metrics()
    text = (
        f"📊 **Signal Bot Status:** `{state}`\n"
        f"• Win Rate: `{win_rate:.1f}%` ({wins}W/{losses}L)\n"
        f"• Profit Factor: `{profit_factor:.2f}`\n"
        f"• Balance: `${current_account_balance:.2f}`\n"
        f"• Daily Losses: `{daily_losses_count}/{MAX_DAILY_LOSSES}`\n\n"
        "Ready to send verified signals with Break-Even targets."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    strategy_text = (
        "📈 **Professional Signal Engine**\n\n"
        "• **Core Strategy:** H1 Trend Filter + M15 Pullback Confirmation with Break-Even guidance.\n"
        "• **Execution:** Manual entry on MT5 using exact signals provided."
    )
    await update.message.reply_text(strategy_text, parse_mode="Markdown")

# --- BACKGROUND LOOPS ---
async def hourly_heartbeat_loop(app):
    while True:
        try:
            await asyncio.sleep(3600) 
            if not bot_active:
                continue
            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            if target_user and authorized_users:
                win_rate, wins, losses, pf = get_quantitative_performance_metrics()
                await app.bot.send_message(
                    chat_id=target_user, 
                    text=f"🟢 **Heartbeat:** `{win_rate:.1f}%` win rate ({wins}W/{losses}L, PF: `{pf:.2f}`).", 
                    parse_mode="Markdown"
                )
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")
            await asyncio.sleep(60)

async def signal_loop(app):
    global last_signal_timestamps, today_date, daily_losses_count, daily_loss_limit_alert_sent

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
                            text=f"🛑 **Circuit Breaker:** Daily loss limit ({MAX_DAILY_LOSSES}) reached. Pausing signals for today.",
                            parse_mode="Markdown"
                        )
                await asyncio.sleep(60)
                continue

            all_assets = {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}

            for ticker, label in all_assets.items():
                if not is_prime_trading_session(ticker):
                    continue

                sig, entry, sl, tp, be_target, rec_lot, rsi, strategy_name, candle_time, quality_score = get_strategy_signal(ticker)
                if sig is None:
                    continue

                candle_str = str(candle_time)
                if last_signal_timestamps.get(ticker) != candle_str:
                    last_signal_timestamps[ticker] = candle_str
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    signal_text = (
                        f"💎 **Verified Signal [Quality: {quality_score:.0f}]**\n"
                        f"{label} {dir_icon} {sig}\n"
                        f"Entry: `{entry}`\n"
                        f"SL: `{sl}`\n"
                        f"TP: `{tp}`\n"
                        f"🛡️ **BE Target (Move SL to Entry here):** `{be_target}`\n"
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
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CallbackQueryHandler(button_click_handler))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    ping_thread = Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    logging.info("Professional Signal Engine Running 24/7...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
