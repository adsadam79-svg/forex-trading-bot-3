import os
import json
import time
import requests
import threading
import base64
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
PORT = int(os.environ.get("PORT", 8080))

PAIRS = ["USD/JPY", "AUD/USD"]
TIMEFRAMES = ["15min", "1h", "4h"]
OPPORTUNITIES_FILE = "opportunities.json"

PAIR_CURRENCIES = {
    "USD/JPY": ["USD", "JPY"],
    "AUD/USD": ["AUD", "USD"],
}

SWING_LOOKBACK = 3          # 3 شمعات يمين + 3 يسار
PULLBACK_MAX_CANDLES = 6    # حد أقصى ديال الشموع لانتظار الـ Pullback
BOS_MAX_CANDLES = 10        # حد أقصى ديال الشموع لانتظار BOS بعد Sweep
SWEEP_ATR_MULTIPLIER = 0.15
RECENT_CHECK_CANDLES = 3    # كنشيكو آخر 3 شموع (ماشي غير آخر وحدة) لـ Sweep/Candle confirmation
PULLBACK_TOUCH_ATR = 0.3    # قرب كافي من BOS level باش نعتبروه "لمس" (touch)

# حالة التريدات المنتظرة للتأكيد — dict بالـ pair كـ key
pending_trades = {}        # {"USD/JPY": trade_dict, ...}
waiting_confirmation = {}  # {"USD/JPY": True/False, ...}

# state machine لكل pair+timeframe: كيتبع فين وصلنا فالتسلسل
# {"USD/JPY_15min": {"stage": "waiting_sweep"/"waiting_bos"/"waiting_pullback"/"waiting_candle",
#                     "direction": "BUY"/"SELL", "swing_level": float, "bos_level": float,
#                     "candles_since_bos": int}}
sequence_state = {}

# Cache ديال البيانات باش ما نطلبوش أكثر من مرة
data_cache = {}

def fetch_all_data():
    """كيجيب بيانات كل الأزواج مرة واحدة ويحفظها فالـ cache"""
    global data_cache
    data_cache = {}
    for pair in PAIRS:
        data_cache[pair] = {}
        for tf in ["15min", "1h", "4h"]:
            result = get_price_data(pair, tf)
            data_cache[pair][tf] = result

def get_cached_data(pair, interval):
    """كيرجع البيانات من الـ cache"""
    return data_cache.get(pair, {}).get(interval, None)

def send_telegram(msg, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload)

def send_with_buttons(msg, trade):
    pair_key = trade["pair"].replace("/", "")  # "USD/JPY" → "USDJPY"
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ نعم، دخلها!", "callback_data": f"yes_{pair_key}"},
            {"text": "❌ لا، تجاوزها", "callback_data": f"no_{pair_key}"}
        ]]
    }
    send_telegram(msg, reply_markup=keyboard)

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id})

def set_webhook():
    # امسح الـ webhook القديم أولاً
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
    time.sleep(2)
    # سجل الجديد
    webhook_url = "https://forex-trading-bot-2-production.up.railway.app/webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    r = requests.post(url, json={"url": webhook_url})
    print(f"Webhook set: {r.json()}")

def get_high_impact_news(pair):
    try:
        currencies = PAIR_CURRENCIES.get(pair, [])
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        danger_events = []
        warning_events = []
        for event in events:
            if event.get("impact") != "High":
                continue
            if event.get("currency") not in currencies:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            diff_minutes = (event_time - now).total_seconds() / 60
            if -30 <= diff_minutes <= 120:
                danger_events.append(event["title"])
            elif 120 < diff_minutes <= 480:
                warning_events.append(event["title"])
        return danger_events, warning_events
    except:
        return [], []

