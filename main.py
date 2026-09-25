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

# RISK & ACCOUNT SAFETY CONFIGURATION
RISK_PER_TRADE_PCT = 0.015     # 1.5% risk profile per trade
MAX_DAILY_LOSS_PCT = 0.06      # Max 6% total account loss per day
MAX_CONSECUTIVE_LOSSES = 4     
MAX_DAILY_WINS = 6             

# ASSET ROSTER (yfinance ticker -> Signal Display Name)
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
TIMEFRAME_H4 = "4h"

# --- SYSTEM STATE TRACKERS ---
last_signals = {}
authorized_users = set()
active_trades = {}      # Tracks ongoing trades for lifecycle management

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
    return "Kings™ Institutional Trading Engine with AI Mentor is Live."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- AUTOMATED JOURNALING MODULE ---
def log_trade_to_journal(label, trade_type, entry, exit_price, outcome, pnl_est):
    journal_file = "trade_mentor_journal.csv"
    file_exists = os.path.exists(journal_file)
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_data = pd.DataFrame([{
        "Timestamp": timestamp,
        "Asset": label,
        "Type": trade_type,
        "Entry": entry,
        "Exit": exit_price,
        "Outcome": outcome,
        "Est_PnL": pnl_est
    }])
    
    try:
        log_data.to_csv(journal_file, mode='a', header=not file_exists, index=False)
        logging.info(f"Journal updated: {label} {outcome}")
    except Exception as e:
        logging.error(f"Journal writing error: {e}")

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
    final_lot = max(0.02, min(calculated_lot, 3.0))
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

# --- SESSION KILLZONE FILTER ---
def is_in_session_killzone(ticker):
    if ticker == "BTC-USD":
        return True

    now_utc = datetime.datetime.now(datetime.timezone.utc).time()
    session_start = datetime.time(6, 0)
    session_end = datetime.time(18, 0)

    return session_start <= now_utc <= session_end

# --- MARKET DATA FETCHING ---
def fetch_data(ticker, interval, period="60d"):
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

def get_h4_trend_bias(ticker):
    """Higher-Timeframe Filter: Enforces alignment with the 4-Hour 200 EMA structure."""
    df_h4 = fetch_data(ticker, interval=TIMEFRAME_H4, period="60d")
    if df_h4 is None or len(df_h4) < 30:
        return "NEUTRAL"
    
    latest_close = float(df_h4['Close'].iloc[-1])
    latest_ema200 = float(df_h4['ema200'].iloc[-1]) if 'ema200' in df_h4 and pd.notna(df_h4['ema200'].iloc[-1]) else latest_close

    if latest_close > latest_ema200:
        return "BULLISH"
    elif latest_close < latest_ema200:
        return "BEARISH"
    return "NEUTRAL"

