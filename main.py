import asyncio, datetime, json, logging, os, requests, pandas as pd, ta, yfinance as yf
from flask import Flask, jsonify
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
BOT_PASSCODE=os.getenv("BOT_PASSCODE")
TELEGRAM_CHANNEL_ID=os.getenv("TELEGRAM_CHANNEL_ID", TELEGRAM_CHAT_ID)
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not BOT_PASSCODE:
    raise ValueError("Missing env vars")
TIER1={"EURUSD=X":"EURUSD","GBPUSD=X":"GBPUSD","JPY=X":"USDJPY"}
BTC="BTC-USD"
MAP={"EURUSD":"EURUSD=X","GBPUSD":"GBPUSD=X","USDJPY":"JPY=X","BTCUSD":"BTC-USD"}
balance=20.0
bot_active=True
day_start=20.0
today=datetime.datetime.now(datetime.timezone.utc).date()
per_losses={"EURUSD=X":0,"GBPUSD=X":0,"JPY=X":0,"BTC-USD":0}
halted={}
firm_halted=False
firm_reason=""
milestone_sent=False
last_signals={}
authorized=set()
history=[]
def load():
    global history,balance,day_start,per_losses,halted
    if os.path.exists("history.json"):
        try:
            d=json.load(open("history.json"))
            history=d.get("history",[])
            balance=float(d.get("balance",20.0))
            day_start=float(d.get("day_start",balance))
            per_losses=d.get("per_losses",per_losses)
            halted=d.get("halted",{})
        except: pass
def save():
    json.dump({"history":history,"balance":balance,"day_start":day_start,"per_losses":per_losses,"halted":halted},open("history.json","w"))
load()
def new_day():
    global today,day_start,firm_halted,firm_reason,halted,per_losses
    nd=datetime.datetime.now(datetime.timezone.utc).date()
    if nd!=today:
        today=nd
        day_start=balance
        firm_halted=False
        firm_reason=""
        halted={}
        for k in per_losses: per_losses[k]=0
        save()
def check_firm():
    global firm_halted,firm_reason,bot_active
    new_day()
    if balance<=day_start*0.95:
        firm_halted=True
        firm_reason="FIRM KILL: -5% DAILY DRAWDOWN"
        bot_active=False
        return True
    return False
def check_milestone():
    global milestone_sent
    if balance>=80.0 and not milestone_sent:
        milestone_sent=True
        return True
    return False
def record(ticker,outcome,pnl):
    global balance
    new_day()
    balance+=pnl
    if outcome=="WIN":
        per_losses[ticker]=0
    else:
        per_losses[ticker]=per_losses.get(ticker,0)+1
        if per_losses[ticker]>=5:
            halted[ticker]=str(today)
    check_firm()
    save()
    return per_losses.get(ticker,0)
def calc_lot(ticker,sl_dist):
    risk_dollars=balance*0.01
    if sl_dist<=0: return None
    if ticker=="JPY=X":
        pips=sl_dist/0.01
    elif ticker=="BTC-USD":
        return max(round(risk_dollars/sl_dist,3),0.001)
    else:
        pips=sl_dist*10000
    rpl=pips*10.0
    if rpl<=0: return None
    return max(round(risk_dollars/rpl,2),0.01)
def fetch(ticker):
    try:
        df=yf.download(tickers=ticker,period="10d",interval="15m",progress=False,timeout=15)
        if df is None or df.empty: return None
        if isinstance(df.columns,pd.MultiIndex): df.columns=[c[0] for c in df.columns]
        df.columns=[str(c).capitalize() for c in df.columns]
        if not all(c in df.columns for c in ['High','Low','Close']): return None
        high=pd.to_numeric(df['High'].squeeze(),errors='coerce')
        low=pd.to_numeric(df['Low'].squeeze(),errors='coerce')
        close=pd.to_numeric(df['Close'].squeeze(),errors='coerce')
        if len(close.dropna())<200: return None
        df['atr14']=ta.volatility.average_true_range(high,low,close,window=14)
        df['sma50']=ta.trend.sma_indicator(close,window=50)
        df['sma200']=ta.trend.sma_indicator(close,window=200)
        try: df['adx']=ta.trend.adx(high,low,close,window=14)
        except: df['adx']=15
        df['z_score']=(close-close.rolling(50).mean())/close.rolling(50).std()
        return df
    except: return None
