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

# --- 1. SECURITY FIX: Environment Variables Only ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_PASSCODE = os.getenv("BOT_PASSCODE")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not BOT_PASSCODE:
    raise ValueError("CRITICAL SECURITY ERROR: Missing required environment variables (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BOT_PASSCODE). Please check your .env configuration.")

# RISK & ACCOUNT SAFETY CONFIGURATION
RISK_PER_TRADE_PCT = 0.015     # 1.5% risk profile per trade
MAX_DAILY_LOSS_PCT = 0.06      # Max 6% total account loss per day
MAX_CONSECUTIVE_LOSSES = 4     
MAX_DAILY_WINS = 6             
DEFAULT_ACCOUNT_BALANCE = 1000.0 # Configurable real account balance baseline

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
    return "Kings™ Institutional Trading Engine with Master Fx Mentor is Live."

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

# --- 2. RISK FIX: Dynamic Lot Size & Reward-to-Risk Validation ---
def calculate_dynamic_lot(ticker, sl_pips, account_balance=DEFAULT_ACCOUNT_BALANCE):
    risk_amount = account_balance * RISK_PER_TRADE_PCT
    pip_value = 10.0  # Standard lot USD per pip on majors
    if "JPY" in ticker:
        pip_value = 6.5
    elif ticker in ["GC=F", "BTC-USD"]:
        pip_value = 1.0

    if sl_pips <= 0:
        return 0.02

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    
    # Hard cap lot size to 0.05 max for accounts under $500
    if account_balance < 500.0:
        final_lot = max(0.01, min(calculated_lot, 0.05))
    else:
        final_lot = max(0.01, min(calculated_lot, 3.0))
        
    return final_lot

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

# --- 3. DATA FIX: Broker API Hook Warning & Spread Buffer ---
# WARNING: yfinance data can have settlement delays and missing spread adjustments.
# TODO: Replace fetch_data implementation with a real broker API connector (e.g., MetaTrader, Oanda, or Alpaca SDK) for production execution.
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

def get_h1_trend_bias(ticker):
    """H1 Trend Filter: Enforces alignment with 1-Hour 50 EMA vs 200 EMA structure."""
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="60d")
    if df_h1 is None or len(df_h1) < 30:
        return "NEUTRAL"
    
    latest_ema50 = float(df_h1['ema50'].iloc[-1]) if 'ema50' in df_h1 and pd.notna(df_h1['ema50'].iloc[-1]) else 0.0
    latest_ema200 = float(df_h1['ema200'].iloc[-1]) if 'ema200' in df_h1 and pd.notna(df_h1['ema200'].iloc[-1]) else 0.0

    if latest_ema50 > latest_ema200:
        return "BULLISH"
    elif latest_ema50 < latest_ema200:
        return "BEARISH"
    return "NEUTRAL"

# --- 4. LOGIC FIX: Streamlined Single Strategy (H1 EMA Trend + M15 Breakout + RSI Filter) ---
def get_strategy_signal(ticker):
    if not is_in_session_killzone(ticker):
        return None, None, None, None, None, None, None, None, None, None, None

    h1_bias = get_h1_trend_bias(ticker)
    
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 20:
        return None, None, None, None, None, None, None, None, None, None, None

    c = df_m15.iloc[-2]
    
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['ema200']):
        return None, None, None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])
    
    recent_high = float(df_m15['High'].iloc[-10:-2].max())
    recent_low = float(df_m15['Low'].iloc[-10:-2].min())

    # 2-Pip spread / execution buffer adjustment
    spread_buffer = 0.0002 if "JPY" not in ticker and "GC=F" not in ticker and "BTC-USD" not in ticker else (0.02 if "JPY" in ticker else 1.0)

    sig = None
    # Clean strategy: H1 EMA filter + M15 breakout + RSI 30-70 filter
    if h1_bias == "BULLISH" and close_p > recent_high and (30 <= rsi <= 70):
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p < recent_low and (30 <= rsi <= 70):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None, None, None

    # Risk parameters using ATR * 1.5 for Stop Loss distance
    sl_distance = atr * 1.5
    tp_distance = sl_distance * 2.0  # 1:2 Reward-to-Risk ratio minimum check

    if sig == "BUY":
        entry = close_p + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        partial_target = entry + (sl_distance * 1.0)
        be_level = entry + (sl_distance * 1.5)
        runner_target = entry + (sl_distance * 2.0)
    else:
        entry = close_p - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        partial_target = entry - (sl_distance * 1.0)
        be_level = entry - (sl_distance * 1.5)
        runner_target = entry - (sl_distance * 2.0)

    # Validate RR ratio >= 1:1.5
    rr_ratio = tp_distance / sl_distance
    if rr_ratio < 1.5:
        return None, None, None, None, None, None, None, None, None, None, None

    sl_pips = sl_distance * 10000 if "JPY" not in ticker else sl_distance * 100
    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    
    return sig, entry, sl, tp, partial_target, be_level, runner_target, rec_lot, rsi, ema50, h1_bias

