import asyncio
import datetime
import json
import logging
import os
import pandas as pd
import requests
import ta
import yfinance as yf
import MetaTrader5 as mt5
from flask import Flask
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

RISK_PER_TRADE_PCT = 0.015  # 1.5% strict risk profile per basket

# --- METATRADER 5 ACCOUNT AUTO-SYNC ---
def get_live_mt5_account_balance():
    """Fetches real-time account balance directly from MT5 to prevent manual input errors."""
    try:
        if not mt5.initialize():
            logging.error(f"MT5 Initialization failed: {mt5.last_error()}")
            return 10.0  # Safe fallback default
        
        acc_info = mt5.account_info()
        if acc_info is not None:
            return float(acc_info.balance)
    except Exception as e:
        logging.error(f"Error fetching live MT5 balance: {e}")
    return 10.0

# --- DAILY LOSS LIMIT & SIMONS STATISTICAL MATRIX ---
today_date = datetime.datetime.now(datetime.timezone.utc).date()
daily_losses_count = 0
daily_loss_limit_alert_sent = False

# --- TAILORED ASSET STRATEGY LIST ---
WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",  # Dedicated Gold Sniper (Mon-Fri)
    "BTC-USD": "BTCUSD" # Bitcoin active 24/7
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"  # Dedicated Bitcoin Momentum (Sat-Sun)
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()

# --- STATE PERSISTENCE FILE FOR CRASH RECOVERY ---
STATE_FILE = "active_trades_state.json"

def save_active_trades_state():
    """Persists active trades to disk so reboots on Render don't wipe memory."""
    try:
        data = {}
        for label, trade in active_trades.items():
            trade_copy = trade.copy()
            if isinstance(trade_copy.get("timestamp"), datetime.datetime):
                trade_copy["timestamp"] = trade_copy["timestamp"].isoformat()
            data[label] = trade_copy
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"Error saving state persistence file: {e}")

