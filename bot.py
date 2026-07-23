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

SWING_LOOKBACK = 3          # 3 شمعات يمين + 3 يسار لتحديد القمم والقيعان بدقة
MAJOR_SWING_LOOKBACK = 5    # لفريمات 1H/4H فقط: عمق أكبر لتحديد Major Swings الرئيسية وتفادي ضجيج السعر (Noise) عند بناء HTF Bias
PULLBACK_MAX_CANDLES = 6    # حد أقصى للشموع لانتظار الـ Pullback
BOS_MAX_CANDLES = 10        # حد أقصى للشموع لانتظار BOS بعد الـ Sweep
SWEEP_ATR_MULTIPLIER = 0.15
RECENT_CHECK_CANDLES = 3    # التحقق من آخر 3 شموع لـ Sweep/Candle confirmation
PULLBACK_TOUCH_ATR = 0.3    # القرب الكافي من منطقة OB/FVG

# حالة التريدات المنتظرة للتأكيد — dict بالـ pair كـ key
pending_trades = {}        # {"USD/JPY": trade_dict, ...}
waiting_confirmation = {}  # {"USD/JPY": True/False, ...}

# State machine لكل زوج وفريم لتتبع مراحل الـ SMC بدقة
sequence_state = {}

# Cache البيانات لمنع استهلاك الـ Credits بشكل عشوائي
data_cache = {}

def fetch_all_data():
    """كيجيب بيانات كل الأزواج مرة واحدة ويحفظها فالـ cache"""
    global data_cache
    data_cache = {}
    for pair in PAIRS:
        data_cache[pair] = {}
        for tf in TIMEFRAMES:  # تم حذف الـ 5min نهائياً
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
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
    time.sleep(2)
    webhook_url = "https://forex-trading-bot-2-production.up.railway.app/webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    r = requests.post(url, json={"url": webhook_url})
    print(f"Webhook set: {r.json()}")

def is_killzone():
    """تحديد وقت السيولة العالية 7h-17h UTC (جلسات لندن ونيويورك)"""
    now_utc = datetime.now(timezone.utc)
    return 7 <= now_utc.hour < 17

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
    try:
        result_1h = get_cached_data(pair, "1h") or get_price_data(pair, "1h", 24)
        result_15 = get_cached_data(pair, "15min") or get_price_data(pair, "15min", 8)
        if not result_1h or not result_15:
            return None

        closes_1h = result_1h[0]
        closes_15 = result_15[0]

        open_price = closes_1h[0]
        current = closes_1h[-1]
        change = round(current - open_price, 6)
        change_pct = round((change / open_price) * 100, 3)
        direction_emoji = "📈" if change > 0 else "📉"

        highs_1h = result_1h[1]
        lows_1h = result_1h[2]
        high_day = round(max(highs_1h), 6)
        low_day = round(min(lows_1h), 6)

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

def calc_ema(prices, period=200):
    if len(prices) < period:
        return None
    ema = sum(prices[:period]) / period
    multiplier = 2 / (period + 1)
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def get_trend_structure(closes):
    if len(closes) < 20:
        return None
    recent = closes[-10:]
    older = closes[-20:-10]
    if max(recent) > max(older) and min(recent) > min(older):
        return "UP"
    if max(recent) < max(older) and min(recent) < min(older):
        return "DOWN"
    return "SIDEWAYS"

def get_swing_points(highs, lows):
    """Swing High/Low بـ 3 شمعات يمين + 3 يسار"""
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
    filtered = [s for s in swings if s[2] == swing_type]
    if before_index is not None:
        filtered = [s for s in filtered if s[0] < before_index]
    if not filtered:
        return None
    return filtered[-1]

def is_bullish_engulfing(opens, closes, i):
    if i < 1:
        return False
    prev_open, prev_close = opens[i-1], closes[i-1]
    curr_open, curr_close = opens[i], closes[i]
    return prev_close < prev_open and curr_close > curr_open and curr_open <= prev_close and curr_close >= prev_open

def is_bearish_engulfing(opens, closes, i):
    if i < 1:
        return False
    prev_open, prev_close = opens[i-1], closes[i-1]
    curr_open, curr_close = opens[i], closes[i]
    return prev_close > prev_open and curr_close < curr_open and curr_open >= prev_close and curr_close <= prev_open

