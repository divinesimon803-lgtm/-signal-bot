import asyncio
import datetime
import logging
import os
import time
import aiohttp
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
    MessageHandler,
    filters,
    ContextTypes,
)

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8874815036:AAHYj9yIYbQ565mQ_szUxwaykEV7CO8ReoY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7889527038")  # Personal Master Chat ID
BOT_PASSCODE = os.getenv("BOT_PASSCODE", "5051")

# AGGRESSIVE RISK & ACCOUNT SAFETY CONFIGURATION
RISK_PER_TRADE_PCT = 0.015     # Slightly boosted risk profile for aggressive growth (1.5%)
MAX_DAILY_LOSS_PCT = 0.06      # Max 6% total account loss per day
MAX_CONSECUTIVE_LOSSES = 4     # Kept flexible to allow the engine to fight back
MAX_DAILY_WINS = 6             # Expanded daily target win capacity for an aggressive hunter

# STRICT ASSET ROSTER (yfinance ticker -> Signal Display Name)
WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",              # Gold Futures
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "JPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "CAD=X": "USDCAD",
    "NZDUSD=X": "NZDUSD",
    "CHF=X": "USDCHF",
    "BTC-USD": "BTCUSD"
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"
}

TIMEFRAME_M15 = "15m"
TIMEFRAME_H1 = "1h"

# --- SYSTEM STATE TRACKERS ---
last_signals = {}
authorized_users = set()
active_trades = {}      # Tracks ongoing trades for multi-stage aggressive lifecycle management

daily_stats = {
    "date": datetime.date.today(),
    "pnl_usd": 0.0,
    "consecutive_losses": 0,
    "daily_wins": 0,
    "trading_paused": False,
    "pause_reason": None
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Kings™ Aggressive Profit Hunter Engine is Live 💙!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- DYNAMIC RISK & LOT SIZE CALCULATOR ---
def calculate_dynamic_lot(ticker, sl_pips):
    account_balance = 10000.0  # Default base balance
    risk_amount = account_balance * RISK_PER_TRADE_PCT
    pip_value = 10.0  # Standard lot USD per pip on majors
    if "JPY" in ticker:
        pip_value = 6.5
    elif ticker in ["GC=F", "BTC-USD"]:
        pip_value = 1.0

    if sl_pips <= 0:
        return "0.02"

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    final_lot = max(0.02, min(calculated_lot, 3.0)) # Boosted max lot ceiling for aggressive growth
    return f"{final_lot:.2f}"

# --- NEWS & BANK HOLIDAY GUARD API ---
async def check_news_blackout():
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    year = datetime.date.today().year
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://date.nager.at/api/v3/PublicHolidays/{year}/US", timeout=aiohttp.ClientTimeout(total=4)) as res:
                if res.status == 200:
                    holidays = await res.json()
                    for h in holidays:
                        if h.get("date") == today_str:
                            logging.info(f"News Guard: Bank Holiday ({h.get('name')}). Pausing signal generation.")
                            return True
    except Exception as e:
        logging.error(f"News Guard check error: {e}")
    return False

# --- EXPANDED SESSION KILLZONE FILTER (AGRESSIVE TRADING HOURS) ---
def is_in_session_killzone(ticker):
    if ticker == "BTC-USD":
        return True

    now_utc = datetime.datetime.now(datetime.timezone.utc).time()
    # Expanded trading hours covering European, NY sessions, and active transition windows
    session_start = datetime.time(6, 0)
    session_end = datetime.time(18, 0)

    return session_start <= now_utc <= session_end

# --- MARKET DATA FETCHING ---
def fetch_data(ticker, interval, period="7d"):
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

        if len(close_series.dropna()) < 30:
            return None

        df['atr14'] = ta.volatility.average_true_range(high_series, low_series, close_series, window=14)
        df['ema50'] = ta.trend.ema_indicator(close_series, window=50)
        df['ema200'] = ta.trend.ema_indicator(close_series, window=200)
        df['rsi14'] = ta.momentum.rsi(close_series, window=14)

        return df

    except Exception as e:
        logging.error(f"Error fetching data for {ticker} ({interval}): {e}")
        return None

def get_h1_trend_bias(ticker):
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="14d")
    if df_h1 is None or len(df_h1) < 20:
        return "NEUTRAL"
    
    latest_close = float(df_h1['Close'].iloc[-1])
    latest_ema200 = float(df_h1['ema200'].iloc[-1]) if 'ema200' in df_h1 and pd.notna(df_h1['ema200'].iloc[-1]) else latest_close

    if latest_close > latest_ema200:
        return "BULLISH"
    elif latest_close < latest_ema200:
        return "BEARISH"
    return "NEUTRAL"