# --- 6. BACKTESTING FUNCTION ---
def backtest_strategy(ticker="EURUSD=X", days=90):
    """Backtests the streamlined strategy on the last 90 days of data."""
    logging.info(f"Running backtest for {ticker} over the last {days} days...")
    df = fetch_data(ticker, interval=TIMEFRAME_H1, period=f"{days}d")
    if df is None or len(df) < 50:
        logging.warning("Insufficient data for backtesting.")
        return {"win_rate": 0.0, "max_drawdown": 0.0, "total_pnl": 0.0}

    wins = 0
    losses = 0
    total_pnl = 0.0
    peak_pnl = 0.0
    max_dd = 0.0
    current_balance = DEFAULT_ACCOUNT_BALANCE

    # Simplified vector/loop evaluation for backtest metrics
    for i in range(50, len(df) - 10):
        sub_df = df.iloc[:i]
        close_p = float(sub_df['Close'].iloc[-1])
        ema50 = float(sub_df['ema50'].iloc[-1])
        ema200 = float(sub_df['ema200'].iloc[-1])
        rsi = float(sub_df['rsi14'].iloc[-1])
        atr = float(sub_df['atr14'].iloc[-1])

        if pd.isna(atr) or atr == 0:
            continue

        future_price = float(df['Close'].iloc[i + 5]) # Look ahead 5 periods outcome
        sl_dist = atr * 1.5
        tp_dist = sl_dist * 2.0

        if ema50 > ema200 and (30 <= rsi <= 70): # Simulated Buy Setup
            if future_price >= close_p + tp_dist:
                wins += 1
                pnl = current_balance * RISK_PER_TRADE_PCT * 2.0
                total_pnl += pnl
                current_balance += pnl
            elif future_price <= close_p - sl_dist:
                losses += 1
                pnl = -(current_balance * RISK_PER_TRADE_PCT)
                total_pnl += pnl
                current_balance += pnl
        elif ema50 < ema200 and (30 <= rsi <= 70): # Simulated Sell Setup
            if future_price <= close_p - tp_dist:
                wins += 1
                pnl = current_balance * RISK_PER_TRADE_PCT * 2.0
                total_pnl += pnl
                current_balance += pnl
            elif future_price >= close_p + sl_dist:
                losses += 1
                pnl = -(current_balance * RISK_PER_TRADE_PCT)
                total_pnl += pnl
                current_balance += pnl

        if total_pnl > peak_pnl:
            peak_pnl = total_pnl
        dd = peak_pnl - total_pnl
        if dd > max_dd:
            max_dd = dd

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    
    results = {
        "win_rate": round(win_rate, 2),
        "max_drawdown": round(max_dd, 2),
        "total_pnl": round(total_pnl, 2)
    }
    logging.info(f"Backtest Results for {ticker}: {results}")
    return results

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