def is_strong_bull_candle(opens, highs, lows, closes, i):
    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
    total_range = h - l
    return total_range > 0 and (c - o) > 0 and ((c - o) / total_range) > 0.70

def is_strong_bear_candle(opens, highs, lows, closes, i):
    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
    total_range = h - l
    return total_range > 0 and (o - c) > 0 and ((o - c) / total_range) > 0.70

def check_candlestick_confirmation(opens, highs, lows, closes, direction):
    n = len(closes)
    start = max(1, n - RECENT_CHECK_CANDLES)
    for i in range(start, n):
        if direction == "BUY":
            if is_bullish_engulfing(opens, closes, i) or is_strong_bull_candle(opens, highs, lows, closes, i):
                return True
        else:
            if is_bearish_engulfing(opens, closes, i) or is_strong_bear_candle(opens, highs, lows, closes, i):
                return True
    return False

def reset_state(state_key):
    sequence_state[state_key] = {"stage": "waiting_sweep"}

def check_recent_sweep(highs, lows, closes, swings, sweep_threshold):
    n = len(closes)
    start = max(0, n - RECENT_CHECK_CANDLES)
    for i in range(start, n):
        last_swing_low = get_last_swing(swings, "low", before_index=i)
        last_swing_high = get_last_swing(swings, "high", before_index=i)

        if last_swing_low:
            low_level = last_swing_low[1]
            if lows[i] < (low_level - sweep_threshold) and closes[i] > low_level:
                return "BUY", low_level

        if last_swing_high:
            high_level = last_swing_high[1]
            if highs[i] > (high_level + sweep_threshold) and closes[i] < high_level:
                return "SELL", high_level
    return None

def find_order_block_buy(closes, opens, highs, lows, bos_index):
    """تحديد الـ Order Block الصاعد (آخر شمعة هابطة قبل الانطلاق لكسر الـ BOS)"""
    for j in range(bos_index, max(0, bos_index - 15), -1):
        if closes[j] < opens[j]:
            return lows[j], highs[j]
    return lows[bos_index], highs[bos_index]

def find_order_block_sell(closes, opens, highs, lows, bos_index):
    """تحديد الـ Order Block الهابط (آخر شمعة صاعدة قبل الانطلاق لكسر الـ BOS)"""
    for j in range(bos_index, max(0, bos_index - 15), -1):
        if closes[j] > opens[j]:
            return lows[j], highs[j]
    return lows[bos_index], highs[bos_index]

def find_recent_fvg_buy(highs, lows, bos_index):
    """البحث عن أقرب Fair Value Gap صاعد"""
    for j in range(bos_index, max(2, bos_index - 5), -1):
        if lows[j] > highs[j-2]:
            return highs[j-2], lows[j]
    return None

def find_recent_fvg_sell(highs, lows, bos_index):
    """البحث عن أقرب Fair Value Gap هابط"""
    for j in range(bos_index, max(2, bos_index - 5), -1):
        if highs[j] < lows[j-2]:
            return highs[j], lows[j-2]
    return None

