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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Optional MetaTrader 5 Integration (Runs natively on Windows VPS)
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logging.warning("MetaTrader5 package not installed or non-Windows system. MT5 execution disabled.")

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8874815036:AAHYj9yIYbQ565mQ_szUxwaykEV7CO8ReoY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7889527038")  # Admin Personal Chat ID
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "-1003723594631")  # Kings™ Channel ID
BOT_PASSCODE = os.getenv("BOT_PASSCODE", "5051")

# DYNAMIC MT5 SESSION STORAGE
mt5_credentials = {
    "account": int(os.getenv("MT5_ACCOUNT", "0")),
    "password": os.getenv("MT5_PASSWORD", ""),
    "server": os.getenv("MT5_SERVER", ""),
    "connected": False,
    "trade_mode": "UNKNOWN"
}

# RISK & ACCOUNT SAFETY CONFIGURATION
RISK_PER_TRADE_PCT = 0.01      # Risk 1% of account balance per trade
MAX_DAILY_LOSS_PCT = 0.05       # Max 5% total account loss per day
MAX_CONSECUTIVE_LOSSES = 3      # Stop trading after 3 straight losses

# STRICT ASSET ROSTER (yfinance ticker -> Signal Display Name)
WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",             # Gold Futures
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
sent_messages = []
draft_signals = {}  # Stores clean public signal templates keyed by message_id
user_login_states = {}  # Tracks user login step-by-step state