def get_signal(ticker):
    df=fetch(ticker)
    if df is None or len(df)<50: return None
    c=df.iloc[-2]
    close_p=float(c['Close'])
    z=float(c['z_score']) if pd.notna(c['z_score']) else 0.0
    atr=float(c['atr14']) if pd.notna(c['atr14']) else 0.0
    sma50=float(c['sma50']) if pd.notna(c['sma50']) else close_p
    sma200=float(c['sma200']) if pd.notna(c['sma200']) else close_p
    adx=float(c['adx']) if pd.notna(c['adx']) else 15
    candle=pd.to_datetime(df.index[-2])
    sig=None
    if z<-2.0: sig="BUY"
    elif z>2.0: sig="SELL"
    if not sig: return None
    if adx>25: return None
    if sig=="SELL" and close_p>sma200 and sma50>sma200: return None
    if sig=="BUY" and close_p<sma200 and sma50<sma200: return None
    if ticker=="JPY=X":
        entry=round(close_p,2); sl_dist=atr*1.5; be=round(entry+0.15,2) if sig=="BUY" else round(entry-0.15,2)
        sl=round(entry-sl_dist,2) if sig=="BUY" else round(entry+sl_dist,2)
        tp=round(entry+sl_dist*2.0,2) if sig=="BUY" else round(entry-sl_dist*2.0,2)
    elif ticker=="BTC-USD":
        entry=round(close_p,2); sl_dist=max(atr*1.5,50.0); be=round(entry+150,2) if sig=="BUY" else round(entry-150,2)
        sl=round(entry-sl_dist,2) if sig=="BUY" else round(entry+sl_dist,2)
        tp=round(entry+sl_dist*2.0,2) if sig=="BUY" else round(entry-sl_dist*2.0,2)
    else:
        entry=round(close_p,4); sl_dist=atr*1.5; be=round(entry+0.0015,4) if sig=="BUY" else round(entry-0.0015,4)
        sl=round(entry-sl_dist,4) if sig=="BUY" else round(entry+sl_dist,4)
        tp=round(entry+sl_dist*2.0,4) if sig=="BUY" else round(entry-sl_dist*2.0,4)
    score=min(abs(z)*35.0,99.0)
    return {"sig":sig,"entry":entry,"sl":sl,"tp":tp,"be":be,"sl_dist":sl_dist,"candle":candle,"score":score}
flask_app=Flask(__name__)
@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status":"ACTIVE" if bot_active else "PAUSED","balance":balance,"firm_halted":firm_halted,"halted":list(halted.keys()),"code":200}),200
def ping_loop():
    url=os.environ.get("RENDER_EXTERNAL_URL","https://signal-bot-t8ud.onrender.com/health")
    while True:
        try: requests.get(url,timeout=10)
        except: pass
        import time; time.sleep(300)
def run_flask():
    flask_app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)),use_reloader=False)
async def btn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    if q.data=="post_to_channel":
        await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID,text=q.message.text,parse_mode="Markdown")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("✅ Published.",parse_mode="Markdown")
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0]==BOT_PASSCODE:
        authorized.add(update.effective_user.id)
        tier="Tier 1 Forex" if balance<100 else "Tier 2 + BTC"
        await update.message.reply_text(f"🔓 **Aura Quantum PDF Build Active**\nBalance: `${balance:.2f}`\nUniverse: `{tier}`\nKill: `Per-Asset 5 Losses / Firm -5%`\nRisk: `1%`",parse_mode="Markdown")
    else:
        await update.message.reply_text("🔒 Denied.",parse_mode="Markdown")
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized: return
    halted_s=", ".join(halted.keys()) if halted else "None"
    per=" | ".join([f"{k}:{v}/5" for k,v in per_losses.items() if k in TIER1])
    await update.message.reply_text(f"📊 **Status:** `{'PAUSED' if not bot_active or firm_halted else 'ACTIVE 🟢'}`\nBalance: `${balance:.2f}` | Day Start: `${day_start:.2f}`\nPer-Asset: `{per}`\nHalted: `{halted_s}`\nFirm: `{firm_reason or 'ARMED'}`\nBreak-Even: `+15 Pips ON`",parse_mode="Markdown")
async def off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized: return
    bot_active=False
    await update.message.reply_text("🔴 Paused.",parse_mode="Markdown")
async def on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if update.effective_user.id not in authorized: return
    if firm_halted:
        await update.message.reply_text(f"⚠️ Firm Halt: {firm_reason}\nUse /reset",parse_mode="Markdown"); return
    bot_active=True
    await update.message.reply_text("🟢 Resumed.",parse_mode="Markdown")
async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active,firm_halted,firm_reason,halted,per_losses,day_start
    if update.effective_user.id not in authorized: return
    firm_halted=False; firm_reason=""; halted={}
    for k in per_losses: per_losses[k]=0
    day_start=balance; bot_active=True; save()
    await update.message.reply_text("🔄 Reset Done.",parse_mode="Markdown")
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global balance
    if update.effective_user.id not in authorized: return
    if context.args:
        balance=float(context.args[0]); save()
        await update.message.reply_text(f"✅ Balance: `${balance:.2f}`",parse_mode="Markdown")
    else:
        await update.message.reply_text(f"💰 Balance: `${balance:.2f}`",parse_mode="Markdown")