def analyze_timeframe(pair, interval):
    """State machine متطورة مع دمج الـ OB والـ FVG"""
    result = get_cached_data(pair, interval) or get_price_data(pair, interval)
    if not result:
        return None

    closes, highs, lows, opens = result
    atr = calc_atr(highs, lows, closes)
    if atr is None:
        return None

    swings = get_swing_points(highs, lows)
    if not swings:
        return None

    state_key = f"{pair}_{interval}"
    state = sequence_state.get(state_key, {"stage": "waiting_sweep"})
    stage = state.get("stage", "waiting_sweep")

    current_price = closes[-1]
    current_high = highs[-1]
    current_low = lows[-1]
    current_close = closes[-1]
    sweep_threshold = atr * SWEEP_ATR_MULTIPLIER

    # ---------- المرحلة 1: البحث على Liquidity Sweep ----------
    if state["stage"] == "waiting_sweep":
        sweep = check_recent_sweep(highs, lows, closes, swings, sweep_threshold)
        if sweep:
            direction, swing_level = sweep
            sequence_state[state_key] = {
                "stage": "waiting_bos",
                "direction": direction,
                "swing_level": swing_level,
                "candles_since_sweep": 0,
            }
        return None

    # ---------- المرحلة 2: البحث على BOS واحتساب الـ OB والـ FVG ----------
    if state["stage"] == "waiting_bos":
        direction = state["direction"]
        bos_found = False
        bos_level = None
        bos_index = len(closes) - 1

        if direction == "BUY":
            last_swing_high = get_last_swing(swings, "high")
            if last_swing_high and current_close > last_swing_high[1]:
                bos_found = True
                bos_level = last_swing_high[1]
        else:
            last_swing_low = get_last_swing(swings, "low")
            if last_swing_low and current_close < last_swing_low[1]:
                bos_found = True
                bos_level = last_swing_low[1]

        if bos_found:
            # البحث وتحديد الـ Order Block والـ FVG
            if direction == "BUY":
                ob_low, ob_high = find_order_block_buy(closes, opens, highs, lows, bos_index)
                fvg = find_recent_fvg_buy(highs, lows, bos_index)
            else:
                ob_low, ob_high = find_order_block_sell(closes, opens, highs, lows, bos_index)
                fvg = find_recent_fvg_sell(highs, lows, bos_index)

            fvg_low = fvg[0] if fvg else ob_low
            fvg_high = fvg[1] if fvg else ob_high

            state["stage"] = "waiting_pullback"
            state["bos_level"] = bos_level
            state["ob_low"] = ob_low
            state["ob_high"] = ob_high
            state["fvg_low"] = fvg_low
            state["fvg_high"] = fvg_high
            state["candles_since_bos"] = 0
            state["touched_bos"] = False
            sequence_state[state_key] = state
            return None

        state["candles_since_sweep"] = state.get("candles_since_sweep", 0) + 1
        if state["candles_since_sweep"] > BOS_MAX_CANDLES:
            reset_state(state_key)
            return None

        sequence_state[state_key] = state
        return None

    # ---------- المرحلة 3: انتظار Pullback لـ OB أو FVG ----------
    if state["stage"] == "waiting_pullback":
        direction = state["direction"]
        ob_low = state["ob_low"]
        ob_high = state["ob_high"]
        fvg_low = state["fvg_low"]
        fvg_high = state["fvg_high"]

        if direction == "BUY":
            # إلغاء الـ Setup في حال كسر الـ OB بالكامل للاسفل واغلق السعر تحته
            if current_close < ob_low:
                reset_state(state_key)
                return None

            # البحث عن تراجع تصحيحي للمنطقة الفوقية من الـ OB أو الـ FVG
            pullback_boundary = max(ob_high, fvg_high)
            if current_low <= pullback_boundary + (atr * PULLBACK_TOUCH_ATR):
                state["touched_bos"] = True

            # بعد الملامسة، ننتظر ارتداد السعر للأعلى وبدء الابتعاد
            if state.get("touched_bos") and current_close > pullback_boundary:
                state["stage"] = "waiting_candle"
                sequence_state[state_key] = state
                return None
        else:
            # إلغاء الـ Setup في حال اخترق الـ OB للاعلى واغلق السعر فوقه
            if current_close > ob_high:
                reset_state(state_key)
                return None

            pullback_boundary = min(ob_low, fvg_low)
            if current_high >= pullback_boundary - (atr * PULLBACK_TOUCH_ATR):
                state["touched_bos"] = True

            if state.get("touched_bos") and current_close < pullback_boundary:
                state["stage"] = "waiting_candle"
                sequence_state[state_key] = state
                return None

        state["candles_since_bos"] = state.get("candles_since_bos", 0) + 1
        if state["candles_since_bos"] > PULLBACK_MAX_CANDLES:
            reset_state(state_key)
            return None

        sequence_state[state_key] = state
        return None

    # ---------- المرحلة 4: تأكيد الشموع الإنعكاسية (Candlestick Confirmation) ----------
    if state["stage"] == "waiting_candle":
        direction = state["direction"]
        bos_level = state["bos_level"]

        confirmed = check_candlestick_confirmation(opens, highs, lows, closes, direction)
        if confirmed:
            reset_state(state_key)
            return {
                "direction": direction,
                "atr": atr,
                "price": current_price,
                "bos_level": bos_level,
            }

        state["candles_since_bos"] = state.get("candles_since_bos", 0) + 1
        if state["candles_since_bos"] > PULLBACK_MAX_CANDLES + RECENT_CHECK_CANDLES:
            reset_state(state_key)
            return None

        sequence_state[state_key] = state
        return None

    return None