# --- AGGRESSIVE MULTI-STRATEGY CONFLUENCE ENGINE ---
def get_multi_strategy_signal(ticker):
    if not is_in_session_killzone(ticker):
        return None, None, None, None, None, None, None, None

    h1_bias = get_h1_trend_bias(ticker)
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 20:
        return None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    prev_c = df_m15.iloc[-3]
    
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['ema200']):
        return None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])
    ema200 = float(c['ema200'])

    recent_high = float(df_m15['High'].iloc[-12:-3].max())
    recent_low = float(df_m15['Low'].iloc[-12:-3].min())

    # Aggressive breakout triggers allowing quick market responsiveness
    apa_buy = close_p > recent_high and float(prev_c['Close']) <= recent_high
    apa_sell = close_p < recent_low and float(prev_c['Close']) >= recent_low

    ema_buy = close_p > ema50
    ema_sell = close_p < ema50

    rsi_buy = rsi < 75 and rsi > 40  # Warmed up RSI parameters to capture aggressive momentum
    rsi_sell = rsi > 25 and rsi < 60

    sig = None
    if (apa_buy or ema_buy) and rsi_buy:
        sig = "BUY"
    elif (apa_sell or ema_sell) and rsi_sell:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None

    # Aggressive reward multipliers (Targeting 3.5x for massive win potential)
    tp_multiplier = 3.5 
    risk_distance = abs(close_p - (recent_low if sig == "BUY" else recent_high)) + (atr * 0.2)
    sl_pips = risk_distance * 10000 if "JPY" not in ticker else risk_distance * 100

    if sig == "BUY":
        sl = close_p - risk_distance
        tp = close_p + (risk_distance * tp_multiplier)
        partial_target = close_p + (risk_distance * 1.0)
        be_level = close_p + (risk_distance * 1.8)
        runner_target = close_p + (risk_distance * 2.8)
    else:
        sl = close_p + risk_distance
        tp = close_p - (risk_distance * tp_multiplier)
        partial_target = close_p - (risk_distance * 1.0)
        be_level = close_p - (risk_distance * 1.8)
        runner_target = close_p - (risk_distance * 2.8)

    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    return sig, close_p, sl, tp, partial_target, be_level, runner_target, rec_lot