async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in authorized: return
    args=context.args
    ticker="EURUSD=X"; outcome=None; pnl=0
    try:
        if len(args)>=3 and args[0].upper() in MAP:
            ticker=MAP[args[0].upper()]; outcome=args[1].upper(); pnl=float(args[2])
        elif len(args)>=2:
            outcome=args[0].upper(); pnl=float(args[1])
        else:
            await update.message.reply_text("Use: /clear EURUSD LOSS 0.20",parse_mode="Markdown"); return
        cnt=record(ticker,outcome,pnl)
        history.append({"ticker":ticker,"outcome":outcome,"pnl":pnl,"ts":datetime.datetime.now(datetime.timezone.utc).isoformat()})
        save()
        msg=f"🧹 Logged: `{ticker} {outcome}` ${pnl:.2f} | Bal: `${balance:.2f}` | Losses: {cnt}/5"
        if ticker in halted: msg+=f"\n🚨 PER-ASSET HALT: {ticker} halted (5 losses). Others still trading."
        if firm_halted: msg+=f"\n🚨 FIRM KILL: {firm_reason}"
        if check_milestone(): msg+="\n🏆 MILESTONE $80: HALT & SWEEP $40"
        await update.message.reply_text(msg,parse_mode="Markdown")
    except:
        await update.message.reply_text("Use: /clear EURUSD LOSS 0.20",parse_mode="Markdown")
async def heartbeat(app):
    while True:
        await asyncio.sleep(10800)
        if bot_active and authorized:
            await app.bot.send_message(chat_id=list(authorized)[0],text=f"💓 Heartbeat | Bal: `${balance:.2f}` | Halted: `{list(halted.keys())}`",parse_mode="Markdown")
async def signal_loop(app):
    while True:
        try:
            if not bot_active or firm_halted:
                await asyncio.sleep(10); continue
            new_day()
            if check_milestone() and authorized:
                await app.bot.send_message(chat_id=list(authorized)[0],text="🏆 **MILESTONE $80**\nHALT & SWEEP $40 TO SECURE. Retain $40.",parse_mode="Markdown")
            assets=dict(TIER1)
            if balance>=100: assets[BTC]="BTCUSD"
            target=list(authorized)[0] if authorized else TELEGRAM_CHAT_ID
            for ticker,label in assets.items():
                if ticker in halted: continue
                data=get_signal(ticker)
                if not data: continue
                cstr=str(data["candle"])
                if last_signals.get(ticker)!=cstr:
                    last_signals[ticker]=cstr
                    lot=calc_lot(ticker,data["sl_dist"])
                    icon="🟢" if data["sig"]=="BUY" else "🔴"
                    text=f"⚡ **Aura Quantum [Score: {data['score']:.0f}]**\n{label} {icon} {data['sig']}\nEntry: `{data['entry']}`\nSL: `{data['sl']}` (1% Risk | 1.5x ATR)\n🛡️ **Break-Even:** `{data['be']}` (+15 Pips -> SL to Entry)\nTP: `{data['tp']}` (2.0x ATR)\nLot: `{lot}`\nTrailing: `ON`"
                    kb=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Post to Channel",callback_data="post_to_channel")]])
                    if target and authorized:
                        await app.bot.send_message(chat_id=target,text=text,parse_mode="Markdown",reply_markup=kb)
        except Exception as e:
            logging.error(f"Loop error: {e}")
            await asyncio.sleep(10)
        await asyncio.sleep(15)
async def post_init(app):
    asyncio.create_task(signal_loop(app))
    asyncio.create_task(heartbeat(app))
def main():
    loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    t_req=HTTPXRequest(connect_timeout=30.0,read_timeout=30.0)
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).request(t_req).post_init(post_init).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CommandHandler("status",status_cmd))
    app.add_handler(CommandHandler("balance",balance_cmd))
    app.add_handler(CommandHandler("off",off_cmd))
    app.add_handler(CommandHandler("on",on_cmd))
    app.add_handler(CommandHandler("reset",reset_cmd))
    app.add_handler(CommandHandler("clear",clear_cmd))
    app.add_handler(CallbackQueryHandler(btn_handler))
    Thread(target=run_flask,daemon=True).start()
    Thread(target=ping_loop,daemon=True).start()
    logging.info("Aura Quantum PDF Build Running")
    app.run_polling(drop_pending_updates=True,close_loop=False)
if __name__=="__main__":
    main()