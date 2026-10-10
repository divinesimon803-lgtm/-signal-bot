import asyncio
import datetime
import logging
import os
import pandas as pd
import requests
import ta
import yfinance as yf
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

RISK_PER_TRADE_PCT = 0.015  # 1.5% strict risk profile

# --- ACCOUNT BALANCE MANAGEMENT ---
current_account_balance = 10.00  # Default micro-account balance guard baseline

# --- BOT TOGGLE CONTROL ---
bot_active = True  # Controls signal generation

# --- DAILY LOSS LIMIT & DIVINE QUANT MATRIX ---
today_date = datetime.datetime.now(datetime.timezone.utc).date()
daily_losses_count = 0
daily_loss_limit_alert_sent = False

# --- ASSET UNIVERSE ---
WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",    # Gold Sniper (Mon-Fri)
    "BTC-USD": "BTCUSD" # Bitcoin active 24/7
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD" # Bitcoin active 24/7
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()
active_trades = {}  # Single active trade management per asset

# --- QUANTITATIVE PERFORMANCE TRACKER ---
trade_history = []  
MAX_HISTORY_LEN = 100  

def record_trade_outcome(outcome):
    global trade_history, today_date, daily_losses_count, daily_loss_limit_alert_sent
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

# --- AUTOMATIC NEWS & WEEKEND CIRCUIT BREAKER ---
def is_high_impact_news_time(ticker="GC=F"):
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
                            return True, f"High-Impact News Event: {ev.get('title')} ({ev.get('country')})"
        
        if ticker != "BTC-USD":
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
    return "Divine \"Jim Jnr\" Quantitative Sniper Engine is Live."

def self_ping_loop():
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

# --- DYNAMIC LOT SIZING ---
def calculate_dynamic_lot(ticker, sl_pips):
    global current_account_balance
    risk_amount = current_account_balance * RISK_PER_TRADE_PCT
    pip_value = 1.0

    if sl_pips <= 0:
        return 0.01

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    
    if current_account_balance <= 10.0:
        return 0.01  # Hard floor for micro-accounts
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

        bb = ta.volatility.BollingerBands(close_series, window=20, window_dev=2)
        df['bb_middle'] = bb.bollinger_mavg()

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

# --- GOLD STRATEGY ---
def get_gold_strategy_signal(ticker):
    h1_bias = get_h1_trend_bias(ticker)
    if h1_bias == "NEUTRAL":
        return None, None, None, None, None, None, None

    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="3d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['z_score']) or pd.isna(c['ema50']):
        return None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    z_score = float(c['z_score'])
    ema50 = float(c['ema50'])

    sig = None
    if h1_bias == "BULLISH" and z_score <= -1.5 and close_p >= ema50 and (30 <= rsi <= 60):
        sig = "BUY"
    elif h1_bias == "BEARISH" and z_score >= 1.5 and close_p <= ema50 and (40 <= rsi <= 70):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 0.5  
    sl_distance = max(atr * 1.5, 12.0)  

    if sig == "BUY":
        entry = round(live_price + spread_buffer, 2)
        sl = round(entry - sl_distance, 2)
        tp = round(entry + (sl_distance * 2.0), 2)
    else:
        entry = round(live_price - spread_buffer, 2)
        sl = round(entry + sl_distance, 2)
        tp = round(entry - (sl_distance * 2.0), 2)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, rec_lot, rsi, f"DIVINE-GOLD ({h1_bias}, Z:{z_score:.2f})"

# --- BITCOIN STRATEGY ---
def get_bitcoin_strategy_signal(ticker):
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="2d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['z_score']):
        return None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
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
    return sig, entry, sl, tp, rec_lot, rsi, f"DIVINE-BTC (Z:{z_score:.2f})"

def get_strategy_signal(ticker):
    if ticker == "GC=F":
        return get_gold_strategy_signal(ticker)
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
            f"🔓 **Divine \"Jim Jnr\" Sniper Online.**\n💰 Balance Guard: **${current_account_balance:.2f}**\n📊 **Matrix Active.**", 
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = False
    await update.message.reply_text("🌙 **[OFF]** Divine \"Jim Jnr\" Engine is asleep. Signals muted.", parse_mode="Markdown")