# --- CIRCUIT BREAKER & DAILY TARGET LOCK ---
def check_circuit_breaker():
    global daily_stats
    today = datetime.date.today()
    
    if daily_stats["date"] != today:
        daily_stats = {
            "date": today,
            "pnl_usd": 0.0,
            "consecutive_losses": 0,
            "daily_wins": 0,
            "trading_paused": False,
            "pause_reason": None
        }
        return True, "Trading Active (Aggressive Mode)"

    if daily_stats["daily_wins"] >= MAX_DAILY_WINS:
        daily_stats["trading_paused"] = True
        return False, f"Target Locked: Secured {MAX_DAILY_WINS} wins today! Unstoppable session 💙"

    if daily_stats["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        daily_stats["trading_paused"] = True
        return False, f"Paused: Hit max {MAX_CONSECUTIVE_LOSSES} losses. Resetting momentum."

    if daily_stats["trading_paused"]:
        return False, daily_stats.get("pause_reason", "Trading paused by Circuit Breaker.")

    return True, "Trading Active (Aggressive Mode)"

# --- TELEGRAM COMMAND HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Passcode accepted!** Aggressive Profit Hunter active 💙🔥", parse_mode="Markdown")
    elif user_id in authorized_users:
        await update.message.reply_text("🟢 **Hunter Engine Active.** Send `/status` for health.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Send the passcode directly to authorize.", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 Access Denied.")
        return

    is_active, status_msg = check_circuit_breaker()

    msg = (
        f"📊 **AGGRESSIVE PROFIT HUNTER STATUS**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **Status:** {status_msg}\n"
        f"🎯 **Today's Wins:** {daily_stats['daily_wins']} / {MAX_DAILY_WINS}\n"
        f"🛡️ **Active Positions Monitored:** {len(active_trades)}\n"
        f"💙 **Motto:** We fight for wins! We pray for blue 💙🔥\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message and update.message.text else ""

    if text == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Passcode accepted!** Aggressive Hunter active 💙.", parse_mode="Markdown")
        return

    if user_id in authorized_users:
        is_active, status_msg = check_circuit_breaker()
        await update.message.reply_text(f"🟢 **Hunter Status:** {status_msg}\nUse `/status` to review engine health.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Send correct passcode in direct messages.", parse_mode="Markdown")

# --- MULTI-STAGE AGGRESSIVE TRADE LIFECYCLE MENTOR LOOP ---
async def trade_lifecycle_mentor_loop(app):
    global active_trades, daily_stats
    while True:
        await asyncio.sleep(45)  # Faster polling for real-time tactical modifications
        if not active_trades:
            continue

        target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID

        for label, trade in list(active_trades.items()):
            try:
                y_ticker = next((k for k, v in WEEKDAY_ASSETS.items() if v == label), None)
                if not y_ticker and label == "BTC-USD":
                    y_ticker = "BTC-USD"
                
                if not y_ticker:
                    continue

                df_live = fetch_data(y_ticker, interval=TIMEFRAME_M15, period="1d")
                if df_live is None or df_live.empty:
                    continue

                current_price = float(df_live['Close'].iloc[-1])
                trade_type = trade["type"]
                entry = trade["entry"]
                sl = trade["sl"]
                tp = trade["tp"]
                partial_target = trade["partial_target"]
                be_level = trade["be_level"]
                runner_target = trade["runner_target"]

                dec = 3 if "JPY" in y_ticker else (2 if y_ticker in ["BTC-USD", "GC=F"] else 4)

                # --- TAKE PROFIT HIT ---
                if (trade_type == "BUY" and current_price >= tp) or (trade_type == "SELL" and current_price <= tp):
                    daily_stats["daily_wins"] += 1
                    msg = (
                        f"🎯 **[MOONSHOT TARGET SMASHED] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"✅ Price conquered full Take Profit at `{tp:.{dec}f}`!\n\n"
                        f"Absolute domination! We fought for every pip and won. We pray for blue 💙🔥."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # --- STOP LOSS HIT ---
                elif (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    msg = (
                        f"🛑 **[STOP LOSS HIT] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"❌ Price hit Stop Loss at `{sl:.{dec}f}`.\n\n"
                        f"Controlled risk. We stay sharp, reload, and hunt right back! 💙"
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    daily_stats["consecutive_losses"] += 1
                    continue

                # --- STAGE 1: INITIAL PARTIAL PROFIT TRIGGER (1.0R) ---
                if not trade["partial_hit"] and ((trade_type == "BUY" and current_price >= partial_target) or (trade_type == "SELL" and current_price <= partial_target)):
                    trade["partial_hit"] = True
                    msg = (
                        f"⚡ **TACTICAL ALERT: TAKE PARTIALS - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 Price reached 1.0R profit at `{current_price:.{dec}f}`.\n"
                        f"👉 **Action:** Close 50% of your position now to bank guaranteed profit, and let the rest run aggressively!"
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # --- STAGE 2: BREAKEVEN LOCK TRIGGER (1.8R) ---
                if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                    trade["be_hit"] = True
                    msg = (
                        f"🛡️ **TACTICAL ALERT: MOVE TO BREAKEVEN - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 Price pushed to `{current_price:.{dec}f}`.\n"
                        f"👉 **Action:** Shift your Stop Loss to your entry price (`{entry:.{dec}f}`). Trade is now 100% risk-free!"
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # --- STAGE 3: RUNNER TRAILING STOP TRIGGER (2.8R) ---
                if not trade["runner_hit"] and ((trade_type == "BUY" and current_price >= runner_target) or (trade_type == "SELL" and current_price <= runner_target)):
                    trade["runner_hit"] = True
                    msg = (
                        f"🚀 **TACTICAL ALERT: TRAIL RUNNERS - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🔥 Price surged to `{current_price:.{dec}f}`.\n"
                        f"👉 **Action:** Trail your Stop Loss closer to lock in massive gains as we charge toward the final moonshot target!"
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

            except Exception as e:
                logging.error(f"Trade lifecycle mentor error for {label}: {e}")

# --- HEARTBEAT & SCANNER LOOPS ---
async def heartbeat_loop(app):
    while True:
        try:
            wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            formatted_wat = wat_time.strftime("%I:%M %p WAT")
            is_active, status_msg = check_circuit_breaker()
            status_icon = "🟢" if is_active else "🔴"
            
            msg_text = f"{status_icon} *[Aggressive Hunter Heartbeat]* Status: {status_msg} ({formatted_wat}) | We fight for wins 💙"
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg_text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")
        await asyncio.sleep(3600)

async def signal_loop(app):
    global last_signals, active_trades
    while True:
        try:
            is_active, _ = check_circuit_breaker()
            if not is_active:
                await asyncio.sleep(300)
                continue

            if len(active_trades) >= 4:  # Increased simultaneous active trade slots for high aggression
                await asyncio.sleep(45)
                continue

            if await check_news_blackout():
                await asyncio.sleep(1800)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                if len(active_trades) >= 4:
                    break

                if label in active_trades:
                    continue

                sig, entry, sl, tp, partial_target, be_level, runner_target, rec_lot = get_multi_strategy_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dec = 3 if "JPY" in ticker else (2 if ticker in ["BTC-USD", "GC=F"] else 4)

                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    expires_wat = now_wat + datetime.timedelta(minutes=20)
                    time_sent_str = now_wat.strftime("%I:%M %p")
                    time_expire_str = expires_wat.strftime("%I:%M %p")
                    dir_emoji = "🟢" if sig == "BUY" else "🔴"

                    # Clean, high-octane signal alert format
                    personal_signal_text = (
                        f"🔥 **AGGRESSIVE PROFIT HUNTER SIGNAL** 🔥\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Pair:** `{label}`\n"
                        f"📈 **Action:** {dir_emoji} **{sig}**\n\n"
                        f"🔹 **Entry:** `{entry:.{dec}f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"🎯 **Take Profit:** `{tp:.{dec}f}`\n"
                        f"💰 **Lot Size:** `{rec_lot}`\n\n"
                        f"🕒 **Time:** `{time_sent_str} WAT` | ⏳ **Valid:** `{time_expire_str} WAT`\n\n"
                        f"💙 *We fight for wins! We pray for blue* 💙🔥"
                    )

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(
                        chat_id=target_user,
                        text=personal_signal_text,
                        parse_mode="Markdown"
                    )

                    active_trades[label] = {
                        "label": label,
                        "type": sig,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "partial_target": partial_target,
                        "be_level": be_level,
                        "runner_target": runner_target,
                        "partial_hit": False,
                        "be_hit": False,
                        "runner_hit": False,
                        "time_opened": time.time()
                    }

        except Exception as e:
            logging.error(f"Signal Loop error: {e}")

        await asyncio.sleep(45)

async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat_loop(app))
    asyncio.create_task(trade_lifecycle_mentor_loop(app))

# --- MAIN ENTRY POINT ---
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Kings™ Aggressive Profit Hunter Active & Running 💙🔥...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