def get_major_swing_points(highs, lows, lookback=MAJOR_SWING_LOOKBACK):
    """Major Swing Highs/Lows بعمق أكبر من swings فريم الدخول (15min) لتفادي ضجيج السعر (Noise) —
    تُستخدم حصرياً لبناء الـ HTF Market Structure (CHoCH/BOS) على 1H و4H"""
    swings = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        window_highs = highs[i - lookback: i + lookback + 1]
        window_lows = lows[i - lookback: i + lookback + 1]

        if highs[i] == max(window_highs) and window_highs.count(highs[i]) == 1:
            swings.append((i, highs[i], "high"))

        if lows[i] == min(window_lows) and window_lows.count(lows[i]) == 1:
            swings.append((i, lows[i], "low"))

    swings.sort(key=lambda s: s[0])
    return swings

def get_smc_htf_bias(highs, lows, closes):
    """يحدد الـ HTF Bias بمنطق SMC حقيقي على Major Swing Structure فقط:
    كسر آخر Major Swing المعاكس = CHoCH (تحذير مبكر فقط، لا يغيّر الـ Bias بعد)
    كسر تأكيدي إضافي في نفس اتجاه الـ CHoCH = BOS → عندها فقط يتم اعتماد الـ Bias الجديد
    التأكيد (CHoCH/BOS) يتم بشمعة لاحقة حقيقية (Subsequent Candle) وليس بنفس شمعة الـ Swing،
    بنفس منطق التأكيد المستعمل فـ 15min."""
    swings = get_major_swing_points(highs, lows)
    if len(swings) < 2:
        return None

    trend = None            # الاتجاه الهيكلي المؤكد حالياً على الـ HTF: "UP" / "DOWN"
    structure_high = None   # آخر Major Swing High مرجعي لكسر الـ CHoCH/BOS الصاعد
    structure_low = None    # آخر Major Swing Low مرجعي لكسر الـ CHoCH/BOS الهابط
    choch_direction = None  # CHoCH معلّق بانتظار BOS تأكيدي
    bias = None

    n = len(closes)

    for idx in range(len(swings)):
        i, level, kind = swings[idx]

        # نحدد نافذة البحث عن شمعة لاحقة تأكد الكسر: من الشمعة اللي بعد الـ Swing
        # حتى الشمعة اللي قبل الـ Swing التالي (أو نهاية البيانات إلا كان آخر Swing)
        next_swing_index = swings[idx + 1][0] if idx + 1 < len(swings) else n
        confirm_start = i + 1
        confirm_end = min(next_swing_index, n)

        if kind == "high":
            if structure_high is None:
                structure_high = level
                continue

            if trend != "UP":
                confirmed = False
                for j in range(confirm_start, confirm_end):
                    close_at_j = closes[j]
                    if close_at_j > structure_high:
                        confirmed = True
                        break

                if confirmed:
                    if trend is None:
                        # أول اتجاه هيكلي مبدئي — يُعتمد مباشرة كأول مرجع هيكلي
                        trend = "UP"
                        bias = "BUY"
                        choch_direction = None
                        structure_high = level
                    elif choch_direction == "UP":
                        # BOS مؤكد بعد CHoCH صاعد سابق → يحدّث المرجع الهيكلي ويعتمد الـ Bias الجديد فقط الآن
                        trend = "UP"
                        bias = "BUY"
                        choch_direction = None
                        structure_high = level
                    else:
                        # CHoCH: إنذار مبكر فقط — المرجع الهيكلي يبقى ثابتاً بلا أي تحديث
                        choch_direction = "UP"
            # أي Major Swing High آخر (استمرار عادي بلا كسر) لا يحدّث المرجع الهيكلي إطلاقاً

        else:  # kind == "low"
            if structure_low is None:
                structure_low = level
                continue

            if trend != "DOWN":
                confirmed = False
                for j in range(confirm_start, confirm_end):
                    close_at_j = closes[j]
                    if close_at_j < structure_low:
                        confirmed = True
                        break

                if confirmed:
                    if trend is None:
                        trend = "DOWN"
                        bias = "SELL"
                        choch_direction = None
                        structure_low = level
                    elif choch_direction == "DOWN":
                        # BOS مؤكد بعد CHoCH هابط سابق → يحدّث المرجع الهيكلي ويعتمد الـ Bias الجديد فقط الآن
                        trend = "DOWN"
                        bias = "SELL"
                        choch_direction = None
                        structure_low = level
                    else:
                        # CHoCH: إنذار مبكر فقط — المرجع الهيكلي يبقى ثابتاً بلا أي تحديث
                        choch_direction = "DOWN"
            # أي Major Swing Low آخر (استمرار عادي بلا كسر) لا يحدّث المرجع الهيكلي إطلاقاً

    return bias

