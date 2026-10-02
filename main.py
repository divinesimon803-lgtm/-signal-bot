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

# --- CIRCUIT BREAKER STATE ---
daily_loss_counter = 0
last_trade_reset_date = datetime.datetime.now(datetime.timezone.utc).date()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER (24/7 Live Status) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Tailored Gold/Bitcoin Sniper Engine is Live & Protecting Capital."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- HIGH-IMPACT NEWS DEFENSE ---
def is_high_impact_news_time():
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() == 4 and now.day <= 7 and 12 <= now.hour <= 15:
        return True, "US NFP Defense Window"
    if now.weekday() in [2, 3] and now.hour == 13 and 20 <= now.minute <= 59:
        return True, "FOMC / CPI Defense Window"
    return False, ""

# --- DYNAMIC LOT SIZING (WITH BROKER REJECTION PREVENTION) ---
def calculate_dynamic_lot(ticker, sl_pips, account_balance=DEFAULT_ACCOUNT_BALANCE):
    risk_amount = account_balance * RISK_PER_TRADE_PCT
    pip_value = 1.0
    if ticker == "GC=F":
        pip_value = 1.0  # Gold dollar value scaling
    elif ticker == "BTC-USD":
        pip_value = 1.0  # BTC dollar value scaling

    if sl_pips <= 0:
        return 0.01

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    return max(0.01, min(calculated_lot, 0.05 if account_balance < 500.0 else 2.0))

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
        
        # Bollinger Bands for Bitcoin Weekend Strategy
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
    # Healthy Pullback Criteria
    if h1_bias == "BULLISH" and close_p > ema50 and (40 <= rsi <= 50):
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p < ema50 and (50 <= rsi <= 60):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 1.0  
    
    # Safe minimum distance to prevent broker rejections
    sl_distance = max(atr * 2.5, 15.00)
    tp_distance = sl_distance * 2.25  # 1:2.25 Risk-to-Reward

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
    # Weekend Mean-Reversion / Bounce Criteria
    if close_p <= bb_lower and rsi < 35:
        sig = "BUY"
    elif close_p >= bb_upper and rsi > 65:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m15['Close'].iloc[-1])
    spread_buffer = 20.0
    
    sl_distance = max(atr * 2.0, 50.0)
    tp_distance = abs(live_price - bb_middle) # Target the middle band
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
        await update.message.reply_text("🔓 **Tailored Gold & Bitcoin Sniper Online.** 15m Framework Active + Anti-Rejection Shield.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied.*", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    if not active_trades:
        await update.message.reply_text("📊 **Portfolio Status:** No active trades right now. Sniper is scanning for clean setups.", parse_mode="Markdown")
        return
    
    msg = f"📊 **Active Portfolio Status ({len(active_trades)} Active):**\n━━━━━━━━━━━━━━━━━━━\n"
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
                    active_trades.pop(label, None)
                    continue

                # Stop Loss Hit
                if (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    msg = f"🛑 **[STOP LOSS HIT]** - {label}\nMarket triggered defense line at `{sl:.2f}`. Risk was safely contained to 1.5%. Staying calm."
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
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
    while True:
        try:
            if len(active_trades) >= 1:
                await asyncio.sleep(60)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            # Weekdays = Gold, Weekends = Bitcoin
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
                        f"⚖️ **Rec. Lot Size:** `{rec_lot}`\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🧠 *Execute with professional discipline on demo!*"
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
    app.add_handler(CommandHandler("status", status_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Tailored Gold & Bitcoin Sniper Engine Running 24/7...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
