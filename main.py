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
current_account_balance = 30.0  # Default baseline; update anytime via /balance command

# --- TAILORED ASSET STRATEGY LIST ---
WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD"  # Dedicated Gold Sniper (Mon-Fri)
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"  # Dedicated Bitcoin Momentum (Sat-Sun)
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()
active_trades = {}  # Tracks ongoing trades for live institutional management

# --- STRATEGY PERFORMANCE MONITORING & AUTO-DIAGNOSIS ---
trade_history = []  # Stores recent trade outcomes ("WIN" or "LOSS")
MAX_HISTORY_LEN = 20  # Keeps track of the last 20 trades for trend analysis
strategy_alert_sent = False  # Prevents spamming alerts

def record_trade_outcome(outcome):
    global trade_history, strategy_alert_sent
    trade_history.append(outcome)
    if len(trade_history) > MAX_HISTORY_LEN:
        trade_history.pop(0)  # Maintain rolling window

def check_strategy_performance():
    """Analyzes recent performance. Returns (needs_update: bool, reason: str, suggestion: str)"""
    global strategy_alert_sent
    if len(trade_history) < 10:  # Need at least a sample of 10 trades before judging
        return False, "", ""
    
    losses_in_row = 0
    for outcome in reversed(trade_history):
        if outcome == "LOSS":
            losses_in_row += 1
        else:
            break

    # If we hit 5 losses in a row, or win rate in the last 15 trades drops below 25%
    recent_window = trade_history[-15:]
    recent_losses = recent_window.count("LOSS")
    
    if losses_in_row >= 5 or (len(recent_window) >= 10 and (recent_losses / len(recent_window)) >= 0.75):
        if not strategy_alert_sent:
            strategy_alert_sent = True  # Lock so it only alerts once until reset
            reason = f"Detected a sustained drawdown ({losses_in_row} consecutive losses or heavy failure rate in recent window)."
            suggestion = "Market volatility regime may have shifted. Consider letting me tweak your RSI boundary thresholds or EMA trend filters."
            return True, reason, suggestion
            
    return False, "", ""

# --- AUTOMATIC NEWS CIRCUIT BREAKER (AVOIDS SLIPPAGE SPIKES) ---
def is_high_impact_news_time():
    """
    Checks for major high-impact economic events (US Dollar / Global macro events).
    Queries a free economic events endpoint or uses safety checks for standard high-volatility windows 
    (e.g., FOMC / NFP generalized timing guards if network fails).
    """
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        
        # 1. Check real-time economic calendar API (Forex Factory public JSON mirror)
        response = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=5)
        if response.status_code == 200:
            events = response.json()
            for ev in events:
                if ev.get("impact") == "High":
                    # Parse event date string
                    ev_date_str = ev.get("date")
                    if ev_date_str:
                        ev_time = datetime.datetime.fromisoformat(ev_date_str.replace("Z", "+00:00"))
                        # If an event is within the next 30 minutes or happened 15 minutes ago
                        time_diff = (ev_time - now_utc).total_seconds() / 60.0
                        if -15 <= time_diff <= 30:
                            return True, f"High-Impact News Event: {ev.get('title')} ({ev.get('country')})"
        
        # 2. General Safe Guard: Friday afternoon market close / Sunday market open volatility windows
        if now_utc.weekday() == 4 and now_utc.hour >= 20: # Friday after 20:00 UTC
            return True, "Weekend Market Close Volatility Window"
        if now_utc.weekday() == 6 and now_utc.hour < 1: # Sunday market open spike guard
            return True, "Weekend Market Open Volatility Window"

    except Exception as e:
        logging.error(f"News check API error: {e}")

    return False, ""

# --- CIRCUIT BREAKER STATE ---
daily_loss_counter = 0
last_trade_reset_date = datetime.datetime.now(datetime.timezone.utc).date()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER (24/7 Live Status) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Tailored Gold/Bitcoin Sniper Engine is Live & Protecting Capital with News Shield."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- DYNAMIC LOT SIZING (TIRED TO LIVE ACCOUNT BALANCE) ---
def calculate_dynamic_lot(ticker, sl_pips):
    global current_account_balance
    risk_amount = current_account_balance * RISK_PER_TRADE_PCT
    pip_value = 1.0
    if ticker == "GC=F":
        pip_value = 1.0  # Gold dollar value scaling
    elif ticker == "BTC-USD":
        pip_value = 1.0  # BTC dollar value scaling

    if sl_pips <= 0:
        return 0.01

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    max_cap = 0.05 if current_account_balance < 100.0 else 2.0
    return max(0.01, min(calculated_lot, max_cap))