daily_stats = {
    "date": datetime.date.today(),
    "pnl_usd": 0.0,
    "consecutive_losses": 0,
    "trading_paused": False,
    "pause_until": None
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Kings™ Multi-Strategy Auto Engine with Dynamic Risk Management is Live!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- MT5 AUTOMATED EXECUTION ENGINE ---
def init_mt5_connection(account=None, password=None, server=None):
    global mt5_credentials
    if not MT5_AVAILABLE:
        logging.warning("MT5 package is not available on this operating system.")
        return False, "MT5 Package Unavailable (Non-Windows OS)"

    if not mt5.initialize():
        err = mt5.last_error()
        logging.error(f"MT5 initialization failed: {err}")
        return False, f"Initialization Failed: {err}"

    acc = account or mt5_credentials["account"]
    pwd = password or mt5_credentials["password"]
    srv = server or mt5_credentials["server"]

    if acc > 0 and pwd and srv:
        authorized = mt5.login(acc, password=pwd, server=srv)
        if not authorized:
            err = mt5.last_error()
            logging.error(f"MT5 login failed for account {acc}: {err}")
            mt5_credentials["connected"] = False
            return False, f"Login Failed for {acc}: {err}"

        acc_info = mt5.account_info()
        mt5_credentials["account"] = acc
        mt5_credentials["password"] = pwd
        mt5_credentials["server"] = srv
        mt5_credentials["connected"] = True
        
        if acc_info:
            mt5_credentials["trade_mode"] = "DEMO" if acc_info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL"
        
        logging.info(f"Connected to MT5 successfully ({mt5_credentials['trade_mode']} Account: {acc}).")
        return True, f"Successfully connected to MT5 ({mt5_credentials['trade_mode']} Account: {acc})"
    
    return False, "No MT5 credentials supplied."

def execute_mt5_trade(symbol, action, lot, sl, tp):
    """Executes trades directly on MT5 with exact market pricing and risk rules."""
    if not MT5_AVAILABLE or not mt5.terminal_info() or not mt5_credentials["connected"]:
        logging.info(f"[SIMULATION] MT5 Order: {action} {symbol} Lot:{lot} SL:{sl} TP:{tp}")
        return True

    order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        logging.error(f"Symbol {symbol} not found in MT5.")
        return False

    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    price = mt5.symbol_info_tick(symbol).ask if action == "BUY" else mt5.symbol_info_tick(symbol).bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": 20,
        "magic": 100200,
        "comment": "Kings Auto-Engine Trade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        comment = result.comment if result else "Unknown error"
        logging.error(f"MT5 Execution Error: {comment}")
        return False

    logging.info(f"MT5 Trade Executed: Ticket #{result.order} for {symbol}")
    return True

# --- DYNAMIC RISK & LOT SIZE CALCULATOR ---
def calculate_dynamic_lot(ticker, sl_pips):
    """Calculates lot size dynamically based on 1% risk per trade."""
    account_balance = 10000.0  # Default demo base
    if MT5_AVAILABLE and mt5.terminal_info() and mt5_credentials["connected"]:
        acc_info = mt5.account_info()
        if acc_info is not None:
            account_balance = acc_info.balance

    risk_amount = account_balance * RISK_PER_TRADE_PCT
    pip_value = 10.0  # Standard lot USD per pip on majors
    if "JPY" in ticker:
        pip_value = 6.5
    elif ticker in ["GC=F", "BTC-USD"]:
        pip_value = 1.0

    if sl_pips <= 0:
        return "0.01"

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    final_lot = max(0.01, min(calculated_lot, 2.0))
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
    london_start = datetime.time(7, 0)
    london_end = datetime.time(11, 0)
    ny_start = datetime.time(12, 0)
    ny_end = datetime.time(16, 0)

    return (london_start <= now_utc <= london_end) or (ny_start <= now_utc <= ny_end)

# --- MARKET DATA FETCHING ---
def fetch_data(ticker, interval, period="7d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
        
        if df is None or df.empty:
            logging.warning(f"No data returned for {ticker} ({interval})")
            return None

        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        # Standardize column headers to single level strings
        df.columns = [str(col).capitalize() for col in df.columns]

        # Verify essential OHLC columns exist
        required_cols = ['High', 'Low', 'Close']
        if not all(col in df.columns for col in required_cols):
            logging.error(f"Missing required price columns for {ticker} ({interval})")
            return None

        # Ensure numeric Series
        high_series = pd.to_numeric(df['High'].squeeze(), errors='coerce')
        low_series = pd.to_numeric(df['Low'].squeeze(), errors='coerce')
        close_series = pd.to_numeric(df['Close'].squeeze(), errors='coerce')

        if len(close_series.dropna()) < 30:
            logging.warning(f"Insufficient data points for {ticker} ({interval})")
            return None

        # Calculate indicators safely
        df['atr14'] = ta.volatility.average_true_range(high_series, low_series, close_series, window=14)
        df['ema50'] = ta.trend.ema_indicator(close_series, window=50)
        df['ema200'] = ta.trend.ema_indicator(close_series, window=200)
        df['rsi14'] = ta.momentum.rsi(close_series, window=14)

        return df

    except Exception as e:
        logging.error(f"Error fetching/processing data for {ticker} ({interval}): {e}")
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

# --- MULTI-STRATEGY CONFLUENCE ENGINE ---
def get_multi_strategy_signal(ticker):
    if not is_in_session_killzone(ticker):
        return None, None, None, None, None, None

    h1_bias = get_h1_trend_bias(ticker)
    df_m15 = fetch_data(ticker, interval=TIMEFRAME_M15, period="5d")
    if df_m15 is None or len(df_m15) < 20:
        return None, None, None, None, None, None

    c = df_m15.iloc[-2]
    prev_c = df_m15.iloc[-3]
    
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['ema200']):
        return None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])
    ema200 = float(c['ema200'])

    recent_high = float(df_m15['High'].iloc[-15:-3].max())
    recent_low = float(df_m15['Low'].iloc[-15:-3].min())

    apa_buy = close_p > recent_high and float(prev_c['Close']) <= recent_high
    apa_sell = close_p < recent_low and float(prev_c['Close']) >= recent_low

    ema_buy = ema50 > ema200 and close_p > ema50
    ema_sell = ema50 < ema200 and close_p < ema50

    rsi_buy = rsi < 65 and rsi > 40
    rsi_sell = rsi > 35 and rsi < 60

    sig = None
    if h1_bias == "BULLISH" and (apa_buy or ema_buy) and rsi_buy:
        sig = "BUY"
    elif h1_bias == "BEARISH" and (apa_sell or ema_sell) and rsi_sell:
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None

    tp_multiplier = 1.5 if (sig == "BUY" and h1_bias == "BULLISH") or (sig == "SELL" and h1_bias == "BEARISH") else 1.2
    risk_distance = abs(close_p - (recent_low if sig == "BUY" else recent_high)) + (atr * 0.3)
    sl_pips = risk_distance * 10000 if "JPY" not in ticker else risk_distance * 100

    if sig == "BUY":
        sl = close_p - risk_distance
        tp = close_p + (risk_distance * tp_multiplier)
        be_level = close_p + (risk_distance * 0.5)
    else:
        sl = close_p + risk_distance
        tp = close_p - (risk_distance * tp_multiplier)
        be_level = close_p - (risk_distance * 0.5)

    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    return sig, close_p, sl, tp, be_level, rec_lot