def get_market_summary(pair):
    """كيجيب ملخص تحركات السوق ديال اليوم"""
    try:
        result_1h = get_cached_data(pair, "1h") or get_price_data(pair, "1h", 24)
        result_15 = get_cached_data(pair, "15min") or get_price_data(pair, "15min", 8)
        if not result_1h or not result_15:
            return None

        closes_1h = result_1h[0]
        closes_15 = result_15[0]

        # تحرك اليوم
        open_price = closes_1h[0]
        current = closes_1h[-1]
        change = round(current - open_price, 6)
        change_pct = round((change / open_price) * 100, 3)
        direction_emoji = "📈" if change > 0 else "📉"

        # أعلى وأدنى اليوم
        highs_1h = result_1h[1]
        lows_1h = result_1h[2]
        high_day = round(max(highs_1h), 6)
        low_day = round(min(lows_1h), 6)

        # تحرك آخر ساعة
        last_hour_change = round(closes_15[-1] - closes_15[0], 6)
        last_hour_emoji = "⬆️" if last_hour_change > 0 else "⬇️"

        return {
            "change": change,
            "change_pct": change_pct,
            "direction_emoji": direction_emoji,
            "high_day": high_day,
            "low_day": low_day,
            "last_hour_change": last_hour_change,
            "last_hour_emoji": last_hour_emoji,
            "current": current
        }
    except:
        return None

def get_news_summary(pair):
    """كيجيب ملخص الأخبار ديال اليوم"""
    try:
        currencies = PAIR_CURRENCIES.get(pair, [])
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        today_news = []
        for event in events:
            if event.get("impact") not in ["High", "Medium"]:
                continue
            if event.get("currency") not in currencies:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            if event_time.strftime("%Y-%m-%d") == today:
                impact_emoji = "🔴" if event.get("impact") == "High" else "🟡"
                diff = (event_time - now).total_seconds() / 60
                if diff < -60:
                    status = "مرات"
                elif diff < 0:
                    status = "داز دابا"
                else:
                    status = f"بعد {int(diff)} دقيقة"
                today_news.append(f"{impact_emoji} {event['title']} ({status})")
        return today_news
    except:
        return []

price_cache = {}

CACHE_SECONDS = {
    "15min": 900,
    "1h": 3600,
    "4h": 14400
}

def get_price_data(pair, interval="15min", outputsize=250):
    """كترجع (closes, highs, lows, opens) — زدنا opens باش نقدرو نحسبو Candlestick Confirmation"""
    global price_cache

    cache_key = f"{pair}_{interval}"
    now_ts = time.time()

    if cache_key in price_cache:
        cached_time = price_cache[cache_key]["time"]

        if now_ts - cached_time < CACHE_SECONDS.get(interval, 900):
            return price_cache[cache_key]["data"]

    params = {
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY
    }

    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params=params,
            timeout=15
        )

        data = r.json()

        if "values" not in data:
            print(
                f"API Error {pair} {interval}: "
                f"{data.get('message', data.get('code', 'unknown'))}"
            )
            return None

        closes = [float(v["close"]) for v in reversed(data["values"])]
        highs = [float(v["high"]) for v in reversed(data["values"])]
        lows = [float(v["low"]) for v in reversed(data["values"])]
        opens = [float(v["open"]) for v in reversed(data["values"])]

        result = (closes, highs, lows, opens)

        price_cache[cache_key] = {
            "time": now_ts,
            "data": result
        }

        return result

    except Exception as e:
        print(f"Price API Error {pair} {interval}: {e}")
        return None

def calc_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return round(sum(trs[-period:]) / period, 6)


def get_swing_points(highs, lows):
    """
    Swing High/Low بـ 3 شمعات يمين + 3 يسار (SWING_LOOKBACK).
    كترجع لائحة ديال (index, price, type) — type = "high" أو "low"
    """
    swings = []
    n = len(highs)
    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        window_highs = highs[i - SWING_LOOKBACK: i + SWING_LOOKBACK + 1]
        window_lows = lows[i - SWING_LOOKBACK: i + SWING_LOOKBACK + 1]

        if highs[i] == max(window_highs) and window_highs.count(highs[i]) == 1:
            swings.append((i, highs[i], "high"))

        if lows[i] == min(window_lows) and window_lows.count(lows[i]) == 1:
            swings.append((i, lows[i], "low"))

    return swings


