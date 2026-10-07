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

# --- DAILY LOSS LIMIT STATE ---
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
active_trades = {}  # Tracks ongoing trades for live institutional management

# --- STRATEGY PERFORMANCE MONITORING & AUTO-DIAGNOSIS ---
trade_history = []  
MAX_HISTORY_LEN = 20  
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

def check_strategy_performance():
    global strategy_alert_sent
    if len(trade_history) < 10:
        return False, "", ""
    
    losses_in_row = 0
    for outcome in reversed(trade_history):
        if outcome == "LOSS":
            losses_in_row += 1
        else:
            break

    recent_window = trade_history[-15:]
    recent_losses = recent_window.count("LOSS")
    
    if losses_in_row >= 5 or (len(recent_window) >= 10 and (recent_losses / len(recent_window)) >= 0.75):
        if not strategy_alert_sent:
            strategy_alert_sent = True
            reason = f"Detected a sustained drawdown ({losses_in_row} consecutive losses or heavy failure rate in recent window)."
            suggestion = "Market volatility regime may have shifted. Consider tweaking RSI boundary thresholds or EMA trend filters."
            return True, reason, suggestion
            
    return False, "", ""

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

# --- AUTOMATIC NEWS CIRCUIT BREAKER ---
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
                            return True, f"High-Impact News Event: {ev.get('title')} ({ev.get('country')})"
        
        if now_utc.weekday() == 4 and now_utc.hour >= 20:
            return True, "Weekend Market Close Volatility Window"
        if now_utc.weekday() == 6 and now_utc.hour < 1:
            return True, "Weekend Market Open Volatility Window"

    except Exception as e:
        logging.error(f"News check API error: {e}")

    return False, ""

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Smart Pro Gold/Bitcoin Sniper Engine is Live with 24/7 BTC & Broker-Safe Stop Buffers."

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

# --- OPTIMISED GOLD (XAUUSD) STRATEGY (High-Quality Pullback) ---
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
    open_p = float(c['Open'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])

    is_bullish_candle = close_p > open_p
    is_bearish_candle = close_p < open_p

    sig = None
    if h1_bias == "BULLISH" and close_p >= ema50 and (35 <= rsi <= 58) and is_bullish_candle:
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p <= ema50 and (42 <= rsi <= 65) and is_bearish_candle:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 0.5  
    
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

    rec_lot = calculate_dynamic_lot(ticker, min_broker_stop_distance)
    return sig, entry, sl, tp, entry, be_level, rec_lot, rsi, f"SMART-GOLD ({h1_bias})"

