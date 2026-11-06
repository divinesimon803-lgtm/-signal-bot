import asyncio
import datetime
import json
import logging
import os
import pandas as pd
import requests
import ta
import yfinance as yf
from flask import Flask, jsonify
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_PASSCODE = os.getenv("BOT_PASSCODE")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", TELEGRAM_CHAT_ID)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not BOT_PASSCODE:
    raise ValueError("Missing env vars")

RISK_PER_TRADE_PCT = 0.01
current_account_balance = 20.00
bot_active = True
kill_switch_active = False
kill_switch_reason = ""
day_start_balance = 20.00
consecutive_losses = 0
milestone_alert_sent = False
today_date = datetime.datetime.now(datetime.timezone.utc).date()

# --- TIER 1 ONLY: FINAL BLUEPRINT ---
# Tier 1 ($20-$100): EURUSD, GBPUSD, USDJPY only
# Tier 2 ($100+): BTCUSD unlocks
TIER1_ASSETS = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "JPY=X": "USDJPY",
}
BTC_TICKER = "BTC-USD"

last_signal_timestamps = {}
authorized_users = set()
HISTORY_FILE = "history.json"
trade_history = []

def load_data():
    global trade_history, current_account_balance, day_start_balance
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
                trade_history = data.get("history", [])
                current_account_balance = float(data.get("balance", 20.00))
                day_start_balance = float(data.get("day_start_balance", current_account_balance))
        except Exception as e:
            logging.error(f"Load error: {e}")

def save_data():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump({"history": trade_history, "balance": current_account_balance, "day_start_balance": day_start_balance, "date": today_date.isoformat()}, f)
    except Exception as e:
        logging.error(f"Save error: {e}")

load_data()

def check_new_day():
    global today_date, day_start_balance, consecutive_losses, kill_switch_active, kill_switch_reason
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    if now_date != today_date:
        today_date = now_date
        day_start_balance = current_account_balance
        consecutive_losses = 0
        kill_switch_active = False
        kill_switch_reason = ""
        save_data()

def check_kill_switch():
    global kill_switch_active, kill_switch_reason, bot_active
    check_new_day()
    # Rule B: -3% daily drawdown
    if current_account_balance <= day_start_balance * 0.97:
        kill_switch_active = True
        kill_switch_reason = "DAILY DRAWDOWN -3% HIT"
        bot_active = False
        return True
    # Rule A: 3 consecutive losses
    if consecutive_losses >= 3:
        kill_switch_active = True
        kill_switch_reason = "3 CONSECUTIVE LOSSES"
        bot_active = False
        return True
    return False

def check_milestone():
    global milestone_alert_sent
    if current_account_balance >= 80.00 and not milestone_alert_sent:
        milestone_alert_sent = True
        return True
    return False