def get_timeframe_bias(pair, interval):
    """يحدد الـ HTF Bias على الفريمات الكبيرة (1H/4H) بمنطق SMC: Major Swings → CHoCH → BOS → Bias"""
    result = get_cached_data(pair, interval) or get_price_data(pair, interval)
    if not result:
        return None
    closes, highs, lows, opens = result
    return get_smc_htf_bias(highs, lows, closes)

def get_htf_structure_debug(highs, lows, closes):
    """Read-only: نفس منطق get_smc_htf_bias بالضبط، لكن كترجع تقرير تشخيصي منظم BUY/SELL
    للاستخدام فـ get_debug_report فقط. ما كتبدلش أي state ولا كتأثر على منطق الدخول."""
    swings = get_major_swing_points(highs, lows)
    if len(swings) < 2:
        return "⏳ Not enough Major Swings yet"

    trend = None
    structure_high = None
    structure_low = None
    choch_direction = None
    bias = None
    choch_events = []
    bos_events = []

    n = len(closes)

    for idx in range(len(swings)):
        i, level, kind = swings[idx]
        next_swing_index = swings[idx + 1][0] if idx + 1 < len(swings) else n
        confirm_start = i + 1
        confirm_end = min(next_swing_index, n)

        if kind == "high":
            if structure_high is None:
                structure_high = level
                continue

            if trend != "UP":
                confirmed = any(closes[j] > structure_high for j in range(confirm_start, confirm_end))
                if confirmed:
                    if trend is None:
                        trend = "UP"
                        bias = "BUY"
                        choch_direction = None
                        structure_high = level
                    elif choch_direction == "UP":
                        bos_events.append(("BUY", level))
                        trend = "UP"
                        bias = "BUY"
                        choch_direction = None
                        structure_high = level
                    else:
                        choch_direction = "UP"
                        choch_events.append(("UP", level))

        else:
            if structure_low is None:
                structure_low = level
                continue

            if trend != "DOWN":
                confirmed = any(closes[j] < structure_low for j in range(confirm_start, confirm_end))
                if confirmed:
                    if trend is None:
                        trend = "DOWN"
                        bias = "SELL"
                        choch_direction = None
                        structure_low = level
                    elif choch_direction == "DOWN":
                        bos_events.append(("SELL", level))
                        trend = "DOWN"
                        bias = "SELL"
                        choch_direction = None
                        structure_low = level
                    else:
                        choch_direction = "DOWN"
                        choch_events.append(("DOWN", level))

    last_choch_up = next((e for e in reversed(choch_events) if e[0] == "UP"), None)
    last_choch_down = next((e for e in reversed(choch_events) if e[0] == "DOWN"), None)
    last_bos_buy = next((e for e in reversed(bos_events) if e[0] == "BUY"), None)
    last_bos_sell = next((e for e in reversed(bos_events) if e[0] == "SELL"), None)

    lines = [f"📌 Major Swings: {len(swings)}"]

    # ---------------- BUY ----------------
    lines.append("\nBUY")
    lines.append(f"⚠️ CHoCH: {'UP (' + str(last_choch_up[1]) + ')' if last_choch_up else 'None'}")
    lines.append(f"✅ BOS: {'BUY (' + str(last_bos_buy[1]) + ')' if last_bos_buy else 'None'}")
    if bias == "BUY":
        lines.append("🎯 HTF Bias: BUY ✅")
    else:
        lines.append("🎯 HTF Bias: Not Active")

    # ---------------- SELL ----------------
    lines.append("\nSELL")
    lines.append(f"⚠️ CHoCH: {'DOWN (' + str(last_choch_down[1]) + ')' if last_choch_down else 'None'}")
    lines.append(f"✅ BOS: {'SELL (' + str(last_bos_sell[1]) + ')' if last_bos_sell else 'None'}")
    if bias == "SELL":
        lines.append("🎯 HTF Bias: SELL ✅")
    else:
        lines.append("🎯 HTF Bias: Not Active")

    return "\n".join(lines)
    