# --- CIRCUIT BREAKER ---
def check_circuit_breaker():
    global daily_stats
    today = datetime.date.today()
    
    if daily_stats["date"] != today:
        daily_stats = {
            "date": today,
            "pnl_usd": 0.0,
            "consecutive_losses": 0,
            "trading_paused": False,
            "pause_until": None
        }
        return True, "Trading Active"

    if daily_stats["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        daily_stats["trading_paused"] = True
        return False, f"Paused: Hit max {MAX_CONSECUTIVE_LOSSES} consecutive losses today."

    if daily_stats["trading_paused"]:
        return False, "Trading paused by Risk Circuit Breaker."

    return True, "Trading Active"

# --- TELEGRAM COMMAND HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Passcode accepted!** Kings™ Multi-Strategy Auto Engine is active.\n\nType `/login` to connect your MT5 Broker Account.", parse_mode="Markdown")
    elif user_id in authorized_users:
        await update.message.reply_text("🟢 **Engine Active.** Send `/login` to set MT5 credentials or `/status` for bot health.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Send the passcode directly to authorize.", parse_mode="Markdown")

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 *Access Denied!* Please authenticate with `/start PASSCODE` first.", parse_mode="Markdown")
        return

    await update.message.reply_text(
        "⚙️ **MT5 Dynamic Login Setup**\n\n"
        "Please enter your credentials in this exact single-line format:\n\n"
        "`ACCOUNT_NUMBER PASSWORD SERVER`\n\n"
        "**Example:**\n`101234567 MySecretPass123 Deriv-Server`",
        parse_mode="Markdown"
    )
    user_login_states[user_id] = "WAITING_CREDENTIALS"

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 Access Denied.")
        return

    is_active, status_msg = check_circuit_breaker()
    mt5_status = "🟢 Connected" if mt5_credentials["connected"] else "🔴 Disconnected / Simulation Mode"
    account_no = mt5_credentials["account"] if mt5_credentials["account"] > 0 else "None"
    account_mode = mt5_credentials["trade_mode"]

    msg = (
        f"📊 **KINGS™ ENGINE STATUS**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **Circuit Breaker:** {status_msg}\n"
        f"🔌 **MT5 Connection:** {mt5_status}\n"
        f"👤 **Account:** `{account_no}` ({account_mode})\n"
        f"🌐 **Server:** `{mt5_credentials['server'] or 'None'}`\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message and update.message.text else ""

    if text == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text("🔓 **Passcode accepted!** Kings™ Engine is active.\n\nSend `/login` to update your MT5 account.", parse_mode="Markdown")
        return

    if user_id in authorized_users and user_login_states.get(user_id) == "WAITING_CREDENTIALS":
        parts = text.split()
        if len(parts) != 3:
            await update.message.reply_text("⚠️ **Invalid format.** Use: `ACCOUNT_NUMBER PASSWORD SERVER`\nExample: `10123456 myPass Deriv-Server`", parse_mode="Markdown")
            return

        try:
            acc_num = int(parts[0])
            pwd = parts[1]
            server = parts[2]
            
            await update.message.reply_text("🔄 Connecting to MT5 Terminal...")
            success, message = init_mt5_connection(account=acc_num, password=pwd, server=server)
            
            user_login_states.pop(user_id, None)
            if success:
                await update.message.reply_text(f"✅ **LOGIN SUCCESSFUL!**\n\n{message}", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ **LOGIN FAILED!**\n{message}\n\nCheck credentials and try `/login` again.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ Account Number must be numeric. Example: `10123456 myPass Deriv-Server`", parse_mode="Markdown")
        return

    if user_id in authorized_users:
        is_active, status_msg = check_circuit_breaker()
        await update.message.reply_text(f"🟢 **Kings™ Engine Status:** {status_msg}\nUse `/status` or `/login` to manage execution.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 *Access Denied!* Send correct passcode in direct messages.", parse_mode="Markdown")

async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    msg_id = query.message.message_id

    if data.startswith("approve_"):
        public_signal_text = draft_signals.pop(msg_id, None)
        if not public_signal_text:
            await query.edit_message_text("⚠️ **Signal session expired or unavailable.**")
            return

        try:
            posted_msg = await context.bot.send_message(
                chat_id=CHANNEL_CHAT_ID,
                text=public_signal_text,
                parse_mode="Markdown"
            )
            sent_messages.append((posted_msg.message_id, time.time()))
            original_text = query.message.text
            await query.edit_message_text(
                text=f"✅ **[POSTED TO KINGS™ CHANNEL]**\n\n{original_text}",
                reply_markup=None,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Broadcast error: {e}")
            await query.edit_message_text(f"⚠️ **BROADCAST FAILED!** Error: `{e}`", parse_mode="Markdown")

    elif data.startswith("reject_"):
        draft_signals.pop(msg_id, None)
        original_text = query.message.text
        await query.edit_message_text(
            text=f"❌ **[SIGNAL DISCARDED]** (Trade remains active on MT5)\n\n{original_text}",
            reply_markup=None,
            parse_mode="Markdown"
        )

# --- HEARTBEAT & SCANNER LOOPS ---
async def heartbeat_loop(app):
    while True:
        try:
            wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            formatted_wat = wat_time.strftime("%I:%M %p WAT")
            is_active, status_msg = check_circuit_breaker()
            status_icon = "🟢" if is_active else "🔴"
            
            msg_text = f"{status_icon} *[Bot Heartbeat]* Kings™ Engine Status: {status_msg} ({formatted_wat})"
            msg = await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg_text, parse_mode="Markdown")
            sent_messages.append((msg.message_id, time.time()))
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")
        await asyncio.sleep(3600)

async def signal_loop(app):
    global last_signals, draft_signals
    while True:
        try:
            is_active, _ = check_circuit_breaker()
            if not is_active:
                await asyncio.sleep(300)
                continue

            if await check_news_blackout():
                await asyncio.sleep(1800)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                sig, entry, sl, tp, be_level, rec_lot = get_multi_strategy_signal(ticker)

                if sig and last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dec = 3 if "JPY" in ticker else (2 if ticker in ["BTC-USD", "GC=F"] else 4)

                    now_wat = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                    expires_wat = now_wat + datetime.timedelta(minutes=15)
                    time_sent_str = now_wat.strftime("%I:%M %p")
                    time_expire_str = expires_wat.strftime("%I:%M %p")
                    dir_emoji = "🟢" if sig == "BUY" else "🔴"

                    trade_executed = execute_mt5_trade(label, sig, rec_lot, sl, tp)
                    exec_status_str = "⚡ **Executed on MT5 Account**" if trade_executed else "⚠️ **Execution Pending / Manual**"

                    public_channel_text = (
                        f"👑 **KINGS™ TRADING SIGNAL**\n\n"
                        f"📌 **Pair:** `{label}`\n"
                        f"📈 **Action:** {dir_emoji} **{sig}**\n\n"
                        f"🔹 **Entry:** `{entry:.{dec}f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"🎯 **Take Profit:** `{tp:.{dec}f}`\n\n"
                        f"🛡️ **Breakeven Level:** `{be_level:.{dec}f}`\n"
                        f"📊 **Lot Size:** `{rec_lot}`\n\n"
                        f"🕒 **Time:** `{time_sent_str} WAT` | ⏳ **Valid:** `{time_expire_str} WAT`"
                    )

                    admin_preview_text = (
                        f"📋 **NEW SIGNAL & TRADE DRAFT**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"{exec_status_str}\n\n"
                        f"{public_channel_text}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"Tap 🚀 to post signal to channel or ❌ to discard."
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton("🚀 Approve & Post", callback_data=f"approve_{label}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{label}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    sent_draft = await app.bot.send_message(
                        chat_id=target_user,
                        text=admin_preview_text,
                        reply_markup=reply_markup,
                        parse_mode="Markdown"
                    )

                    draft_signals[sent_draft.message_id] = public_channel_text

        except Exception as e:
            logging.error(f"Signal Loop error: {e}")

        await asyncio.sleep(60)

async def post_init(app):
    """Starts background loops once Telegram app initialized."""
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat_loop(app))

# --- MAIN ENTRY POINT ---
def main():
    init_mt5_connection()

    # Create explicit asyncio event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))
    app.add_handler(CallbackQueryHandler(handle_button_click))

    # Start Flask background server thread after loop initialization
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Kings™ Multi-Strategy Auto Engine Active & Running...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