def record_trade_outcome(outcome, pnl_amount=0.0, ticker_label=None):
    global current_account_balance, consecutive_losses
    check_new_day()
    trade_history.append({"outcome": outcome, "pnl": pnl_amount, "ticker": ticker_label, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    current_account_balance += pnl_amount
    if outcome == "WIN":
        consecutive_losses = 0
    elif outcome == "LOSS":
        consecutive_losses += 1
    save_data()

def get_metrics():
    total = len(trade_history)
    if total == 0:
        return 0.0, 0, 0, 0.0
    wins = sum(1 for t in trade_history if t.get("outcome") == "WIN")
    losses = sum(1 for t in trade_history if t.get("outcome") == "LOSS")
    win_rate = (wins / total) * 100.0
    gross_p = sum(t.get("pnl", 0) for t in trade_history if t.get("pnl", 0) > 0)
    gross_l = abs(sum(t.get("pnl", 0) for t in trade_history if t.get("pnl", 0) < 0))
    pf = (gross_p / gross_l) if gross_l > 0 else gross_p
    return win_rate, wins, losses, pf

# --- FLASK ---
flask_app = Flask(__name__)
@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return jsonify({"status": "ACTIVE" if bot_active else "PAUSED", "kill_switch": kill_switch_active, "balance": current_account_balance, "code": 200}), 200

def self_ping_loop():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "https://signal-bot-t8ud.onrender.com/health")
    while True:
        try:
            requests.get(app_url, timeout=10)
        except Exception as e:
            logging.error(f"Ping error: {e}")
        import time
        time.sleep(180)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- LOT SIZING FIXED ---
def calculate_dynamic_lot(ticker, sl_dist):
    risk_dollars = current_account_balance * RISK_PER_TRADE_PCT
    if sl_dist <= 0:
        return None
    # Correct pip values: Forex 1 lot = $10 per pip
    if ticker == "JPY=X":
        # USDJPY: 1 pip = 0.01, sl_dist in price units
        pips = sl_dist / 0.01
    elif ticker == "BTC-USD":
        pips = sl_dist  # BTC uses $1 per $1 move per 0.001 lot approx - simplified
        risk_per_lot = sl_dist * 1.0
        lot = risk_dollars / risk_per_lot if risk_per_lot > 0 else 0.01
        return max(round(lot, 3), 0.001)
    else:
        pips = sl_dist * 10000
    risk_per_lot = pips * 10.0
    if risk_per_lot <= 0:
        return None
    lot = risk_dollars / risk_per_lot
    return max(round(lot, 2), 0.01)

# --- DATA + REGIME FILTER ---
def fetch_data(ticker, interval="15m", period="10d"):
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False, timeout=15)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]
        df.columns = [str(col).capitalize() for col in df.columns]
        if not all(c in df.columns for c in ['High', 'Low', 'Close']):
            return None
        high = pd.to_numeric(df['High'].squeeze(), errors='coerce')
        low = pd.to_numeric(df['Low'].squeeze(), errors='coerce')
        close = pd.to_numeric(df['Close'].squeeze(), errors='coerce')
        if len(close.dropna()) < 200:
            return None
        df['atr14'] = ta.volatility.average_true_range(high, low, close, window=14)
        df['sma50'] = ta.trend.sma_indicator(close, window=50)
        df['sma200'] = ta.trend.sma_indicator(close, window=200)
        # Regime: ADX for trend strength
        try:
            df['adx'] = ta.trend.adx(high, low, close, window=14)
        except:
            df['adx'] = 15
        df['z_score'] = (close - close.rolling(50).mean()) / close.rolling(50).std()
        return df
    except Exception as e:
        logging.error(f"Fetch error {ticker}: {e}")
        return None

def get_strategy_signal(ticker):
    try:
        df = fetch_data(ticker)
        if df is None or len(df) < 50:
            return None, None, None, None, None, None, None, 0.0, "NO DATA"
        c = df.iloc[-2]
        close_p = float(c['Close'])
        z = float(c['z_score']) if pd.notna(c['z_score']) else 0.0
        atr = float(c['atr14']) if pd.notna(c['atr14']) else 0.0
        sma50 = float(c['sma50']) if pd.notna(c['sma50']) else close_p
        sma200 = float(c['sma200']) if pd.notna(c['sma200']) else close_p
        adx = float(c['adx']) if pd.notna(c['adx']) else 15
        candle_time = pd.to_datetime(df.index[-2])

        sig = None
        if z < -2.0:
            sig = "BUY"
        elif z > 2.0:
            sig = "SELL"
        if not sig:
            return None, None, None, None, None, None, None, 0.0, "NO SIGNAL"

        # --- REGIME FILTER: Pillar 3 Step 8 ---
        # If ADX > 25 = strong trend, pause mean reversion
        if adx > 25:
            return None, None, None, None, None, None, None, 0.0, f"TRENDING BLOCKED ADX={adx:.0f}"
        # If price is far above SMA200 and SMA50 > SMA200, don't SELL into uptrend
        if sig == "SELL" and close_p > sma200 and sma50 > sma200:
            return None, None, None, None, None, None, None, 0.0, "UPTREND BLOCKED SELL"
        if sig == "BUY" and close_p < sma200 and sma50 < sma200:
            return None, None, None, None, None, None, None, 0.0, "DOWNTREND BLOCKED BUY"

        if ticker == "JPY=X":
            entry = round(close_p, 2)
            sl_dist = atr * 1.5
            be_offset = 0.15  # 15 pips for JPY = 0.15
            if sig == "BUY":
                sl, tp, be = round(entry - sl_dist, 2), round(entry + sl_dist*2.0, 2), round(entry + be_offset, 2)
            else:
                sl, tp, be = round(entry + sl_dist, 2), round(entry - sl_dist*2.0, 2), round(entry - be_offset, 2)
        elif ticker == "BTC-USD":
            entry = round(close_p, 2)
            sl_dist = max(atr * 1.5, 50.0)
            if sig == "BUY":
                sl, tp, be = round(entry - sl_dist, 2), round(entry + sl_dist*2.0, 2), round(entry + 150, 2)
            else:
                sl, tp, be = round(entry + sl_dist, 2), round(entry - sl_dist*2.0, 2), round(entry - 150, 2)
        else:  # EURUSD, GBPUSD
            entry = round(close_p, 4)
            sl_dist = atr * 1.5
            be_offset = 0.0015
            if sig == "BUY":
                sl, tp, be = round(entry - sl_dist, 4), round(entry + sl_dist*2.0, 4), round(entry + be_offset, 4)
            else:
                sl, tp, be = round(entry + sl_dist, 4), round(entry - sl_dist*2.0, 4), round(entry - be_offset, 4)

        lot = calculate_dynamic_lot(ticker, sl_dist)
        if lot is None:
            return None, None, None, None, None, None, None, 0.0, "LOT ERROR"
        score = min(abs(z) * 35.0, 99.0)
        return sig, entry, sl, tp, be, lot, candle_time, score, "OK"
    except Exception as e:
        logging.error(f"Signal error {ticker}: {e}")
        return None, None, None, None, None, None, None, 0.0, str(e)