def reset_pair_states(pair):
    for tf in TIMEFRAMES:
        reset_state(f"{pair}_{tf}")

def analyze_pair(pair):
    """تقييم الإشارة وفحص التوافق عبر الأطر الزمنية المتعددة (Stars System)"""
    results = {}
    
    # فريم 15min هو محرك البحث والإشارة الرئيسي (The Trigger)
    m15_res = analyze_timeframe(pair, "15min")
    if not m15_res:
        return None

    results["15min"] = m15_res
    direction = m15_res["direction"]
    price = m15_res["price"]
    atr = m15_res["atr"]

    # فحص توافق الاتجاه على الفريمات الكبيرة لتوزيع النجوم
    h1_bias = get_timeframe_bias(pair, "1h")
    h4_bias = get_timeframe_bias(pair, "4h")

    confirmed_tfs = ["15min"]
    if h1_bias == direction:
        confirmed_tfs.append("1h")
    if h4_bias == direction:
        confirmed_tfs.append("4h")

    is_jpy = pair.endswith("JPY") or pair.startswith("JPY")
    max_tp = 2.20 if is_jpy else 0.00220
    
    # احتساب أهداف جني الأرباح الافتراضية
    tp_distance = min(atr * 1.5, max_tp)

    # احتساب الهدف الهيكلي المرن (Flexible Target) بالاعتماد على القمم والقيعان السابقة
    result_15 = get_cached_data(pair, "15min")
    if result_15:
        closes, highs, lows, opens = result_15
        swings = get_swing_points(highs, lows)
        if direction == "BUY":
            last_high = get_last_swing(swings, "high")
            if last_high:
                struct_dist = abs(last_high[1] - price)
                if struct_dist < tp_distance:
                    tp_distance = struct_dist
        else:
            last_low = get_last_swing(swings, "low")
            if last_low:
                struct_dist = abs(price - last_low[1])
                if struct_dist < tp_distance:
                    tp_distance = struct_dist

    # ضمان عدم اختيار أهداف متناهية الصغر أثناء ضغط السوق
    tp_distance = max(tp_distance, 0.50 if is_jpy else 0.00050)
    sl_distance = tp_distance / 1.5

    if direction == "BUY":
        if is_jpy:
            tp = round(price + tp_distance, 3)
            sl = round(price - sl_distance, 3)
        else:
            tp = round(price + tp_distance, 5)
            sl = round(price - sl_distance, 5)
    else:
        if is_jpy:
            tp = round(price - tp_distance, 3)
            sl = round(price + sl_distance, 3)
        else:
            tp = round(price - tp_distance, 5)
            sl = round(price + sl_distance, 5)

    rr = round(tp_distance / sl_distance, 2)

    return {
        "pair": pair,
        "direction": "BUY 📈" if direction == "BUY" else "SELL 📉",
        "price": price,
        "tp": tp,
        "sl": sl,
        "rr": rr,
        "strength": len(confirmed_tfs),
        "confirmed_tfs": confirmed_tfs,
        "details": {"15min": m15_res}
    }