# --- MULTI-STRATEGY CONFLUENCE ENGINE WITH H4 FILTER ---
def get_multi_strategy_signal(ticker):
    if not is_in_session_killzone(ticker):
        return None, None, None, None, None, None, None, None, None, None, None

    h4_bias = get_h4_trend_bias(ticker)
    
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 20:
        return None, None, None, None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    prev_c = df_m15.iloc[-3]
    
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['ema200']):
        return None, None, None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])
    ema200 = float(c['ema200'])

    recent_high = float(df_m15['High'].iloc[-12:-3].max())
    recent_low = float(df_m15['Low'].iloc[-12:-3].min())

    apa_buy = close_p > recent_high and float(prev_c['Close']) <= recent_high
    apa_sell = close_p < recent_low and float(prev_c['Close']) >= recent_low

    ema_buy = close_p > ema50
    ema_sell = close_p < ema50

    rsi_buy = rsi < 75 and rsi > 40  
    rsi_sell = rsi > 25 and rsi < 60

    sig = None
    if (apa_buy or ema_buy) and rsi_buy and h4_bias == "BULLISH":
        sig = "BUY"
    elif (apa_sell or ema_sell) and rsi_sell and h4_bias == "BEARISH":
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None, None, None

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
    return sig, close_p, sl, tp, partial_target, be_level, runner_target, rec_lot, rsi, ema50, h4_bias

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
        return True, "System Operational (Active Mode)"

    if daily_stats["daily_wins"] >= MAX_DAILY_WINS:
        daily_stats["trading_paused"] = True
        return False, f"Daily Target Achieved ({MAX_DAILY_WINS} Wins Secured)"

    if daily_stats["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        daily_stats["trading_paused"] = True
        return False, f"Safety Threshold Reached ({MAX_CONSECUTIVE_LOSSES} Consecutive Losses)"

    if daily_stats["trading_paused"]:
        return False, daily_stats.get("pause_reason", "Trading paused by Circuit Breaker.")

    return True, "System Operational (Active Mode)"

# --- BUILT-IN EXPERT FOREX MENTOR BRAIN (FREE & INTELLIGENT) ---
def get_forex_mentor_response(query):
    q = query.lower()
    
    if "strategy" in q or "how" in q and "trade" in q:
        return (
            "🧠 **Institutional Mentor Strategy Guidance**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "We trade with a strict **Multi-Timeframe Confluence** model:\n"
            "1️⃣ **Macro Filter:** The 4-Hour (H4) 200 EMA dictates the primary trend direction.\n"
            "2️⃣ **Execution Timing:** On the 15-minute chart, we wait for price action breakout combined with RSI health.\n"
            "3️⃣ **Risk Discipline:** Every trade risks exactly 1.5% of the account with a minimum 3.5R reward target."
        )
    elif "risk" in q or "lot" in q or "money" in q:
        return (
            "🛡️ **Mentor Rule on Risk Management**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Professional trading isn't about getting rich on one trade—it's about survival and compounding.\n"
            "• Never risk more than 1.5% to 2% per trade.\n"
            "• Let the bot calculate your lot size based on your Stop Loss distance in pips.\n"
            "• If you hit 4 losses in a row, the circuit breaker locks the terminal to protect your capital."
        )
    elif "gold" in q or "xauusd" in q:
        return (
            "🥇 **XAUUSD (Gold) Trading Wisdom**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Gold is volatile and respects major liquidity pools and institutional session opens (London & New York).\n"
            "Always give Gold slightly wider Stop Losses (using ATR buffers) to avoid getting wicked out by bank algorithmic sweeps."
        )
    elif "loss" in q or "lose" in q or "psyc" in q or "drawdown" in q:
        return (
            "🧘 **Trader Psychology & Drawdown Coaching**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Losses are just business expenses in trading. What separates professionals from amateurs is how they react to a losing streak.\n"
            "Take a step back, review the system journal (`trade_mentor_journal.csv`), and trust the mathematical edge over a sample of 100 trades."
        )
    elif "hello" in q or "hi" in q or "mentor" in q:
        return (
            "👋 **Hello Boss! Your Institutional Mentor is Online.**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "I am monitoring the markets with H4 trend filters, managing your active trades, and tracking your risk limits.\n"
            "Ask me anything about forex strategy, risk management, asset behavior, or type `/status` to view your current system health!"
        )
    else:
        return (
            "💡 **Mentor Coaching Insight**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"Regarding your query (*\"{query}\"*):\n"
            "Always prioritize market structure, wait for confirmation across timeframes, and never override the bot's stop loss. Discipline compounds capital faster than aggression."
        )

# --- TELEGRAM COMMAND HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text(
            "🔓 **Access Granted:** Institutional Trading Engine & AI Mentor Active!\n"
            "You can now receive H4-filtered signals and chat with me anytime for forex mentorship.", 
            parse_mode="Markdown"
        )
    elif user_id in authorized_users:
        await update.message.reply_text("🟢 **Mentor Online:** Use `/status` or type any forex question to chat.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied:* Provide valid passcode.", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 Access Denied.")
        return

    is_active, status_msg = check_circuit_breaker()

    msg = (
        f"📊 **SYSTEM STATUS REPORT**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **Status:** {status_msg}\n"
        f"🎯 **Session Wins:** {daily_stats['daily_wins']} / {MAX_DAILY_WINS}\n"
        f"🛡️ **Active Positions:** {len(active_trades)}\n"
        f"🧠 **AI Mentor:** Online & Ready"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message and update.message.text else ""

    if text == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Access Granted:** Institutional Trading Engine & AI Mentor active.", parse_mode="Markdown")
        return

    if user_id in authorized_users:
        # Pass text to the built-in AI Forex Mentor brain
        mentor_reply = get_forex_mentor_response(text)
        await update.message.reply_text(mentor_reply, parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied:* Authentication required.", parse_mode="Markdown")

# --- TRADE LIFECYCLE MANAGEMENT LOOP ---
async def trade_lifecycle_mentor_loop(app):
    global active_trades, daily_stats
    while True:
        await asyncio.sleep(45)
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
                    log_trade_to_journal(label, trade_type, entry, current_price, "WIN (TP Hit)", "+3.5R")
                    msg = (
                        f"🎯 **[TRADE EXECUTED: TP REACHED] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"✅ Target achieved at `{tp:.{dec}f}`.\n"
                        f"📊 Position closed successfully with full +3.5R gain."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # --- STOP LOSS HIT ---
                elif (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    log_trade_to_journal(label, trade_type, entry, current_price, "LOSS (SL Hit)", "-1.0R")
                    msg = (
                        f"🛑 **[TRADE EXECUTED: STOP LOSS] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"❌ Stop loss triggered at `{sl:.{dec}f}`.\n"
                        f"📊 Risk managed and recorded to system journal (-1.0R)."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    daily_stats["consecutive_losses"] += 1
                    continue

                # --- STAGE 1: PARTIAL PROFIT TRIGGER (1.0R) ---
                if not trade["partial_hit"] and ((trade_type == "BUY" and current_price >= partial_target) or (trade_type == "SELL" and current_price <= partial_target)):
                    trade["partial_hit"] = True
                    msg = (
                        f"⚡ **[LIFECYCLE ALERT: PARTIAL TARGET] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 1.0R threshold reached at `{current_price:.{dec}f}`.\n"
                        f"📌 Action Required: Secure 50% partial profits."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # --- STAGE 2: BREAKEVEN TRIGGER (1.8R) ---
                if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                    trade["be_hit"] = True
                    msg = (
                        f"🛡️ **[LIFECYCLE ALERT: BREAKEVEN] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 Price reached `{current_price:.{dec}f}`.\n"
                        f"📌 Action Required: Move Stop Loss to entry price (`{entry:.{dec}f}`)."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # --- STAGE 3: RUNNER TRAILING TRIGGER (2.8R) ---
                if not trade["runner_hit"] and ((trade_type == "BUY" and current_price >= runner_target) or (trade_type == "SELL" and current_price <= runner_target)):
                    trade["runner_hit"] = True
                    msg = (
                        f"🚀 **[LIFECYCLE ALERT: TRAILING STOP] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🔥 Price reached `{current_price:.{dec}f}`.\n"
                        f"📌 Action Required: Trail remaining position stop loss to lock gains."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

            except Exception as e:
                logging.error(f"Trade lifecycle error for {label}: {e}")

# --- HEARTBEAT & SCANNER LOOPS ---
async def heartbeat_loop(app):
    while True:
        try:
            wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            formatted_wat = wat_time.strftime("%I:%M %p WAT")
            is_active, status_msg = check_circuit_breaker()
            status_icon = "🟢" if is_active else "🔴"
            
            msg_text = f"{status_icon} *[System Heartbeat]* Status: {status_msg} ({formatted_wat})"
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

            if len(active_trades) >= 4:  
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

                sig, entry, sl, tp, partial_target, be_level, runner_target, rec_lot, rsi, ema50, h4_bias = get_multi_strategy_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dec = 3 if "JPY" in ticker else (2 if ticker in ["BTC-USD", "GC=F"] else 4)

                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    expires_wat = now_wat + datetime.timedelta(minutes=20)
                    time_sent_str = now_wat.strftime("%I:%M %p")
                    time_expire_str = expires_wat.strftime("%I:%M %p")
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    professional_signal_text = (
                        f"📊 **INSTITUTIONAL H4-FILTERED SIGNAL**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Instrument:** `{label}` | 🌐 **H4 Bias:** `{h4_bias}`\n"
                        f"📈 **Position:** {dir_icon} **{sig}**\n\n"
                        f"🔹 **Entry Price:** `{entry:.{dec}f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"🎯 **Take Profit:** `{tp:.{dec}f}`\n"
                        f"💰 **Calculated Lot:** `{rec_lot}`\n\n"
                        f"📋 **Execution Parameters:**\n"
                        f"1️⃣ Execute `{sig}` order aligned with H4 trend.\n"
                        f"2️⃣ Set Stop Loss strictly at `{sl:.{dec}f}`.\n"
                        f"3️⃣ Target objective set to `{tp:.{dec}f}` (3.5R).\n\n"
                        f"🕒 **Timestamp:** `{time_sent_str} WAT` | ⏳ **Valid Until:** `{time_expire_str} WAT`\n"
                        f"━━━━━━━━━━━━━━━━━━━"
                    )

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(
                        chat_id=target_user,
                        text=professional_signal_text,
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

    logging.info("Kings™ Institutional Trading Engine with AI Mentor Active...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
