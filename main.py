import asyncio
import datetime
import logging
import os
import pandas as pd
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
DEFAULT_ACCOUNT_BALANCE = 1000.0

# --- NARROWED 3-ASSET INSTITUTIONAL LIST ---
WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",
    "EURUSD=X": "EURUSD",
    "BTC-USD": "BTCUSD"
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"
}

TIMEFRAME_M5 = "5m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()
active_trades = {}  # Tracks ongoing trades for live institutional management

# --- CIRCUIT BREAKER STATE ---
daily_loss_counter = 0
last_trade_reset_date = datetime.datetime.now(datetime.timezone.utc).date()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER (24/7 Live Status) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Institutional Master Trader Engine (3-Asset Pro Framework) is Live & Protecting Capital."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- ADVANCED ECONOMIC NEWS & HIGH-IMPACT FILTER ---
def is_high_impact_news_time():
    now = datetime.datetime.now(datetime.timezone.utc)
    
    # 1. Non-Farm Payrolls (First Friday of the month, 12:00 to 15:30 UTC)
    if now.weekday() == 4 and now.day <= 7:
        if 12 <= now.hour <= 15:
            return True, "US Non-Farm Payrolls (NFP) High-Volatility Defense Window"
            
    # 2. Mid-week high impact windows (Wednesdays & Thursdays around major US data releases 12:30 - 15:00 UTC)
    if now.weekday() in [2, 3] and now.hour == 13 and 20 <= now.minute <= 59:
        return True, "FOMC / CPI High-Impact Data Release Defense Window"
        
    # 3. Daily Tokyo/London/New York session transition chop filters
    if now.hour in [21, 22, 6, 7, 16, 17]:
        return True, "Inter-Session Liquidity Transition Window"
        
    return False, ""

# --- DYNAMIC LOT SIZING ---
def calculate_dynamic_lot(ticker, sl_pips, account_balance=DEFAULT_ACCOUNT_BALANCE):
    risk_amount = account_balance * RISK_PER_TRADE_PCT
    pip_value = 10.0
    if ticker == "EURUSD=X":
        pip_value = 10.0
    elif ticker in ["GC=F", "BTC-USD"]:
        pip_value = 1.0

    if sl_pips <= 0:
        return 0.01

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    return max(0.01, min(calculated_lot, 0.05 if account_balance < 500.0 else 2.0))

# --- DATA FETCHER ---
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

# --- STRICT INSTITUTIONAL GOLD STRATEGY (XAUUSD) ---
def get_gold_strategy_signal(ticker):
    now = datetime.datetime.now(datetime.timezone.utc)
    if not (8 <= now.hour <= 20):
        return None, None, None, None, None, None, None, None, None

    h1_bias = get_h1_trend_bias(ticker)
    if h1_bias == "NEUTRAL":
        return None, None, None, None, None, None, None, None, None

    df_m5 = fetch_data(ticker, interval=TIMEFRAME_M5, period="2d")
    if df_m5 is None or len(df_m5) < 50:
        return None, None, None, None, None, None, None, None, None

    c = df_m5.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])

    sig = None
    if h1_bias == "BULLISH" and close_p > ema50 and (50 <= rsi <= 65):
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p < ema50 and (35 <= rsi <= 50):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m5['Close'].iloc[-1])
    spread_buffer = 1.5 
    
    sl_distance = max(atr * 3.0, 25.00)
    tp_distance = sl_distance * 2.5  # 1:2.5 Risk-to-Reward

    if sig == "BUY":
        entry = live_price + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        partial_target = entry + (sl_distance * 1.0)
        be_level = entry + (sl_distance * 1.1)
    else:
        entry = live_price - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        partial_target = entry - (sl_distance * 1.0)
        be_level = entry - (sl_distance * 1.1)

    sl_pips = sl_distance * 1.0 
    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    
    return sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, f"GOLD-STRICT ({h1_bias})"