# --- TELEGRAM ---
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "post_to_channel":
        try:
            await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=query.message.text, parse_mode="Markdown")
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Published to Channel.", parse_mode="Markdown")
        except Exception as e:
            await query.message.reply_text(f"❌ Failed: {e}", parse_mode="Markdown")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0] == BOT_PASSCODE:
        authorized_users.add(update.effective_user.id)
        tier = "Tier 1 (Forex Only)" if current_account_balance < 100 else "Tier 2 (Forex + BTC)"
        await update.message.reply_text(f"🔓 **Aura Quantum FINAL Active**\nBalance: `${current_account_balance:.2f}`\nUniverse: `{tier}`\nKill Switch: `3 Losses / -3% Day`\nRegime Filter: `ON`", parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 Access Denied.", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users:
        return
    win_rate, wins, losses, pf = get_metrics()
    state = "ACTIVE 🟢" if bot_active and not kill_switch_active else f"PAUSED 🔴 {kill_switch_reason}"
    text = f"📊 **Aura Quantum Status:** `{state}`\n• Win Rate: `{win_rate:.1f}%` ({wins}W/{losses}L)\n• Profit Factor: `{pf:.2f}`\n• Balance: `${current_account_balance:.2f}`\n• Day Start: `${day_start_balance:.2f}`\n• Consecutive Losses: `{consecutive_losses}/3`\n• Break-Even Shield: `+15 Pips ON`\n• Kill Switch: `{'ARMED' if not kill_switch_active else 'TRIGGERED'}`"
    await update.message.reply_text(text, parse_mode="Markdown")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized_users: return
    bot_active = False
    await update.message.reply_text("🔴 Engine Paused (/off)", parse_mode="Markdown")

async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active, kill_switch_active, consecutive_losses
    if update.effective_user.id not in authorized_users: return
    # Manual reset only if you confirm - kill switch needs manual review
    if kill_switch_active:
        await update.message.reply_text(f"⚠️ Kill Switch is ACTIVE: `{kill_switch_reason}`\nUse /reset to clear after review.", parse_mode="Markdown")
        return
    bot_active = True
    await update.message.reply_text("🟢 Engine Resumed (/on)", parse_mode="Markdown")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global kill_switch_active, kill_switch_reason, consecutive_losses, bot_active, day_start_balance
    if update.effective_user.id not in authorized_users: return
    kill_switch_active = False
    kill_switch_reason = ""
    consecutive_losses = 0
    day_start_balance = current_account_balance
    bot_active = True
    save_data()
    await update.message.reply_text("🔄 Kill Switch Reset. Engine Active.", parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_account_balance
    if update.effective_user.id not in authorized_users: return
    if context.args:
        try:
            current_account_balance = float(context.args[0])
            save_data()
            await update.message.reply_text(f"✅ Balance: `${current_account_balance:.2f}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Use: /balance 100.00", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"💰 Balance: `${current_account_balance:.2f}`", parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized_users: return
    if context.args and len(context.args) >= 2 and context.args[0].upper() in ["WIN", "LOSS"]:
        try:
            pnl = float(context.args[1])
            record_trade_outcome(context.args[0].upper(), pnl_amount=pnl)
            # Check kill switch after logging
            if check_kill_switch():
                await update.message.reply_text(f"🧹 Logged: `{context.args[0].upper()}` ${pnl:.2f} | Balance: `${current_account_balance:.2f}`\n🚨 KILL SWITCH TRIGGERED: {kill_switch_reason}", parse_mode="Markdown")
            elif check_milestone():
                await update.message.reply_text(f"🧹 Logged: `{context.args[0].upper()}` ${pnl:.2f}\n🏆 MILESTONE REACHED: HALT & SWEEP 50% PROFIT ($40)", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"🧹 Logged: `{context.args[0].upper()}` ${pnl:.2f} | Balance: `${current_account_balance:.2f}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Format: /clear WIN 0.50", parse_mode="Markdown")
    else:
        await update.message.reply_text("Format: /clear WIN [pnl] or /clear LOSS [pnl]", parse_mode="Markdown")

async def heartbeat_loop(app):
    while True:
        await asyncio.sleep(10800)
        try:
            if not bot_active: continue
            win_rate, wins, losses, pf = get_metrics()
            msg = f"💓 **Aura Quantum Heartbeat**\n• Balance: `${current_account_balance:.2f}`\n• Win Rate: `{win_rate:.1f}%` ({wins}W/{losses}L)\n• Kill Switch: `ARMED`\n• Universe: `Tier 1 Forex`"
            if authorized_users:
                await app.bot.send_message(chat_id=list(authorized_users)[0], text=msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")

async def signal_loop(app):
    while True:
        try:
            if not bot_active or kill_switch_active:
                await asyncio.sleep(10)
                continue
            # Check kill switch every loop
            if check_kill_switch():
                if authorized_users:
                    await app.bot.send_message(chat_id=list(authorized_users)[0], text=f"🚨 **KILL SWITCH TRIGGERED**\nReason: `{kill_switch_reason}`\nBalance: `${current_account_balance:.2f}`\nTrading HALTED. Use /reset after review.", parse_mode="Markdown")
                await asyncio.sleep(60)
                continue
            if check_milestone():
                if authorized_users:
                    await app.bot.send_message(chat_id=list(authorized_users)[0], text="🏆 **MILESTONE REACHED: $80**\nHALT TRADING & SWEEP 50% PROFIT ($40) TO SECURE SUB-ACCOUNT.\nRetain $40 to compound.", parse_mode="Markdown")

            # Build active universe based on capital gate
            active_assets = dict(TIER1_ASSETS)
            if current_account_balance >= 100:
                active_assets[BTC_TICKER] = "BTCUSD"

            target_user = list(authorized_users)[0] if authorized_users else TELEGRAM_CHAT_ID
            for ticker, label in active_assets.items():
                sig, entry, sl, tp, be, lot, candle_time, score, reason = get_strategy_signal(ticker)
                if sig is None:
                    continue
                candle_str = str(candle_time)
                if last_signal_timestamps.get(ticker) != candle_str:
                    last_signal_timestamps[ticker] = candle_str
                    icon = "🟢" if sig == "BUY" else "🔴"
                    text = f"⚡ **Aura Quantum Signal [Score: {score:.0f}]**\n{label} {icon} {sig}\nEntry: `{entry}`\nSL: `{sl}` (1% Risk)\n🛡️ **Break-Even:** `{be}` (+15 Pips -> Move SL to Entry)\nTP: `{tp}`\nLot: `{lot}`\nKill Switch: `ARMED`"
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Post to Channel", callback_data="post_to_channel")]])
                    if target_user and authorized_users:
                        await app.bot.send_message(chat_id=target_user, text=text, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logging.error(f"Signal loop error: {e}")
            await asyncio.sleep(10)
        await asyncio.sleep(15)

async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat_loop(app))

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    t_req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_req).post_init(post_init).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("off", off_command))
    app.add_handler(CommandHandler("on", on_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CallbackQueryHandler(button_click_handler))
    Thread(target=run_flask, daemon=True).start()
    Thread(target=self_ping_loop, daemon=True).start()
    logging.info("Aura Quantum FINAL Running - Tier 1 + Kill Switch + Regime Filter")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()