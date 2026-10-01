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
from metaapi_cloud_sdk import MetaApi

# --- CONFIGURATION & SECURITY ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_PASSCODE = os.getenv("BOT_PASSCODE")
META_API_TOKEN = os.getenv("META_API_TOKEN") # Master API token for MetaApi

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not BOT_PASSCODE or not META_API_TOKEN:
    raise ValueError("CRITICAL SECURITY ERROR: Missing required environment variables.")

RISK_PER_TRADE_PCT = 0.015  # 1.5% risk profile
DEFAULT_ACCOUNT_BALANCE = 1000.0

WEEKDAY_ASSETS = {
    "GC=F": "XAUUSD",
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "JPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "CAD=X": "USDCAD",
    "NZDUSD=X": "NZDUSD",
    "CHF=X": "USDCHF",
    "AUDJPY=X": "AUDJPY",
    "EURGBP=X": "EURGBP",
    "BTC-USD": "BTCUSD"
}

WEEKEND_ASSETS = {
    "BTC-USD": "BTCUSD"
}

TIMEFRAME_M5 = "5m"
TIMEFRAME_H1 = "1h"

last_signals = {}
authorized_users = set()
active_trades = {}  

# --- BROKER & AUTO-TRADING STATE CONFIG ---
bot_config = {
    "account_id": os.getenv("DEFAULT_MT5_ACCOUNT_ID", ""), # Can be changed via Telegram
    "auto_execute": False,  # Toggle ON/OFF via Telegram
    "mode": "DEMO"          # DEMO or LIVE tracker
}

# --- CIRCUIT BREAKER STATE ---
daily_loss_counter = 0
last_trade_reset_date = datetime.datetime.now(datetime.timezone.utc).date()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FLASK WEB SERVER (24/7 Live Status) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Automated MT5 Execution & Telegram Control Engine is Live 24/7."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- DIRECT METAAPI EXECUTION BRIDGE ---
async def execute_broker_order(symbol, action, volume, sl, tp):
    if not bot_config["auto_execute"] or not bot_config["account_id"]:
        logging.info("Auto-execution is OFF or Account ID not set. Skipping live broker order.")
        return False, "Auto-execution disabled or missing account."

    try:
        api = MetaApi(META_API_TOKEN)
        account = await api.metatrader_account_api.get_account(bot_config["account_id"])
        
        if account.state != 'DEPLOYED':
            await account.deploy()
            
        connection = account.get_rpc_connection()
        if not connection.synchronized:
            await connection.connect()
            await connection.wait_synchronized()

        # Map Yahoo tickers to standard broker symbol formats if needed
        broker_symbol = symbol
        if symbol == "GC=F":
            broker_symbol = "XAUUSD"

        if action.upper() == "BUY":
            result = await connection.create_market_buy_order(broker_symbol, volume, sl, tp)
            logging.info(f"METAAPI SUCCESS BUY: {result}")
        elif action.upper() == "SELL":
            result = await connection.create_market_sell_order(broker_symbol, volume, sl, tp)
            logging.info(f"METAAPI SUCCESS SELL: {result}")
            
        return True, "Order executed successfully on MT5."
    except Exception as e:
        logging.error(f"MetaApi Execution Error: {e}")
        return False, str(e)

# --- DYNAMIC LOT SIZING & DATA FETCHER ---
def calculate_dynamic_lot(ticker, sl_pips, account_balance=DEFAULT_ACCOUNT_BALANCE):
    risk_amount = account_balance * RISK_PER_TRADE_PCT
    pip_value = 10.0
    if "JPY" in ticker:
        pip_value = 6.5
    elif ticker in ["GC=F", "BTC-USD"]:
        pip_value = 1.0

    if sl_pips <= 0:
        return 0.02

    calculated_lot = round(risk_amount / (sl_pips * pip_value), 2)
    return max(0.01, min(calculated_lot, 0.05 if account_balance < 500.0 else 3.0))

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

        if len(close_series.dropna()) < 30:
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
    df_h1 = fetch_data(ticker, interval=TIMEFRAME_H1, period="5d")
    if df_h1 is None or len(df_h1) < 30:
        return "NEUTRAL"
    
    latest_ema50 = float(df_h1['ema50'].iloc[-1]) if pd.notna(df_h1['ema50'].iloc[-1]) else 0.0
    latest_ema200 = float(df_h1['ema200'].iloc[-1]) if pd.notna(df_h1['ema200'].iloc[-1]) else 0.0

    if latest_ema50 > latest_ema200:
        return "BULLISH"
    elif latest_ema50 < latest_ema200:
        return "BEARISH"
    return "NEUTRAL"