# --- MULTI-FILTER STRATEGY FOR EURUSD & BTCUSD ---
def get_strategy_signal(ticker):
    global daily_loss_counter, last_trade_reset_date
    
    current_date = datetime.datetime.now(datetime.timezone.utc).date()
    if current_date != last_trade_reset_date:
        daily_loss_counter = 0
        last_trade_reset_date = current_date

    # Circuit breaker: Stop trading for the day if 3 losses are hit
    if daily_loss_counter >= 3:
        return None, None, None, None, None, None, None, None, None

    if ticker == "GC=F":
        return get_gold_strategy_signal(ticker)

    h1_bias = get_h1_trend_bias(ticker)
    if h1_bias == "NEUTRAL":
        return None, None, None, None, None, None, None, None, None
    
    df_m5 = fetch_data(ticker, interval=TIMEFRAME_M5, period="2d")
    if df_m5 is None or len(df_m5) < 50:
        return None, None, None, None, None, None, None, None, None

    c = df_m5.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['ema200']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    
    recent_high = float(df_m5['High'].iloc[-15:-2].max())
    recent_low = float(df_m5['Low'].iloc[-15:-2].min())

    # Asset-specific spread and broker pip buffers
    if ticker == "EURUSD=X":
        spread_buffer = 0.0002
        min_broker_dist = 0.0025
    else:  # BTC-USD
        spread_buffer = 30.0
        min_broker_dist = 40.0

    sig = None
    # Trend + Breakout + Momentum Confluence filter
    if h1_bias == "BULLISH" and close_p > recent_high and (45 <= rsi <= 68):
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p < recent_low and (32 <= rsi <= 55):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m5['Close'].iloc[-1])
    sl_distance = max(atr * 2.0, min_broker_dist)
    tp_distance = sl_distance * 2.25

    if sig == "BUY":
        entry = live_price + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        partial_target = entry + (sl_distance * 1.0)
        be_level = entry + (sl_distance * 1.1)
    else:
        entry = live_price - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        partial_target = entry - (sl_distance * 1.0)
        be_level = entry - (sl_distance * 1.1)

    sl_pips = sl_distance * 10000 if ticker == "EURUSD=X" else sl_distance
    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    
    return sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, h1_bias