# --- OPTIMISED BITCOIN (BTCUSD) STRATEGY ---
def get_bitcoin_strategy_signal(ticker):
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="2d")
    if df_m15 is None or len(df_m15) < 50:
        return None, None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['bb_lower']) or pd.isna(c['bb_upper']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    open_p = float(c['Open'])
    rsi = float(c['rsi14'])
    bb_lower = float(c['bb_lower'])
    bb_upper = float(c['bb_upper'])
    bb_middle = float(c['bb_middle'])

    is_bullish_candle = close_p > open_p
    is_bearish_candle = close_p < open_p

    sig = None
    if close_p <= bb_lower and rsi < 40 and is_bullish_candle:
        sig = "BUY"
    elif close_p >= bb_upper and rsi > 60 and is_bearish_candle:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 10.0
    
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

    rec_lot = calculate_dynamic_lot(ticker, sl_distance)
    return sig, entry, sl, tp, entry, be_level, rec_lot, rsi, "SMART-BTC-BOUNCE"

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
            f"🔓 **Smart Pro Sniper Bot Online.**\n💰 Balance Mode: **${current_account_balance}**\n🛡️ **24/7 BTC & Broker-Safe Filters Active.**", 
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
            await update.message.reply_text(f"✅ **Balance Updated:** Risk calculated on **${current_account_balance}**.", parse_mode="Markdown")
            return
        except ValueError:
            pass
            
    await update.message.reply_text(f"💰 **Current Account Balance:** ${current_account_balance}\n*To update:* `/balance 20`", parse_mode="Markdown")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trades, last_signals, daily_losses_count, daily_loss_limit_alert_sent
    if update.effective_user.id not in authorized_users:
        return
    active_trades.clear()
    last_signals.clear()
    daily_losses_count = 0
    daily_loss_limit_alert_sent = False
    await update.message.reply_text("🔄 **Bot Reset Successful!**\nAll active trade states, cache, and daily loss counters reset.", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    news_active, news_reason = is_high_impact_news_time()
    status_msg = "🟢 **Optimal (Scanning Active 24/7 for BTC & Weekdays for Gold)**"
    if news_active:
        status_msg = f"⚠️ **Paused (News Shield):** {news_reason}"
    elif daily_losses_count >= 3:
        status_msg = f"🛑 **Paused (Daily Loss Limit Reached: {daily_losses_count}/3)**"

    if not active_trades:
        await update.message.reply_text(f"📊 **Smart Bot Status (Balance: ${current_account_balance} | Losses Today: {daily_losses_count}/3):**\nState: {status_msg}\nNo active trades.", parse_mode="Markdown")
        return
    
    msg = f"📊 **Active Portfolio Status (Balance: ${current_account_balance} | Losses Today: {daily_losses_count}/3):**\nState: {status_msg}\n━━━━━━━━━━━━━━━━━━━\n"
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
                tp = trade["tp"]
                sl = trade["sl"]
                be_level = trade["be_level"]
                entry = trade["entry"]

                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    msg = f"🎯 **[TARGET SECURED!]** - {label}\nPrice hit Take Profit at `{tp:.2f}`. Profit locked in! 🚀"
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    record_trade_outcome("WIN")
                    active_trades.pop(label, None)
                    continue

                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    msg = f"🛑 **[STOP LOSS HIT]** - {label}\nMarket defended at `{sl:.2f}`. Risk contained cleanly."
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    record_trade_outcome("LOSS")
                    active_trades.pop(label, None)
                    continue

                if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                    trade["be_hit"] = True
                    msg = f"🛡 **[PROTECT TRADE]** - {label}\nMove Stop Loss to entry (`{entry:.2f}`) to make this trade risk-free."
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

            except Exception as e:
                logging.error(f"Guidance error for {label}: {e}")

# --- SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals, active_trades, today_date, daily_losses_count, daily_loss_limit_alert_sent
    news_alert_sent = False

    while True:
        try:
            # Daily Loss Limit & Date Reset Check
            now_date = datetime.datetime.now(datetime.timezone.utc).date()
            if now_date != today_date:
                today_date = now_date
                daily_losses_count = 0
                daily_loss_limit_alert_sent = False

            if daily_losses_count >= 3:
                if not daily_loss_limit_alert_sent:
                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    if target_user and authorized_users:
                        await app.bot.send_message(
                            chat_id=target_user,
                            text="🛑 **[DAILY LOSS LIMIT REACHED]**\n3 losses recorded today. Automated trading is paused until tomorrow to protect capital.",
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
                            text=f"🛡️ **[NEWS SHIELD ENGAGED]**\nPaused due to: *{news_desc}*.", 
                            parse_mode="Markdown"
                        )
                    news_alert_sent = True
                await asyncio.sleep(60)
                continue
            else:
                news_alert_sent = False

            if len(active_trades) >= 2:  
                await asyncio.sleep(60)
                continue

            all_assets = {**WEEKDAY_ASSETS, **WEEKEND_ASSETS}

            for ticker, label in all_assets.items():
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

                    signal_text = (
                        f"💎 **SMART PRO SNIPER SIGNAL**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Asset:** `{label}` | **Strategy:** `{strategy_name}`\n"
                        f"📈 **Direction:** {dir_icon} **{sig}**\n\n"
                        f"🔹 **Entry:** `{entry:.2f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.2f}`\n"
                        f"🎯 **Take Profit:** `{tp:.2f}`\n"
                        f"⚖️ **Rec. Lot Size:** `{rec_lot}` *(Based on ${current_account_balance})*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🛡️ *Broker Stop Levels & Candle Action Verified.*"
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
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("clear", reset_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Smart Pro Sniper Bot Running with 24/7 BTC & Broker-Safe Stop Buffers...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