def get_gold_strategy_signal(ticker):
    now = datetime.datetime.now(datetime.timezone.utc)
    if 22 <= now.hour or now.hour < 7:
        return None, None, None, None, None, None, None, None, None

    df_m5 = fetch_data(ticker, interval=TIMEFRAME_M5, period="2d")
    if df_m5 is None or len(df_m5) < 30:
        return None, None, None, None, None, None, None, None, None

    c = df_m5.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    ema50 = float(c['ema50'])

    sig = None
    if close_p > ema50 and (48 <= rsi <= 68):
        sig = "BUY"
    elif close_p < ema50 and (32 <= rsi <= 52):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m5['Close'].iloc[-1])
    spread_buffer = 1.0 
    sl_distance = max(atr * 2.5, 20.00)
    tp_distance = sl_distance * 2.0 

    if sig == "BUY":
        entry = live_price + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        partial_target = entry + (sl_distance * 1.0)
        be_level = entry + (sl_distance * 1.2)
    else:
        entry = live_price - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        partial_target = entry - (sl_distance * 1.0)
        be_level = entry - (sl_distance * 1.2)

    sl_pips = sl_distance * 1.0 
    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    
    return sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, "GOLD-STRICT"

def get_strategy_signal(ticker):
    global daily_loss_counter, last_trade_reset_date
    current_date = datetime.datetime.now(datetime.timezone.utc).date()
    if current_date != last_trade_reset_date:
        daily_loss_counter = 0
        last_trade_reset_date = current_date

    if daily_loss_counter >= 3:
        return None, None, None, None, None, None, None, None, None

    if ticker == "GC=F":
        return get_gold_strategy_signal(ticker)

    h1_bias = get_h1_trend_bias(ticker)
    df_m5 = fetch_data(ticker, interval=TIMEFRAME_M5, period="2d")
    if df_m5 is None or len(df_m5) < 30:
        return None, None, None, None, None, None, None, None, None

    c = df_m5.iloc[-2]
    if pd.isna(c['atr14']) or pd.isna(c['rsi14']) or pd.isna(c['ema50']) or pd.isna(c['ema200']):
        return None, None, None, None, None, None, None, None, None

    atr = float(c['atr14'])
    close_p = float(c['Close'])
    rsi = float(c['rsi14'])
    
    recent_high = float(df_m5['High'].iloc[-10:-2].max())
    recent_low = float(df_m5['Low'].iloc[-10:-2].min())

    spread_buffer = 0.0002 if "JPY" not in ticker and "BTC-USD" not in ticker else (0.02 if "JPY" in ticker else 30.0)

    sig = None
    if h1_bias == "BULLISH" and close_p > recent_high and (40 <= rsi <= 70):
        sig = "BUY"
    elif h1_bias == "BEARISH" and close_p < recent_low and (30 <= rsi <= 60):
        sig = "SELL"

    if not sig:
        return None, None, None, None, None, None, None, None, None

    live_price = float(df_m5['Close'].iloc[-1])
    min_broker_dist = 0.0020 if "JPY" not in ticker and "BTC-USD" not in ticker else (0.20 if "JPY" in ticker else 30.0)
    
    sl_distance = max(atr * 1.5, min_broker_dist)
    tp_distance = sl_distance * 2.0

    if sig == "BUY":
        entry = live_price + spread_buffer
        sl = entry - sl_distance
        tp = entry + tp_distance
        partial_target = entry + (sl_distance * 1.0)
        be_level = entry + (sl_distance * 1.2)
    else:
        entry = live_price - spread_buffer
        sl = entry + sl_distance
        tp = entry - tp_distance
        partial_target = entry - (sl_distance * 1.0)
        be_level = entry - (sl_distance * 1.2)

    sl_pips = sl_distance * 10000 if "JPY" not in ticker else sl_distance * 100
    rec_lot = calculate_dynamic_lot(ticker, sl_pips)
    
    return sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, h1_bias

# --- TELEGRAM INTERACTIVE COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0] == BOT_PASSCODE:
        authorized_users.add(user_id)
        await update.message.reply_text(
            "🔓 **Master Trading Engine Ready.**\n\n"
            "• Use `/autotrade on` or `/autotrade off` to toggle execution.\n"
            "• Use `/setaccount <MetaApi_Account_ID>` to switch accounts.\n"
            "• Use `/status` to view active trades and broker connection status.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🔒 *Access Denied. Invalid Passcode.*", parse_mode="Markdown")