async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users:
        return
    bot_active = True
    await update.message.reply_text("☀️ **[ON]** Divine \"Jim Jnr\" Engine is active. Scanning signals!", parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_account_balance
    if update.effective_user.id not in authorized_users:
        return
    
    args = context.args
    if args:
        try:
            new_bal = float(args[0])
            current_account_balance = new_bal
            await update.message.reply_text(f"✅ Balance Updated: ${current_account_balance:.2f}.", parse_mode="Markdown")
            return
        except ValueError:
            pass
            
    await update.message.reply_text(f"💰 Balance: ${current_account_balance:.2f}\n*Update:* `/balance 10.00`", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    news_active, news_reason = is_high_impact_news_time("GC=F")
    if not bot_active:
        status_msg = "🌙 Paused (Off)"
    elif news_active:
        status_msg = f"⚠️ News Shield: {news_reason}"
    elif daily_losses_count >= 3:
        status_msg = f"🛑 Daily Limit Hit ({daily_losses_count}/3)"
    else:
        status_msg = "🟢 Optimal (Active)"

    win_rate, wins, losses, profit_factor = get_quantitative_performance_metrics()
    total_samples = len(trade_history)

    stats_text = (
        f"📊 **Divine \"Jim Jnr\" Performance:**\n"
        f"• Trades: `{total_samples}` | Win Rate: `{win_rate:.1f}%`\n"
        f"• Wins: {wins} | Losses: {losses} | PF: `{profit_factor:.2f}`\n"
        f"• Balance Guard: `${current_account_balance:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    if not active_trades:
        await update.message.reply_text(stats_text + f"State: {status_msg}\nNo active trade.", parse_mode="Markdown")
        return
    
    msg = stats_text + f"State: {status_msg}\n━━━━━━━━━━━━━━━━━━━\n"
    for label, trade in active_trades.items():
        y_ticker = "GC=F" if label == "XAUUSD" else "BTC-USD"
        df_temp = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="1d")
        current_price = float(df_temp['Close'].iloc[-1]) if df_temp is not None and not df_temp.empty else trade['entry']
        
        msg += (
            f"📌 **{label} ({trade['type']})**\n"
            f"• Entry: `{trade['entry']:.2f}` | Live: `{current_price:.2f}`\n"
            f"• SL: `{trade['sl']:.2f}` | TP: `{trade['tp']:.2f}`\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- HOURLY HEARTBEAT LOOP ---
async def hourly_heartbeat_loop(app):
    while True:
        try:
            await asyncio.sleep(3600)
            if not bot_active:
                continue
            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            if target_user and authorized_users:
                win_rate, _, _, _ = get_quantitative_performance_metrics()
                heartbeat_msg = f"🟢 [HEARTBEAT] Divine \"Jim Jnr\" Active | Win Rate: `{win_rate:.1f}%`"
                await app.bot.send_message(chat_id=target_user, text=heartbeat_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Heartbeat loop error: {e}")
            await asyncio.sleep(60)

# --- REAL-TIME GUIDANCE & SMART CHART MANAGEMENT LOOP ---
async def live_chart_guidance_loop(app):
    global active_trades
    while True:
        try:
            await asyncio.sleep(10)
            if not active_trades:
                continue

            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID

            for label, trade in list(active_trades.items()):
                y_ticker = "GC=F" if label == "XAUUSD" else "BTC-USD"
                df_live = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="2d")
                if df_live is None or len(df_live) < 20:
                    continue

                current_price = float(df_live['Close'].iloc[-1])
                trade_type = trade["type"]
                entry = trade["entry"]
                tp = trade["tp"]
                sl = trade["sl"]

                # --- TARGET CHECK (TP) ---
                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    msg = f"🎯 **[TP SECURED] - {label}**\nPrice hit target at `{tp:.2f}`. Edge Realized! 🚀"
                    if target_user and authorized_users:
                        await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    record_trade_outcome("WIN")
                    active_trades.pop(label, None)
                    continue

                # --- STOP LOSS CHECK (SL) ---
                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    msg = f"🛑 **[SL HIT] - {label}**\nRisk boundary defended at `{sl:.2f}`."
                    if target_user and authorized_users:
                        await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    record_trade_outcome("LOSS")
                    active_trades.pop(label, None)
                    continue

                # --- OPTION B: SMART CHART CHOP / INVALIDATION CHECK ---
                current_z = float(df_live['z_score'].iloc[-1]) if 'z_score' in df_live.columns and pd.notna(df_live['z_score'].iloc[-1]) else 0.0
                invalidation_triggered = False
                if trade_type == "BUY" and current_z > 1.0:
                    invalidation_triggered = True
                elif trade_type == "SELL" and current_z < -1.0:
                    invalidation_triggered = True

                if invalidation_triggered and not trade.get("invalidation_alerted", False):
                    trade["invalidation_alerted"] = True
                    inv_msg = (
                        f"⚠️ **[MARKET STALL] - {label}**\n"
                        f"Momentum reversed (`Z:{current_z:.2f}`).\n"
                        f"👉 **Close trade manually on MT5.**"
                    )
                    if target_user and authorized_users:
                        await app.bot.send_message(chat_id=target_user, text=inv_msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # --- BREAK / MID-TRADE GUIDANCE ---
                halfway_target = entry + ((tp - entry) / 2.0) if trade_type == "BUY" else entry - ((entry - tp) / 2.0)
                breakeven_alerted = trade.get("breakeven_alerted", False)

                if not breakeven_alerted:
                    if (trade_type == "BUY" and current_price >= halfway_target) or (trade_type == "SELL" and current_price <= halfway_target):
                        trade["breakeven_alerted"] = True
                        guidance_msg = (
                            f"💡 **[GUIDANCE] - {label}**\n"
                            f"Price reached halfway (`{current_price:.2f}`).\n"
                            f"👉 **Move SL to Breakeven (`{entry:.2f}`) & Take Partials!**"
                        )
                        if target_user and authorized_users:
                            await app.bot.send_message(chat_id=target_user, text=guidance_msg, parse_mode="Markdown")

        except Exception as e:
            logging.error(f"Guidance loop error: {e}")
            await asyncio.sleep(10)

# --- SIGNAL SCANNER LOOP ---
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

            if not bot_active:
                await asyncio.sleep(10)
                continue

            if daily_losses_count >= 3:
                if not daily_loss_limit_alert_sent:
                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user,
                            text="🛑 **[DAILY LIMIT]**\n3 losses hit. Execution paused.",
                            parse_mode="Markdown"
                        )
                    daily_loss_limit_alert_sent = True
                await asyncio.sleep(60)
                continue

            all_assets = {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}

            for ticker, label in all_assets.items():
                if label in active_trades:
                    continue  

                if not is_active_trading_session(ticker):
                    continue

                is_news, news_desc = is_high_impact_news_time(ticker)
                if is_news:
                    if not news_alert_sent:
                        target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                        if target_user and authorized_users:
                            await app.bot.send_message(
                                chat_id=target_user, 
                                text=f"🛡 **[NEWS SHIELD] - {label}**\nPaused: *{news_desc}*.", 
                                parse_mode="Markdown"
                            )
                    continue

                sig, entry, sl, tp, rec_lot, rsi, strategy_name = get_strategy_signal(ticker)

                if sig is None:
                    continue

                if last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    signal_text = (
                        f"💎 **DIVINE \"JIM JNR\" SIGNAL**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Asset:** {label}\n"
                        f"⚙️ **Model:** `{strategy_name}`\n"
                        f"📈 **Direction:** {dir_icon} {sig}\n\n"
                        f"🔹 **Entry:** `{entry:.2f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.2f}`\n"
                        f"🎯 **Take Profit:** `{tp:.2f}`\n"
                        f"⚖️ **Lot:** `{rec_lot}` *(${current_account_balance:.2f})*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🛡 *High-Conviction Edge Verified.*"
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
            await asyncio.sleep(15)

        await asyncio.sleep(20)

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
    app.add_handler(CommandHandler("off", off_command))
    app.add_handler(CommandHandler("on", on_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("status", status_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    ping_thread = Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    logging.info("Divine \"Jim Jnr\" Quantitative Sniper Engine Running...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