def get_last_swing(swings, swing_type, before_index=None):
    """كيرجع آخر Swing High/Low قبل index معينة (أو آخر واحد فالمجموعة)"""
    filtered = [s for s in swings if s[2] == swing_type]
    if before_index is not None:
        filtered = [s for s in filtered if s[0] < before_index]
    if not filtered:
        return None
    return filtered[-1]  # آخر واحد (الأقرب للحاضر)


# --- EXPERT SMC LOGIC (NEW) ---
def detect_smc_setup_expert(closes, highs, lows, opens):
    n = len(closes)
    if n < 20: return None, {}
    last_high, last_low = max(highs[-15:-3]), min(lows[-15:-3])
    curr_c, curr_h, curr_l = closes[-1], highs[-1], lows[-1]
    setup, details = None, {"sweep": False, "bos": False, "fvg": False}
    
    if curr_l < last_low and curr_c > last_low: details["sweep"] = True
    if details["sweep"] and curr_c > max(highs[-6:-1]):
        details["bos"], setup = True, "BUY"
    if setup == "BUY" and lows[-1] > highs[-3]: details["fvg"] = True
    
    if curr_h > last_high and curr_c < last_high: details["sweep"] = True
    if details["sweep"] and curr_c < min(lows[-6:-1]):
        details["bos"], setup = True, "SELL"
    if setup == "SELL" and highs[-1] < lows[-3]: details["fvg"] = True
    return setup, details

def get_trend_expert(data):
    if not data: return "NEUTRAL"
    return "BUY" if data[0][-1] > data[0][-20] else ("SELL" if data[0][-1] < data[0][-20] else "NEUTRAL")

def is_killzone_expert():
    now_h = datetime.now(timezone.utc).hour
    return 7 <= now_h <= 17

def analyze_pair(pair):
    d15, d1h, d4h = get_cached_data(pair, "15min"), get_cached_data(pair, "1h"), get_cached_data(pair, "4h")
    if not d15 or not d1h or not d4h: return None
    setup_15, details = detect_smc_setup_expert(d15[0], d15[1], d15[2], d15[3])
    if not setup_15: return None
    t1h, t4h = get_trend_expert(d1h), get_trend_expert(t4h)
    
    stars = "⭐⭐⭐ GOLD" if setup_15 == t1h == t4h else ("⭐⭐ SILVER" if setup_15 == t1h else "⭐ BRONZE")
    price, is_jpy = d15[0][-1], pair.endswith("JPY")
    tp_dist = min(abs((max(d15[1][-12:-1]) if setup_15=="BUY" else min(d15[2][-12:-1])) - price), 0.220 if is_jpy else 0.00220)
    tp_dist = max(tp_dist, 0.050 if is_jpy else 0.00050)
    sl_dist = tp_dist / 1.5
    
    tp = round(price + tp_dist if setup_15=="BUY" else price - tp_dist, 5 if not is_jpy else 3)
    sl = round(price - sl_dist if setup_15=="BUY" else price + sl_dist, 5 if not is_jpy else 3)
    return {"pair": pair, "direction": setup_15, "stars": stars, "price": price, "tp": tp, "sl": sl, 
            "tp_pts": round(tp_dist*(100 if is_jpy else 100000)), "details": details, "kz": is_killzone_expert(), "t": {"1h": t1h, "4h": t4h}}

def pull_from_github():
    if not GH_TOKEN or not GITHUB_REPO:
        return []
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{OPPORTUNITIES_FILE}"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return []
    content = base64.b64decode(r.json()["content"]).decode()
    try:
        return json.loads(content)
    except:
        return []