async def autotrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    args = context.args
    if not args:
        status_str = "ON 🟢" if bot_config["auto_execute"] else "OFF 🔴"
        await update.message.reply_text(f"⚙️ Current Auto-Trade Status: **{status_str}**\nUse `/autotrade on` or `/autotrade off` to switch.", parse_mode="Markdown")
        return
    
    cmd = args[0].lower()
    if cmd == "on":
        bot_config["auto_execute"] = True
        await update.message.reply_text("🟢 **Auto-Execution ACTIVATED.** Trades will now fire directly to MT5 automatically!", parse_mode="Markdown")
    elif cmd == "off":
        bot_config["auto_execute"] = False
        await update.message.reply_text("🔴 **Auto-Execution DEACTIVATED.** Bot is now in Signal-Only mode.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: `/autotrade on` or `/autotrade off`", parse_mode="Markdown")

async def setaccount_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    args = context.args
    if not args:
        await update.message.reply_text(f"📌 Current MetaApi Account ID: `{bot_config['account_id'] or 'Not Set'}`\nUsage: `/setaccount YOUR_ACCOUNT_ID`", parse_mode="Markdown")
        return
    
    bot_config["account_id"] = args[0]
    await update.message.reply_text(f"✅ **Broker Account ID Updated Successfully!**\nTarget Account: `{bot_config['account_id']}`", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    
    auto_status = "ON 🟢" if bot_config["auto_execute"] else "OFF 🔴"
    acc_id = bot_config["account_id"] or "Not Configured"
    
    msg = (
        f"📊 **Engine & Broker Command Center:**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• Auto-Execution: **{auto_status}**\n"
        f"• MetaApi Account ID: `{acc_id}`\n"
        f"• Active Trades: **{len(active_trades)}/2 Max**\n\n"
    )
    
    if not active_trades:
        msg += "No open positions right now. Scanning markets..."
    else:
        for label, trade in active_trades.items():
            msg += f"📌 **{label}** ({trade['type']}) | Lot: `{trade['lot']}`\n"
            
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- SIGNAL & AUTO-EXECUTION SCANNER LOOP ---
async def signal_loop(app):
    global last_signals, active_trades
    while True:
        try:
            if len(active_trades) >= 2:
                await asyncio.sleep(60)
                continue

            day = datetime.datetime.now(datetime.timezone.utc).weekday()
            active_assets = WEEKDAY_ASSETS if day < 5 else WEEKEND_ASSETS

            for ticker, label in active_assets.items():
                if label in active_trades:
                    continue
                if len(active_trades) >= 2:
                    break

                sig, entry, sl, tp, partial_target, be_level, rec_lot, rsi, h1_bias = get_strategy_signal(ticker)
                if sig is None:
                    continue

                if last_signals.get(ticker) != sig:
                    last_signals[ticker] = sig
                    dec = 3 if "JPY" in ticker else (2 if ticker in ["BTC-USD", "GC=F"] else 4)
                    dir_icon = "🟢" if sig == "BUY" else "🔴"

                    # Attempt automated broker execution if enabled
                    exec_success, exec_msg = await execute_broker_order(ticker, sig, rec_lot, sl, tp)
                    exec_status_text = "🚀 **Auto-Executed on MT5!**" if exec_success else f"⚠️ *Execution skipped/failed:* {exec_msg}"

                    signal_text = (
                        f"🚨 **SIGNAL & AUTO-EXECUTION REPORT**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **Asset:** `{label}` | **Mode:** `{h1_bias}`\n"
                        f"📈 **Direction:** {dir_icon} **{sig}**\n\n"
                        f"🔹 **Entry:** `{entry:.{dec}f}`\n"
                        f"🔴 **Stop Loss:** `{sl:.{dec}f}`\n"
                        f"🎯 **Take Profit:** `{tp:.{dec}f}`\n"
                        f"⚖ **Lot Size:** `{rec_lot}`\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"{exec_status_text}"
                    )

                    target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
                    await app.bot.send_message(chat_id=target_user, text=signal_text, parse_mode="Markdown")

                    active_trades[label] = {
                        "type": sig,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "lot": rec_lot,
                        "be_level": be_level,
                        "be_hit": False,
                        "profit_locked": False
                    }
        except Exception as e:
            logging.error(f"Signal loop execution error: {e}")

        await asyncio.sleep(30)

async def post_init(app):
    asyncio.create_task(signal_loop(app))

# --- MAIN ENTRY ---
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_request).post_init(post_init).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("autotrade", autotrade_command))
    app.add_handler(CommandHandler("setaccount", setaccount_command))
    app.add_handler(CommandHandler("status", status_command))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Fully Automated MT5 Telegram Trading Engine Initialized...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