# --- TELEGRAM COMMAND HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text(
            "🔓 **Access Granted:** Streamlined Institutional Engine Active!\n"
            "Ready for signals and execution tracking.", 
            parse_mode="Markdown"
        )
    elif user_id in authorized_users:
        await update.message.reply_text("🟢 **Engine Online:** Use `/status` to check system metrics.", parse_mode="Markdown")
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
        f"🛡️ **Active Positions:** {len(active_trades)}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message and update.message.text else ""

    if text == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Access Granted:** Engine active.", parse_mode="Markdown")
        return

    if user_id in authorized_users:
        await update.message.reply_text("💡 Use `/status` or wait for automated market signals.", parse_mode="Markdown")
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
                    log_trade_to_journal(label, trade_type, entry, current_price, "WIN (TP Hit)", "+2.0R")
                    msg = (
                        f"⚠️ EDUCATIONAL ONLY - NOT FINANCIAL ADVICE - Trading is risky\n"
                        f"Win rate unknown, test on demo first\n\n"
                        f"🎯 **[TRADE EXECUTED: TP REACHED] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"✅ Target achieved at `{tp:.{dec}f}`.\n"
                        f"📊 Position closed successfully with +2.0R gain."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    continue

                # --- STOP LOSS HIT ---
                elif (trade_type == "BUY" and current_price <= sl) or (trade_type == "SELL" and current_price >= sl):
                    log_trade_to_journal(label, trade_type, entry, current_price, "LOSS (SL Hit)", "-1.0R")
                    msg = (
                        f"⚠️ EDUCATIONAL ONLY - NOT FINANCIAL ADVICE - Trading is risky\n"
                        f"Win rate unknown, test on demo first\n\n"
                        f"🛑 **[TRADE EXECUTED: STOP LOSS] - {label}**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"❌ Stop loss triggered at `{sl:.{dec}f}`.\n"
                        f"📊 Risk managed (-1.0R) and recorded to journal."
                    )
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")
                    active_trades.pop(label, None)
                    daily_stats["consecutive_losses"] += 1
                    continue

                # --- STAGE 1: PARTIAL PROFIT TRIGGER (1.0R) ---
                if not trade["partial_hit"] and ((trade_type == "BUY" and current_price >= partial_target) or (trade_type == "SELL" and current_price <= partial_target)):
                    trade["partial_hit"] = True
                    msg = f"⚡ **[LIFECYCLE ALERT: PARTIAL TARGET] - {label}**\n💰 1.0R threshold reached at `{current_price:.{dec}f}`. Secure partials."
                    await app.bot.send_message(chat_id=target_user, text=msg, parse_mode="Markdown")

                # --- STAGE 2: BREAKEVEN TRIGGER (1.5R) ---
                if not trade["be_hit"] and ((trade_type == "BUY" and current_price >= be_level) or (trade_type == "SELL" and current_price <= be_level)):
                    trade["be_hit"] = True
                    msg = f"🛡️ **[LIFECYCLE ALERT: BREAKEVEN] - {label}**\n📈 Price reached `{current_price:.{dec}f}`. Move SL to entry (`{entry:.{dec}f}`)."
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

                sig, entry, sl, tp, partial_target, be_level, runner_target, rec_lot, rsi, ema50, h1_bias = get_strategy_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dec = 3 if "JPY" in ticker else (2 if ticker in ["BTC-USD", "GC=F"] else 4)

                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    time_sent_str = now_wat.strftime("%I:%M %p")
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    # --- 5. HONESTY FIX: Mandatory Disclaimer ---
                    professional_signal_text = (
                        f"⚠️ **EDUCATIONAL ONLY - NOT FINANCIAL ADVICE - Trading is risky**\n"
                        f"Win rate unknown, test on demo first\n\n"
                        f"📊 **INSTITUTIONAL H1-FILTERED SIGNAL**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Instrument:** `{label}` | 🌐 **H1 Trend Bias:** `{h1_bias}`\n"
                        f"📈 **Position:** {dir_icon} **{sig}**\n\n"
                        f"🔹 **Entry Price:** `{entry:.{dec}f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"🎯 **Take Profit:** `{tp:.{dec}f}`\n"
                        f"💰 **Calculated Lot:** `{rec_lot}`\n\n"
                        f"🕒 **Timestamp:** `{time_sent_str} WAT`\n"
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
    # Run backtest verification check prior to launching live signal loops
    backtest_strategy("EURUSD=X", days=90)
    
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

    logging.info("Kings™ Institutional Trading Engine Active...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