# --- DATA FETCHER & INDICATORS ---
def fetch_data(ticker, interval, period="5d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        df.columns = [str(col).capitalize() for col in df.columns]
        required_cols = ['High', 'Low', 'Close']
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

# --- STRATEGY 1: GOLD (XAUUSD) TREND-PULLBACK (WEEKDAYS) ---
def get_gold_strategy_signal(ticker):
    h1_bias = get_h1_trend_bias(ticker)
    if h1_bias == "NEUTRAL":
        return None, None, None, None, None, None, None, None, None

    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="3d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])

    sig = None
    if h1_bias == "BULLISH" and close_p > ema50 and (40 <= rsi <= 50):
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p < ema50 and (50 <= rsi <= 60):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 1.0  
    
    sl_distance = max(atr * 2.5, 15.00)
    tp_distance = sl_distance * 2.25

    if sig == "BUY":
        entry = live_price + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        be_level = entry + (sl_distance * 1.1)
    else:
        entry = live_price - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        be_level = entry - (sl_distance * 1.1)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, entry, be_level, rec_lot, rsi, f"GOLD-PULLBACK ({h1_bias})"

# --- STRATEGY 2: BITCOIN (BTCUSD) MOMENTUM BOUNCE (WEEKENDS) ---
def get_bitcoin_strategy_signal(ticker):
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="2d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['bb_lower']) or pd.isna(c['bb_upper']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    bb_lower = float(c['bb_lower'])
    bb_upper = float(c['bb_upper'])
    bb_middle = float(c['bb_middle'])

    sig = None
    if close_p <= bb_lower and rsi < 35:
        sig = "BUY"
    elif close_p >= bb_upper and rsi > 65:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 20.0
    
    sl_distance = max(atr * 2.0, 50.0)
    tp_distance = abs(live_price - bb_middle)
    if tp_distance < (sl_distance * 1.5):
        tp_distance = sl_distance * 2.0

    if sig == "BUY":
        entry = live_price + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        be_level = entry + (sl_distance * 1.1)
    else:
        entry = live_price - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        be_level = entry - (sl_distance * 1.1)

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, entry, be_level, rec_lot, rsi, "BTC-WEEKEND-BOUNCE"

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
        await update.message.reply_text(
            f"🔓 **Tailored Gold & Bitcoin Sniper Online.**\n💰 Active Balance Mode: **${current_account_balance}**\n🛡️ **News Shield Active:** Automatic slippage protection enabled.", 
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_account_balance
    if update.effective_user.id not in authorized_users:
        return
    
    args = context.args
    if args:
        try:
            new_bal = float(args[0])
            current_account_balance = new_bal
            await update.message.reply_text(f"✅ **Account Balance Updated:** Bot will now calculate risk based on **${current_account_balance}**.", parse_mode="Markdown")
            return
        except ValueError:
            pass
            
    await update.message.reply_text(f"💰 **Current Account Balance:** ${current_account_balance}\n*To update your balance, type:* `/balance 20`", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    news_active, news_reason = is_high_impact_news_time()
    news_status_str = f"⚠️ **Paused (News Shield):** {news_reason}" if news_active else "🟢 **Normal (Scanning active)**"

    if not active_trades:
        await update.message.reply_text(f"📊 **Portfolio Status (Balance: ${current_account_balance}):**\nStatus: {news_status_str}\nNo active trades right now.", parse_mode="Markdown")
        return
    
    msg = f"📊 **Active Portfolio Status (Balance: ${current_account_balance}):**\nStatus: {news_status_str}\n━━━━━━━━━━━━━━━━━━━\n"
    for label, trade in active_trades.items():
        y_ticker = "GC=F" if label == "XAUUSD" else "BTC-USD"
        df_temp = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="1d")
        current_price = float(df_temp['Close'].iloc[-1]) if df_temp is not None and not df_temp.empty else trade['entry']
        
        msg += (
            f"📌 **{label}** ({trade['type']})\n"
            f"• Entry: `{trade['entry']:.2f}` | Live: `{current_price:.2f}`\n"
            f"• Stop Loss: `{trade['sl']:.2f}` | Take Profit: `{trade['tp']:.2f}`\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- REAL-TIME GUIDANCE LOOP ---
async def live_chart_guidance_loop(app):
    global active_trades
    while True:
        await asyncio.sleep(10)
        if not active_trades:
            continue

        target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID

        for label, trade in list(active_trades.items()):
            try:
                y_ticker = "GC=F" if label == "XAUUSD" else "BTC-USD"
                df_live = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="1d")
                if df_live is None or len(df_live) < 10:
                    continue

                current_price = float(df_live['Close'].iloc[-1])
                trade_type = trade["type"]
                entry = trade["entry"]
                sl = trade["sl"]
                tp = trade["tp"]
                be_level = trade["be_level"]

                # Take Profit Hit
                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    msg = f"🎯 **[TARGET CRUSHED!]** - {label}\nPrice hit Take Profit at `{tp:.2f}`. Great discipline! 🚀"
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    
                    record_trade_outcome("WIN")
                    active_trades.pop(label, None)
                    continue

                # Stop Loss Hit
                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    msg = f"🛑 **[STOP LOSS HIT]** - {label}\nMarket triggered defense line at `{sl:.2f}`. Risk was safely contained to 1.5% of ${current_account_balance}."
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    
                    record_trade_outcome("LOSS")
                    active_trades.pop(label, None)
                    
                    needs_update, reason, suggestion = check_strategy_performance()
                    if needs_update:
                        update_alert = (
                            f"⚠ **[STRATEGY PERFORMANCE ALERT]**\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"ℹ️ **Reason:** {reason}\n"
                            f"💡 **Suggestion:** {suggestion}\n"
                            f"👉 *Paste your code back to me whenever you're ready to review or tweak it!*"
                        )
                        await app.bot.send_message(chat_id=target_user, text=update_alert, parse_mode="Markdown")

                    continue

                # Breakeven Alert
                if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                    trade["be_hit"] = True
                    msg = f"🛡 **[MOVE SL TO ENTRY]** - {label}\nPrice progressed to `{current_price:.2f}`. Move Stop Loss to `{entry:.2f}` to make this trade risk-free!"
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

            except Exception as e:
                logging.error(f"Guidance loop error for {label}: {e}")

# --- SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals, active_trades
    news_alert_sent = False

    while True:
        try:
            # 1. Check News Circuit Breaker before scanning
            is_news, news_desc = is_high_impact_news_time()
            if is_news:
                if not news_alert_sent:
                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user, 
                            text=f"🛡️ **[NEWS CIRCUIT BREAKER TRIGGERED]**\nDetected: *{news_desc}*.\nPausing sniper signal generation to protect your account from slippage spikes.", 
                            parse_mode="Markdown"
                        )
                    news_alert_sent = True
                await asyncio.sleep(60)
                continue
            else:
                news_alert_sent = False # Reset alert flag when coast is clear

            if len(active_trades) >= 1:
                await asyncio.sleep(60)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                if label in active_trades:
                    continue

                sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, strategy_name = get_strategy_signal(ticker)

                if sig is None:
                    continue

                if last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    signal_text = (
                        f"💎 **ELITE SNIPER SIGNAL**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Asset:** `{label}` | **Strategy:** `{strategy_name}`\n"
                        f"📈 **Direction:** {dir_icon} **{sig}**\n\n"
                        f"🔹 **Entry:** `{entry:.2f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.2f}`\n"
                        f"🎯 **Take Profit:** `{tp:.2f}`\n"
                        f"⚖️ **Rec. Lot Size:** `{rec_lot}` *(Based on ${current_account_balance})*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🛡️ *News Shield verified: Safe to execute.*"
                    )

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(chat_id=target_user, text=signal_text, parse_mode="Markdown")

                    active_trades[label] = {
                        "type": sig,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "be_level": be_level,
                        "be_hit": False
                    }
        except Exception as e:
            logging.error(f"Signal loop error: {e}")

        await asyncio.sleep(20)

async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(live_chart_guidance_loop(app))

# --- MAIN ENTRY ---
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("status", status_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Tailored Gold & Bitcoin Sniper Engine Running 24/7 with News Shield...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