def get_strength_label(strength):
    if strength == 3:
        return "⭐⭐⭐ Gold (15min + 1H + 4H Alignment)"
    elif strength == 2:
        return "⭐⭐ Silver (15min + HTF Alignment)"
    return "⭐ Bronze (15min Setup)"

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
            f"🔄 <b>تحديث — {trade['pair']}</b>\n"
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
    """Read-only SMC diagnostic report. No trading logic or state changes."""

    lines = [f"🔍 {pair}"]

    # =========================
    # 15m SMC LOGIC
    # =========================
    result_15 = get_cached_data(pair, "15min") or get_price_data(pair, "15min")

    lines.append("\n━━━━━━━━━━━━━━━━")
    lines.append("15min (SMC Logic)")
    lines.append("━━━━━━━━━━━━━━━━")

    if not result_15:
        lines.append("❌ No market data")
    else:
        closes, highs, lows, opens = result_15
        atr = calc_atr(highs, lows, closes)

        if atr is None:
            lines.append("❌ ATR not available")
        else:
            swings = get_swing_points(highs, lows)
            sweep_threshold = atr * SWEEP_ATR_MULTIPLIER

            state_key = f"{pair}_15min"
            state = sequence_state.get(
                state_key,
                {"stage": "waiting_sweep"}
            )

            stage = state.get("stage", "waiting_sweep")
            direction = state.get("direction")

            # ---------------------------------
            # BUY / SELL diagnostic display
            # ---------------------------------
            for side in ["BUY", "SELL"]:
                lines.append(f"\n{side}")

                score = 0

                if direction == side:
                    current_stage = stage

                    # Sweep
                    if current_stage in [
                        "waiting_bos",
                        "waiting_pullback",
                        "waiting_candle"
                    ]:
                        swing_level = state.get("swing_level", 0.0)
                        lines.append(
                            f"✅ Sweep: Found ({swing_level})"
                        )
                        score += 1
                    else:
                        lines.append("❌ Sweep: Not found")

                    # BOS
                    if current_stage in [
                        "waiting_pullback",
                        "waiting_candle"
                    ]:
                        bos_level = state.get("bos_level", 0.0)
                        lines.append(
                            f"✅ BOS: Confirmed ({bos_level})"
                        )
                        score += 1
                    elif current_stage == "waiting_bos":
                        candles = state.get(
                            "candles_since_sweep",
                            0
                        )
                        lines.append(
                            f"⏳ BOS: Waiting "
                            f"({candles}/{BOS_MAX_CANDLES})"
                        )
                    else:
                        lines.append("❌ BOS: Waiting")

                    # OB / FVG
                    if current_stage in [
                        "waiting_pullback",
                        "waiting_candle"
                    ]:
                        ob_low = state.get("ob_low", 0.0)
                        ob_high = state.get("ob_high", 0.0)
                        fvg_low = state.get("fvg_low", 0.0)
                        fvg_high = state.get("fvg_high", 0.0)

                        lines.append(
                            f"✅ OB: {ob_low} → {ob_high}"
                        )
                        lines.append(
                            f"✅ FVG: {fvg_low} → {fvg_high}"
                        )
                        score += 1
                    elif current_stage == "waiting_bos":
                        lines.append(
                            "⏳ OB/FVG: Waiting for formation"
                        )
                    else:
                        lines.append(
                            "❌ OB/FVG: Not formed"
                        )

                    # Pullback
                    if current_stage == "waiting_candle":
                        touched = state.get(
                            "touched_bos",
                            False
                        )
                        lines.append(
                            "✅ Pullback: Confirmed"
                            if touched
                            else "⏳ Pullback: Waiting"
                        )
                        if touched:
                            score += 1
                    elif current_stage == "waiting_pullback":
                        touched = state.get(
                            "touched_bos",
                            False
                        )
                        candles = state.get(
                            "candles_since_bos",
                            0
                        )

                        if touched:
                            lines.append(
                                "✅ Pullback: Touched"
                            )
                            score += 1
                        else:
                            lines.append(
                                "⏳ Pullback: Waiting"
                            )

                        lines.append(
                            f"   Candles: "
                            f"{candles}/{PULLBACK_MAX_CANDLES}"
                        )
                    else:
                        lines.append(
                            "❌ Pullback: Waiting"
                        )

                    # Candle confirmation
                    if current_stage == "waiting_candle":
                        candles = state.get(
                            "candles_since_bos",
                            0
                        )

                        lines.append(
                            "⏳ Candle Conf: Waiting"
                        )
                        lines.append(
                            f"   Attempts: "
                            f"{candles}/"
                            f"{PULLBACK_MAX_CANDLES + RECENT_CHECK_CANDLES}"
                        )
                    else:
                        lines.append(
                            "❌ Candle Conf: Waiting"
                        )

                else:
                    lines.append("❌ Sweep: Not found")
                    lines.append("❌ BOS: Waiting")
                    lines.append("❌ OB/FVG: Not formed")
                    lines.append("❌ Pullback: Waiting")
                    lines.append("❌ Candle Conf: Waiting")

                lines.append(f"Score: {score}/5")

    # =========================
    # 1H SMC STRUCTURE (Major Swings → CHoCH → BOS → Bias)
    # =========================
    lines.append("\n━━━━━━━━━━━━━━━━")
    lines.append("1H (SMC Structure)")
    lines.append("━━━━━━━━━━━━━━━━")

    result_1h = get_cached_data(pair, "1h") or get_price_data(pair, "1h")

    if result_1h:
        closes, highs, lows, opens = result_1h
        lines.append(get_htf_structure_debug(highs, lows, closes))

    # =========================
    # 4H SMC STRUCTURE (Major Swings → CHoCH → BOS → Bias)
    # =========================
    lines.append("\n━━━━━━━━━━━━━━━━")
    lines.append("4H (SMC Structure)")
    lines.append("━━━━━━━━━━━━━━━━")

    result_4h = get_cached_data(pair, "4h") or get_price_data(pair, "4h")

    if result_4h:
        closes, highs, lows, opens = result_4h
        lines.append(get_htf_structure_debug(highs, lows, closes))

    return "\n".join(lines)
    