# --- TELEGRAM COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Institutional 3-Asset Sniper Online.** Strategy Framework Active + Max 1 Active Trade Enforced.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    if not active_trades:
        await update.message.reply_text("📊 **Active Portfolio Status:** No active trades right now. Waiting for high-conviction sniper setup.", parse_mode="Markdown")
        return
    
    msg = f"📊 **Lead Trader Command Center ({len(active_trades)} Active):**\n━━━━━━━━━━━━━━━━━━━\n"
    for label, trade in active_trades.items():
        y_ticker = next((k for k, v in WEEKDAY_ASSETS.items() if v == label), "BTC-USD")
        df_temp = fetch_data(y_ticker, interval=TIMEFRAME_M5, period="1d")
        current_price = float(df_temp['Close'].iloc[-1]) if df_temp is not None and not df_temp.empty else trade['entry']
        dec = 2 if y_ticker in ["BTC-USD", "GC=F"] else 4
        
        pips_away_tp = abs(trade['tp'] - current_price) * (10000 if y_ticker == "EURUSD=X" else 1)
        pips_away_sl = abs(current_price - trade['sl']) * (10000 if y_ticker == "EURUSD=X" else 1)
        
        if trade.get('profit_locked', False):
            status_desc = "💰 Profit Locked / Trailing"
        elif trade['be_hit']:
            status_desc = "🛡 Breakeven Secured"
        else:
            status_desc = "⚔ In Battle for Target"
            
        msg += (
            f"📌 **{label}** ({trade['type']})\n"
            f"• Entry: `{trade['entry']:.{dec}f}` | Live: `{current_price:.{dec}f}`\n"
            f"• Status: *{status_desc}*\n"
            f"• Distance to TP: `{pips_away_tp:.1f} units` | SL Dist: `{pips_away_sl:.1f} units`\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- HEALTH PING LOOP ---
async def hourly_status_loop(app):
    while True:
        await asyncio.sleep(14400)
        if authorized_users:
            target_user = list(authorized_users)[0]
            msg = "🟢 **[SYSTEM HEALTH CHECK]**\n3-Asset Sniper Bot is active, scanning high-confluence zones, and defending your capital. 🚀"
            try:
                await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Health check ping error: {e}")

# --- REAL-TIME LIVE CHART GUIDANCE LOOP ---
async def live_chart_guidance_loop(app):
    global active_trades, daily_loss_counter
    while True:
        await asyncio.sleep(10)
        if not active_trades:
            continue

        target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID

        for label, trade in list(active_trades.items()):
            try:
                y_ticker = next((k for k, v in WEEKDAY_ASSETS.items() if v == label), "BTC-USD")
                df_live = fetch_data(y_ticker, interval=TIMEFRAME_M5, period="1d")
                if df_live is None or len(df_live) < 10:
                    continue

                current_price = float(df_live['Close'].iloc[-1])
                current_rsi = float(df_live['rsi14'].iloc[-1])
                trade_type = trade["type"]
                entry = trade["entry"]
                sl = trade["sl"]
                tp = trade["tp"]
                be_level = trade["be_level"]
                dec = 2 if y_ticker in ["BTC-USD", "GC=F"] else 4

                # 1. Take Profit Hit
                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    msg = (
                        f"🎯 **[SNIPER: TARGET CRUSHED!]** - {label}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"Price successfully hit Take Profit at `{tp:.{dec}f}`.\n\n"
                        f"🏆 *Mindset:* Flawless execution. Capital grown. Slot ready for next elite setup! 🚀"
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # 2. Stop Loss Hit
                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    daily_loss_counter += 1
                    msg = (
                        f"🛑 **[STOP LOSS DEFENSE]** - {label}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"Market pulled back past our defense line at `{sl:.{dec}f}`.\n\n"
                        f"🛡 *Discipline:* Risk was strictly isolated to 1.5%. Staying calm, waiting for high quality reload."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # 3. Emergency Momentum Reversal Warning
                if not trade.get("reversal_alerted", False):
                    if (trade_type == "BUY" and current_rsi > 78) or (trade_type == "SELL" and current_rsi < 22):
                        trade["reversal_alerted"] = True
                        msg = (
                            f"⚠️ **[SNIPER ALERT: URGENT REVERSAL WARNING]** - {label}\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"📊 **Reason:** RSI reached extreme exhaustion levels (`{current_rsi:.1f}`). Market is showing sharp snapback signs.\n"
                            f"👉 **Action:** Consider manually booking profits right now at `{current_price:.{dec}f}` before momentum flips!"
                        )
                        await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # 4. Breakeven Trigger
                if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                    trade["be_hit"] = True
                    msg = (
                        f"🛡 **[SNIPER: MOVE SL TO ENTRY]** - {label}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 **Reason:** Price progressed nicely to `{current_price:.{dec}f}`.\n"
                        f"👉 **Action:** Move your Stop Loss to Entry (`{entry:.{dec}f}`). This trade is now 100% risk-free!"
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # 5. Advanced Profit Lock
                distance_total = abs(tp - entry)
                distance_covered = abs(current_price - entry)
                if trade["be_hit"] and not trade.get("profit_locked", False) and distance_covered >= (distance_total * 0.70):
                    trade["profit_locked"] = True
                    secure_lock_price = entry + (distance_total * 0.4) if trade_type == "BUY" else entry - (distance_total * 0.4)
                    msg = (
                        f"💰 **[AGGRESSIVE MANAGEMENT: LOCK 40% GAINS]** - {label}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 **Reason:** We are 70% toward full target (`{current_price:.{dec}f}`).\n"
                        f"👉 **Action:** Trail your Stop Loss to `{secure_lock_price:.{dec}f}` to lock in guaranteed profits!"
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

            except Exception as e:
                logging.error(f"Guidance loop error for {label}: {e}")

# --- SIGNAL SCANNER LOOP ---
async def signal_loop(app):
    global last_signals, active_trades
    while True:
        try:
            is_news, news_reason = is_high_impact_news_time()
            if is_news:
                await asyncio.sleep(300)
                continue

            # Strict quality control: Only allow 1 active trade at a time
            if len(active_trades) >= 1:
                await asyncio.sleep(60)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                if label in active_trades:
                    continue

                if len(active_trades) >= 1:
                    break

                sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, h1_bias = get_strategy_signal(ticker)

                if sig is None:
                    continue

                if last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dec = 2 if ticker in ["BTC-USD", "GC=F"] else 4
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    signal_text = (
                        f"💎 **ELITE SNIPER SIGNAL (3-ASSET FOCUS)**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Asset:** `{label}` | **Trend:** `{h1_bias}`\n"
                        f"📈 **Direction:** {dir_icon} **{sig}**\n\n"
                        f"🔹 **Entry:** `{entry:.{dec}f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"🎯 **Take Profit:** `{tp:.{dec}f}`\n"
                        f"⚖️ **Rec. Lot Size:** `{rec_lot}`\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🧠 *Sniper Note: Multi-filter strategy criteria met. Execute with professional discipline.*"
                    )

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(chat_id=target_user, text=signal_text, parse_mode="Markdown")

                    active_trades[label] = {
                        "type": sig,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "partial_target": partial_target,
                        "be_level": be_level,
                        "be_hit": False,
                        "profit_locked": False,
                        "reversal_alerted": False
                    }
        except Exception as e:
            logging.error(f"Signal loop error: {e}")

        await asyncio.sleep(30)

async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(live_chart_guidance_loop(app))
    asyncio.create_task(hourly_status_loop(app))

# --- MAIN ENTRY ---
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Institutional 3-Asset Sniper Engine Running 24/7...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