def push_to_github(opportunities):
    if not GH_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{OPPORTUNITIES_FILE}"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(url, headers=headers)
    sha = r.json().get("sha", "") if r.status_code == 200 else ""
    content = json.dumps(opportunities, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    payload = {"message": "update opportunities", "content": encoded, "sha": sha}
    requests.put(url, headers=headers, json=payload)

def monitor_trade(trade):
    global waiting_confirmation, pending_trades
    pair = trade["pair"]

    for i in range(3):
        time.sleep(600)  # كل 10 دقائق
        if not waiting_confirmation.get(pair):
            return

        result = get_price_data(pair)
        if not result:
            continue
        closes = result[0]
        current_price = closes[-1]

        if "BUY" in trade["direction"]:
            progress = "📈 السوق ماشي فالاتجاه الصح" if current_price > trade["price"] else "⚠️ السوق راجع شوية"
        else:
            progress = "📈 السوق ماشي فالاتجاه الصح" if current_price < trade["price"] else "⚠️ السوق راجع شوية"

        remaining = 20 - (i + 1) * 10
        send_telegram(
            f"🔄 <b>تحديث — {pair}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{progress}\n"
            f"💰 السعر دابا: <b>{current_price}</b>\n"
            f"⏳ باقي: <b>{remaining} دقيقة</b>\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

    if waiting_confirmation.get(pair):
        result = get_price_data(pair)
        current_price = result[0][-1] if result else trade["price"]
        send_telegram(
            f"🎯 <b>وقت الدخول — {pair}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"الإشارة باقية قوية ✅\n"
            f"💰 السعر دابا: <b>{current_price}</b>\n"
            f"🎯 TP: <b>{trade['tp']}</b>\n"
            f"🛑 SL: <b>{trade['sl']}</b>\n"
            f"⚖️ R/R: <b>1:{trade['rr']}</b>\n\n"
            f"واش واجد تدخل؟ 🚀\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
    waiting_confirmation[pair] = False
    pending_trades.pop(pair, None)

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_POST(self):
        global waiting_confirmation, pending_trades
        content_length = int(self.headers['Content-Length'])
        body = self.rfile.read(content_length)
        self.send_response(200)
        self.end_headers()

        try:
            update = json.loads(body)

            if "callback_query" in update:
                cb = update["callback_query"]
                data = cb.get("data", "")
                answer_callback(cb["id"])

                if "_" in data:
                    action, pair_key = data.split("_", 1)
                    pair = next((p for p in pending_trades if p.replace("/", "") == pair_key), None)
                else:
                    action, pair = data, None

                if action == "yes" and pair and pair in pending_trades:
                    waiting_confirmation[pair] = True
                    trade = pending_trades[pair].copy()
                    send_telegram(
                        f"✅ <b>واخا! غادي نراقب التريد 30 دقيقة</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"غادي نبعت ليك تحديث كل 10 دقائق 👀\n"
                        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                    )
                    t = threading.Thread(target=monitor_trade, args=(trade,))
                    t.daemon = True
                    t.start()

                elif action == "no" and pair:
                    pending_trades.pop(pair, None)
                    waiting_confirmation[pair] = False
                    send_telegram("❌ واخا، تجاوزنا هاد التريد. غادي نكملو نراقبو السوق 👀")

        except Exception as e:
            print(f"Webhook error: {e}")

    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"Server running on port {PORT}")
    server.serve_forever()


def get_debug_report(pair):
    """Debug report — كيبين فين وصل الـ state machine لكل timeframe"""
    lines = [f"🔍 {pair}"]

    for tf in TIMEFRAMES:
        tf_label = {"15min": "15min", "1h": "1H", "4h": "4H"}.get(tf, tf)
        lines.append(f"\n━━━━━━━━")
        lines.append(tf_label)

        state_key = f"{pair}_{tf}"
        state = sequence_state.get(state_key, {"stage": "waiting_sweep"})
        stage = state.get("stage", "waiting_sweep")

        stage_labels = {
            "waiting_sweep": "🔎 كنبحثو على Liquidity Sweep",
            "waiting_bos": "✅ Sweep وقع — كنستناو BOS",
            "waiting_pullback": "✅ BOS وقع — كنستناو Pullback",
            "waiting_candle": "✅ Pullback وقع — كنستناو Candlestick Confirmation",
        }
        lines.append(stage_labels.get(stage, stage))

        if stage != "waiting_sweep":
            direction = state.get("direction", "?")
            lines.append(f"الاتجاه: {direction}")

        if stage == "waiting_bos":
            swing_level = state.get("swing_level")
            candles = state.get("candles_since_sweep", 0)
            lines.append(f"Swing Level (Sweep): {swing_level}")
            lines.append(f"شموع منذ Sweep: {candles}/{BOS_MAX_CANDLES}")

        if stage == "waiting_pullback":
            bos_level = state.get("bos_level")
            touched = state.get("touched_bos", False)
            candles = state.get("candles_since_bos", 0)
            lines.append(f"BOS Level: {bos_level}")
            lines.append(f"لمس BOS zone: {'✅' if touched else '❌ لسا ماوصلش'}")
            lines.append(f"شموع منذ BOS: {candles}/{PULLBACK_MAX_CANDLES}")

        if stage == "waiting_candle":
            bos_level = state.get("bos_level")
            candles = state.get("candles_since_bos", 0)
            lines.append(f"BOS Level: {bos_level}")
            lines.append(f"كنستناو شمعة تأكيد — محاولات: {candles}/{PULLBACK_MAX_CANDLES + RECENT_CHECK_CANDLES}")

    return "\n".join(lines)


def send_hourly_report(pairs_status):
    """كيبعت تقرير ساعي — رسالة debug منفصلة لكل pair"""
    for pair in pairs_status:
        send_telegram(get_debug_report(pair))

def main_loop():
    global pending_trades, waiting_confirmation
    time.sleep(5)
    set_webhook()

    opportunities = pull_from_github()
    last_report_hour = -1
    last_signal = {}
    # last_signal[pair] = {"direction": "BUY", "bos_level": float}
    # إشارة جديدة تتبعت فقط إذا تغير الاتجاه أو تبدل مستوى BOS (يعني Sweep+BOS جدد بالكامل)

    while True:
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%H:%M UTC")

        try:
            if now.hour == 21 and now.minute < 15:
                today = now.strftime("%Y-%m-%d")
                today_ops = [o for o in opportunities if o.get("date", "").startswith(today)]

                if not today_ops:
                    send_telegram(
                        f"📊 <b>التقرير اليومي — {today}</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"ما كانت كاينة حتى فرصة اليوم\n"
                        f"🕐 {now_str}"
                    )
                else:
                    msg = f"📊 <b>التقرير اليومي — {today}</b>\n━━━━━━━━━━━━━━━━\n"
                    msg += f"📈 عدد الفرص: <b>{len(today_ops)}</b>\n\n"
                    for i, op in enumerate(today_ops, 1):
                        status = "🚫 ملغاة (news)" if op.get("cancelled") else "✅ أُرسلت"
                        msg += (
                            f"<b>{i}. {op['pair']}</b> — {op['direction']}\n"
                            f"   💰 {op['price']} | 🎯 {op['tp']} | 🛑 {op['sl']}\n"
                            f"   ⏱ {op['time']} | {status}\n\n"
                        )
                    msg += "━━━━━━━━━━━━━━━━\n⚠️ هاد المعلومات للتعلم فقط"
                    send_telegram(msg)

                time.sleep(900)
                continue

            fetch_all_data()

            # تقرير كل ساعة
            if now.hour != last_report_hour and now.minute < 15 and not any(waiting_confirmation.values()):
                last_report_hour = now.hour
                pairs_status = {pair: {} for pair in PAIRS}
                send_hourly_report(pairs_status)

            for pair in PAIRS:
                    if waiting_confirmation.get(pair):
                        continue

                    trade = analyze_pair(pair)
                    if not trade:
                        continue
                    
                    signal_key = f"{pair}_{trade['direction']}_{now.hour}"
                    if signal_key in last_signal:
                        continue
                    
                    danger_news, warning_news = get_high_impact_news(pair)
                    
                    # S-ajel l-fursa f l-list dyal GitHub (opportunities)
                    op = {
                        "date": now.strftime("%Y-%m-%d %H:%M"),
                        "time": now_str,
                        "pair": pair,
                        "direction": trade["direction"],
                        "price": trade["price"],
                        "tp": trade["tp"],
                        "sl": trade["sl"],
                        "rr": 1.5,
                        "strength": trade["stars"],
                        "cancelled": bool(danger_news)
                    }
                    opportunities.append(op)
                    push_to_github(opportunities)

                    if danger_news:
                        continue
                        
                    kz_icon = "✅" if trade["kz"] else "⚠️"
                    msg = (f"🔔 <b>فرصة — {trade['pair']}</b>\n"
                           f"Strength: <b>{trade['stars']}</b>\n"
                           f"Direction: <b>{trade['direction']}</b>\n"
                           f"Killzone: {kz_icon}\n"
                           f"Confirmations: Sweep {'✅' if trade['details']['sweep'] else '❌'} | BOS {'✅' if trade['details']['bos'] else '❌'} | FVG {'✅' if trade['details']['fvg'] else '❌'}\n"
                           f"💰 Entry: <b>{trade['price']}</b>\n"
                           f"🎯 TP: <b>{trade['tp']}</b> ({trade['tp_pts']} pts)\n"
                           f"🛑 SL: <b>{trade['sl']}</b>\n"
                           f"Trends: 1H {trade['t']['1h']} | 4H {trade['t']['4h']}")
                    
                    pending_trades[pair] = trade
                    last_signal[signal_key] = True
                    send_with_buttons(msg, trade)

                        "cancelled": bool(danger_news)
                    }
                    opportunities.append(op)
                    push_to_github(opportunities)

                    if danger_news:
                        reset_pair_states(pair)  # نريسيتيو الـ state machine كاملة — الإشارة ملغاة بسبب الأخبار
                        last_signal.pop(pair, None)
                        send_telegram(
                            f"⚠️ <b>تحذير — {pair}</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"كانت كاينة إشارة {trade['direction']} ولكن تم إلغاؤها:\n\n"
                            + "\n".join([f"🔴 {n}" for n in danger_news]) +
                            f"\n\n⏳ استنى تعدي الأخبار\n🕐 {now_str}"
                        )
                        continue

                    tfs_text = " + ".join(trade["confirmed_tfs"])
                    strength_text = get_strength_label(trade["strength"])

                    news_warning = ""
                    if warning_news:
                        news_warning = "\n⚠️ <b>أخبار قادمة:</b>\n" + "\n".join([f"🟡 {n}" for n in warning_news]) + "\n"

                    market = get_market_summary(trade['pair'])
                    today_news = get_news_summary(trade['pair'])

                    market_section = ""
                    if market:
                        market_section = (
                            f"\n📊 <b>السوق اليوم:</b>\n"
                            f"  {market['direction_emoji']} التغيير: {market['change']:+.6f} ({market['change_pct']:+.3f}%)\n"
                            f"  🔝 أعلى: {market['high_day']} | 🔻 أدنى: {market['low_day']}\n"
                            f"  {market['last_hour_emoji']} آخر ساعة: {market['last_hour_change']:+.6f}\n"
                        )

                    news_section = ""
                    if today_news:
                        news_section = f"\n📰 <b>أخبار اليوم:</b>\n" + "\n".join([f"  {n}" for n in today_news]) + "\n"

                    msg = (
                        f"🔔 <b>فرصة تريد — {trade['pair']}</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"📊 الإشارة: <b>{trade['direction']}</b>\n"
                        f"💪 القوة: <b>{strength_text}</b>\n"
                        f"⏱ مؤكدة على: <b>{tfs_text}</b>\n"
                        f"📐 السلسلة: Liquidity Sweep ✅ → BOS ✅ → Pullback ✅ → Candle ✅\n"
                        f"{market_section}"
                        f"{news_section}"
                        f"\n💰 السعر الحالي: <b>{trade['price']}</b>\n"
                        f"🎯 TP: <b>{trade['tp']}</b>\n"
                        f"🛑 SL: <b>{trade['sl']}</b>\n"
                        f"⚖️ R/R: <b>1:{trade['rr']}</b>\n\n"
                        f"{news_warning}"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"🕐 {now_str}\n\n"
                        f"واش بغيتي تدخل هاد التريد؟"
                    )

                    pending_trades[pair] = trade
                    last_signal[pair] = {
                        "direction": current_direction,
                        "bos_level": current_bos_level,
                    }
                    send_with_buttons(msg, trade)
                    # ماكاينش break — كيكمل على باقي الأزواج

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(900)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
    main_loop()