def send_hourly_report(pairs_status):
    for pair in pairs_status:
        send_telegram(get_debug_report(pair))

def main_loop():
    global pending_trades, waiting_confirmation
    time.sleep(5)
    set_webhook()

    opportunities = pull_from_github()
    last_report_hour = -1
    last_signal = {}

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

            # دمج التحليل المستمر لإبقاء الذاكرة نشطة مع تصفية الإرسال فقط وقت الـ Killzone
            for pair in PAIRS:
                if waiting_confirmation.get(pair):
                    continue

                # البوت يحلل ويحدث الـ State Machine على مدار 24 ساعة لكي لا تضيع أي حركة
                trade = analyze_pair(pair)
                current_direction = "BUY" if trade and "BUY" in trade["direction"] else ("SELL" if trade and "SELL" in trade["direction"] else None)

                if not current_direction:
                    last_signal.pop(pair, None)
                    continue

                # تصفية الدخول الفعلي وإرسال التنبيهات: يتم فقط أثناء جلسات السيولة العالية
                if not is_killzone():
                    print(f"⏳ {pair}: فرصة جاهزة ومكتملة الشروط، ولكن تم تأجيلها لعدم دخول الـ Killzone بعد.")
                    continue

                current_bos_level = trade["details"]["15min"]["bos_level"]

                prev = last_signal.get(pair)
                if prev is not None:
                    same_direction = prev["direction"] == current_direction
                    same_bos = prev["bos_level"] == current_bos_level
                    if same_direction and same_bos:
                        continue

                danger_news, warning_news = get_high_impact_news(pair)

                op = {
                    "date": now.strftime("%Y-%m-%d %H:%M"),
                    "time": now_str,
                    "pair": pair,
                    "direction": trade["direction"],
                    "price": trade["price"],
                    "tp": trade["tp"],
                    "sl": trade["sl"],
                    "rr": trade["rr"],
                    "strength": trade["strength"],
                    "cancelled": bool(danger_news)
                }
                opportunities.append(op)
                push_to_github(opportunities)

                if danger_news:
                    reset_pair_states(pair)  # إلغاء الـ state بالكامل في حالة الأخبار الخطيرة
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
                    f"📐 السلسلة: Liquidity Sweep ✅ → BOS ✅ → Pullback (OB/FVG) ✅ → Candle Confirmation ✅\n"
                    f"{market_section}"
                    f"{news_section}"
                    f"\n💰 السعر الحالي: <b>{trade['price']}</b>\n"
                    f"🎯 TP: <b>{trade['tp']}</b>\n"
                    f"🛑 SL: <b>{trade['sl']}</b>\n"
                    f"⚖️ R/R: <b>1:{trade['rr']}</b>\n\n"
                    f"{news_warning}"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"🕐 {now_str}\n\n"
                    f"واش بغيتي تدخل هاد التريد? "
                )

                pending_trades[pair] = trade
                last_signal[pair] = {
                    "direction": current_direction,
                    "bos_level": current_bos_level,
                }
                send_with_buttons(msg, trade)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(900)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
    main_loop()