def load_active_trades_state():
    """Recovers active trades from disk after a system restart."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            for label, trade in data.items():
                if isinstance(trade.get("timestamp"), str):
                    trade["timestamp"] = datetime.datetime.fromisoformat(trade["timestamp"])
            return data
    except Exception as e:
        logging.error(f"Error loading state persistence file: {e}")
        return {}

active_trades = load_active_trades_state()

# --- QUANTITATIVE PERFORMANCE TRACKER (SIMONS FRAMEWORK) ---
trade_history = []  
MAX_HISTORY_LEN = 100  
strategy_alert_sent = False  

def record_trade_outcome(outcome):
    global trade_history, strategy_alert_sent, today_date, daily_losses_count, daily_loss_limit_alert_sent
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    if now_date != today_date:
        today_date = now_date
        daily_losses_count = 0
        daily_loss_limit_alert_sent = False

    trade_history.append(outcome)
    if len(trade_history) > MAX_HISTORY_LEN:
        trade_history.pop(0)  

    if outcome == "LOSS":
        daily_losses_count += 1

def get_quantitative_performance_metrics():
    """Calculates statistical win rate and sample size like a quantitative fund."""
    total_trades = len(trade_history)
    if total_trades == 0:
        return 0.0, 0, 0, 0.0
    
    wins = trade_history.count("WIN")
    losses = trade_history.count("LOSS")
    win_rate = (wins / total_trades) * 100.0
    profit_factor = (wins / losses) if losses > 0 else float(wins)
    return win_rate, wins, losses, profit_factor

# --- PROFESSIONAL SESSION TIME-OF-DAY FILTER ---
def is_active_trading_session(ticker):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    weekday = now_utc.weekday()
    hour = now_utc.hour

    if ticker == "BTC-USD":
        return True

    if ticker == "GC=F":
        if weekday >= 5:  
            return False
        if 7 <= hour < 20:
            return True
        return False

    return True

# --- ALTERNATIVE DATA & NEWS SENTIMENT CIRCUIT BREAKER ---
def is_high_impact_news_time():
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        response = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=5)
        if response.status_code == 200:
            events = response.json()
            for ev in events:
                if ev.get("impact") == "High":
                    ev_date_str = ev.get("date")
                    if ev_date_str:
                        ev_time = datetime.datetime.fromisoformat(ev_date_str.replace("Z", "+00:00"))
                        time_diff = (ev_time - now_utc).total_seconds() / 60.0
                        if -15 <= time_diff <= 30:
                            return True, f"High-Impact News Sentiment Event: {ev.get('title')} ({ev.get('country')})"
        
        if now_utc.weekday() == 4 and now_utc.hour >= 20:
            return True, "Weekend Market Close Volatility Window"
        if now_utc.weekday() == 6 and now_utc.hour < 1:
            return True, "Weekend Market Open Volatility Window"

    except Exception as e:
        logging.error(f"News check API error: {e}")

    return False, ""

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER & KEEP-ALIVE PING ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Divine Simon's High-Frequency Quantitative Engine is Live."

def self_ping_loop():
    """Pings its own Render URL every 5 minutes to prevent host sleep/inactivity stalls."""
    app_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not app_url:
        app_url = "https://signal-bot-t8ud.onrender.com"
    
    while True:
        try:
            requests.get(app_url, timeout=10)
        except Exception as e:
            logging.error(f"Self-ping keep-alive error: {e}")
        import time
        time.sleep(300)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- STRICT INSTITUTIONAL RISK POSITION SIZING ---
def calculate_dynamic_lot(ticker, sl_pips, current_atr):
    live_balance = get_live_mt5_account_balance()
    risk_amount = live_balance * RISK_PER_TRADE_PCT
    pip_value = 1.0

    if sl_pips <= 0:
        return 0.01

    # Split risk evenly across 3 tranches (legs) to protect the account
    tranche_risk_amount = risk_amount / 3.0
    volatility_scalar = 1.0 / max(current_atr, 1.0)
    calculated_lot = round((tranche_risk_amount / (sl_pips * pip_value)) * (1.0 + volatility_scalar * 0.1), 2)
    
    if live_balance <= 10.0:
        return 0.01  # Absolute micro-balance capital guard
    elif live_balance < 100.0:
        return max(0.01, min(calculated_lot, 0.05))
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
        
        bb = ta.volatility.BollingerBands(close_series, window=20, window_dev=2)
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_lower'] = bb.bollinger_lband()
        df['bb_middle'] = bb.bollinger_mavg()

        # --- Z-SCORE CALCULATION ---
        rolling_mean = close_series.rolling(window=20).mean()
        rolling_std = close_series.rolling(window=20).std()
        df['z_score'] = (close_series - rolling_mean) / rolling_std

        return df
    except Exception as e:
        logging.error(f"Data fetch error for {ticker}: {e}")
        return None

# --- MULTI-TIMEFRAME MATRIX CORRELATION ---
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

# --- DYNAMIC VOLATILITY REGIME (ATR REGIME CHECK) ---
def is_market_in_random_chop(df_m15):
    if df_m15 is None or len(df_m15) < 20:
        return False
    recent_atr = df_m15['atr14'].iloc[-1]
    avg_atr = df_m15['atr14'].rolling(window=20).mean().iloc[-1]
    if pd.notna(recent_atr) and pd.notna(avg_atr):
        if recent_atr < (avg_atr * 0.3):
            return True
    return False

# --- OPTIMISED GOLD (XAUUSD) STRATEGY (HIGH FREQUENCY Z-SCORE 0.8) ---
def get_gold_strategy_signal(ticker):
    h1_bias = get_h1_trend_bias(ticker)
    if h1_bias == "NEUTRAL":
        return None, None, None, None, None, None, None, None, None

    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="3d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None, None

    if is_market_in_random_chop(df_m15):
        return None, None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['z_score']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    open_p = float(c['Open'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])
    z_score = float(c['z_score'])

    is_bullish_candle = close_p > open_p
    is_bearish_candle = close_p < open_p

    sig = None
    if h1_bias == "BULLISH" and close_p >= ema50 and (28 <= rsi <= 68) and z_score <= -0.8 and is_bullish_candle:
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p <= ema50 and (32 <= rsi <= 72) and z_score >= 0.8 and is_bearish_candle:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    slippage_buffer = 0.05 * atr
    spread_buffer = 0.5 + slippage_buffer  
    min_broker_stop_distance = max(atr * 1.5, 12.0)  

    if sig == "BUY":
        entry = round(live_price + spread_buffer, 2)
        sl = round(entry - min_broker_stop_distance, 2)
        tp = round(entry + (min_broker_stop_distance * 1.6), 2)
        be_level = round(entry + (min_broker_stop_distance * 0.8), 2)
    else:
        entry = round(live_price - spread_buffer, 2)
        sl = round(entry + min_broker_stop_distance, 2)
        tp = round(entry - (min_broker_stop_distance * 1.6), 2)
        be_level = round(entry - (min_broker_stop_distance * 0.8), 2)

    rec_lot = calculate_dynamic_lot(ticker, min_broker_stop_distance, atr)
    return sig, entry, sl, tp, entry, be_level, rec_lot, rsi, f"DIVINE-SIMONS-GOLD-HF ({h1_bias})"

# --- OPTIMISED BITCOIN (BTCUSD) STRATEGY (HIGH FREQUENCY Z-SCORE 0.85) ---
def get_bitcoin_strategy_signal(ticker):
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="2d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None, None

    if is_market_in_random_chop(df_m15):
        return None, None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['bb_lower']) or pd.isna(c['bb_upper']) or pd.isna(c['z_score']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    open_p = float(c['Open'])
    rsi = float(c['rsi14'])
    bb_lower = float(c['bb_lower'])
    bb_upper = float(c['bb_upper'])
    bb_middle = float(c['bb_middle'])
    z_score = float(c['z_score'])

    is_bullish_candle = close_p > open_p
    is_bearish_candle = close_p < open_p

    sig = None
    if z_score <= -0.85 and rsi < 50 and is_bullish_candle:
        sig = "BUY"
    elif z_score >= 0.85 and rsi > 50 and is_bearish_candle:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    slippage_buffer = 0.05 * atr
    spread_buffer = 10.0 + slippage_buffer
    
    sl_distance = max(atr * 1.6, 40.0)  
    tp_distance = abs(live_price - bb_middle) * 0.85  
    if tp_distance < (sl_distance * 1.1):
        tp_distance = sl_distance * 1.5

    if sig == "BUY":
        entry = round(live_price + spread_buffer, 2)
        sl = round(entry - sl_distance, 2)
        tp = round(entry + tp_distance, 2)
        be_level = round(entry + (sl_distance * 0.8), 2)
    else:
        entry = round(live_price - spread_buffer, 2)
        sl = round(entry + sl_distance, 2)
        tp = round(entry - tp_distance, 2)
        be_level = round(entry - (sl_distance * 0.8), 2)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance, atr)
    return sig, entry, sl, tp, entry, be_level, rec_lot, rsi, "DIVINE-SIMONS-BTC-HF"

def get_strategy_signal(ticker):
    if ticker == "GC=F":
        return get_gold_strategy_signal(ticker)
    elif ticker == "BTC-USD":
        return get_bitcoin_strategy_signal(ticker)
    return None, None, None, None, None, None, None, None, None

# --- TELEGRAM COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        live_bal = get_live_mt5_account_balance()
        await update.message.reply_text(
            f"🔓 **Divine Simon's HF Engine Online.**\n💰 Live MT5 Balance Auto-Sync: **${live_bal:.2f}**\n📊 **High-Frequency Z-Score & Strict Risk Guard Active.**", 
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    live_bal = get_live_mt5_account_balance()
    await update.message.reply_text(f"💰 **Live MT5 Account Balance:** ${live_bal:.2f}\n*Risk managed automatically per basket.*", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    news_active, news_reason = is_high_impact_news_time()
    status_msg = "🟢 **Optimal (HF Quantitative Engine Active)**"
    if news_active:
        status_msg = f"⚠️ **Paused (News Sentiment Shield):** {news_reason}"
    elif daily_losses_count >= 3:
        status_msg = f"🛑 **Paused (Daily Loss Limit Reached: {daily_losses_count}/3)**"

    win_rate, wins, losses, profit_factor = get_quantitative_performance_metrics()
    total_samples = len(trade_history)
    live_bal = get_live_mt5_account_balance()

    stats_text = (
        f"📊 **Divine Simon HF Performance Matrix:**\n"
        f"• Total Samples Logged: `{total_samples}`\n"
        f"• Win Rate: `{win_rate:.2f}%` (Wins: {wins} | Losses: {losses})\n"
        f"• Profit Factor: `{profit_factor:.2f}`\n"
        f"• Live MT5 Balance Guard: `${live_bal:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    if not active_trades:
        await update.message.reply_text(stats_text + f"State: {status_msg}\nNo active multi-tranche baskets.", parse_mode="Markdown")
        return
    
    msg = stats_text + f"State: {status_msg}\n━━━━━━━━━━━━━━━━━━━\n"
    for label, trade in active_trades.items():
        y_ticker = "GC=F" if label == "XAUUSD" else "BTC-USD"
        df_temp = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="1d")
        current_price = float(df_temp['Close'].iloc[-1]) if df_temp is not None and not df_temp.empty else trade['entry']
        
        msg += (
            f"📌 **{label}** ({trade['type']}) - Baskets: {trade['tranches']}\n"
            f"• Base Entry: `{trade['entry']:.2f}` | Live: `{current_price:.2f}`\n"
            f"• Stop Loss: `{trade['sl']:.2f}` | Take Profit: `{trade['tp']:.2f}`\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- HOURLY HEARTBEAT LOOP ---
async def hourly_heartbeat_loop(app):
    while True:
        try:
            await asyncio.sleep(3600)
            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            if target_user and authorized_users:
                win_rate, wins, losses, _ = get_quantitative_performance_metrics()
                live_bal = get_live_mt5_account_balance()
                heartbeat_msg = f"🟢 **[HEARTBEAT - DIVINE SIMON HF]** Engine Active | MT5 Balance: `${live_bal:.2f}` | Win Rate: `{win_rate:.1f}%`"
                await app.bot.send_message(chat_id=target_user, text=heartbeat_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Heartbeat loop fatal exception caught & recovered: {e}")
            await asyncio.sleep(60)

# --- REAL-TIME GUIDANCE LOOP WITH STATE PERSISTENCE ---
async def live_chart_guidance_loop(app):
    global active_trades
    while True:
        try:
            await asyncio.sleep(10)
            if not active_trades:
                continue

            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            current_time = datetime.datetime.now(datetime.timezone.utc)

            state_changed = False
            for label, trade in list(active_trades.items()):
                try:
                    trade_age = (current_time - trade.get("timestamp", current_time)).total_seconds()
                    if trade_age > 14400: # 4 hours
                        active_trades.pop(label, None)
                        state_changed = True
                        if target_user and authorized_users:
                            await app.bot.send_message(chat_id=target_user, text=f"🔄 **[AUTO-RESET]** Stale basket cleared for {label}.", parse_mode="Markdown")
                        continue

                    y_ticker = "GC=F" if label == "XAUUSD" else "BTC-USD"
                    df_live = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="1d")
                    if df_live is None or len(df_live) < 10:
                        continue

                    current_price = float(df_live['Close'].iloc[-1])
                    trade_type = trade["type"]
                    tp = trade["tp"]
                    sl = trade["sl"]
                    be_level = trade["be_level"]
                    entry = trade["entry"]

                    if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                        msg = f"🎯 **[BASKET TARGET SECURED!]** - {label}\nAll tranches hit Take Profit at `{tp:.2f}`. Statistical Edge Realized! 🚀"
                        await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                        record_trade_outcome("WIN")
                        active_trades.pop(label, None)
                        state_changed = True
                        continue

                    if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                        msg = f"🛑 **[BASKET STOP LOSS HIT]** - {label}\nRisk boundary defended at `{sl:.2f}`. Micro-account protected."
                        await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                        record_trade_outcome("LOSS")
                        active_trades.pop(label, None)
                        state_changed = True
                        continue

                    if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                        trade["be_hit"] = True
                        state_changed = True
                        msg = f"🛡 **[PROTECT BASKET]** - {label}\nTrail Stop Loss to base entry (`{entry:.2f}`) to secure risk-free execution."
                        await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                except Exception as inner_e:
                    logging.error(f"Error processing individual trade {label} in guidance loop: {inner_e}")

            if state_changed:
                save_active_trades_state()

        except Exception as e:
            logging.error(f"Guidance loop fatal exception caught & recovered: {e}")
            await asyncio.sleep(15)

# --- HIGH-FREQUENCY SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals, active_trades, today_date, daily_losses_count, daily_loss_limit_alert_sent
    news_alert_sent = False

    while True:
        try:
            now_date = datetime.datetime.now(datetime.timezone.utc).date()
            if now_date != today_date:
                today_date = now_date
                daily_losses_count = 0
                daily_loss_limit_alert_sent = False
                last_signals.clear()

            if daily_losses_count >= 3:
                if not daily_loss_limit_alert_sent:
                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user,
                            text="🛑 **[DAILY LOSS LIMIT REACHED]**\n3 losses recorded today. Automated execution paused to protect capital.",
                            parse_mode="Markdown"
                        )
                    daily_loss_limit_alert_sent = True
                await asyncio.sleep(60)
                continue

            is_news, news_desc = is_high_impact_news_time()
            if is_news:
                if not news_alert_sent:
                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=f"🛡️ **[NEWS SENTIMENT SHIELD ENGAGED]**\nPaused due to: *{news_desc}*.", 
                            parse_mode="Markdown"
                        )
                    news_alert_sent = True
                await asyncio.sleep(60)
                continue
            else:
                news_alert_sent = False

            all_assets = {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}

            for ticker, label in all_assets.items():
                try:
                    if label in active_trades:
                        continue

                    if not is_active_trading_session(ticker):
                        continue

                    sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, strategy_name = get_strategy_signal(ticker)

                    if sig is None:
                        continue

                    if last_signals.get(ticker) != sig:
                        last_signals[ticker] = sig
                        dir_icon = "🟢" if sig == "BUY" else "🔴"
                        num_tranches = 3  
                        live_bal = get_live_mt5_account_balance()

                        signal_text = (
                            f"💎 **DIVINE SIMON HF SIGNAL**\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 **Asset:** `{label}` | **Model:** `{strategy_name}`\n"
                            f"📈 **Direction:** {dir_icon} **{sig}** ({num_tranches} Legs)\n\n"
                            f"🔹 **Base Entry:** `{entry:.2f}`\n"
                            f"🔴 **Stop Loss:** `{sl:.2f}`\n"
                            f"🎯 **Take Profit:** `{tp:.2f}`\n"
                            f"⚖️ **Lot Per Tranche (HF):** `{rec_lot}` *(MT5 Balance: ${live_bal:.2f})*\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"🛡️ *High-Frequency Edge Verified.*"
                        )

                        target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                        await app.bot.send_message(chat_id=target_user, text=signal_text, parse_mode="Markdown")

                        active_trades[label] = {
                            "type": sig,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "be_level": be_level,
                            "be_hit": False,
                            "tranches": num_tranches,
                            "timestamp": datetime.datetime.now(datetime.timezone.utc)
                        }
                        save_active_trades_state()

                except Exception as asset_e:
                    logging.error(f"Error scanning asset {label}: {asset_e}")

        except Exception as e:
            logging.error(f"Signal loop fatal exception caught & recovered: {e}")
            await asyncio.sleep(10)

        # Accelerated polling frequency (8 seconds) to catch fast market turns
        await asyncio.sleep(8)

async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(live_chart_guidance_loop(app))
    asyncio.create_task(hourly_heartbeat_loop(app))

# --- MAIN ENTRY ---
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("status", status_command))

    # Start Flask Web Server
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Start Keep-Alive Anti-Sleep Self-Ping Engine
    ping_thread = Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    logging.info("Divine Simon's High-Frequency Quantitative Engine Running...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
