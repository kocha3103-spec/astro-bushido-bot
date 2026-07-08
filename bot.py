import os
import asyncio
import logging
import json
import httpx
import swisseph as swe
from geopy.geocoders import Nominatim
from tzfpy import get_tz
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
from yookassa import Configuration, Payment as YooPayment
from retrieval import retrieve, build_query
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
HYDRA_API_KEY = os.environ["HYDRA_API_KEY"]
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET = os.environ.get("YOOKASSA_SECRET", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
HYDRA_API_URL = "https://api.hydraai.ru/v1/chat/completions"
MODEL = "claude-opus-4.6"
USER_DATA_FILE = os.environ.get("USER_DATA_FILE", "/data/user_data.json")
COMMUNITY_LINK = "https://t.me/astro_bushido"
KATYA_TG = "katerinakocha"
PDPA_URL = "https://docs.google.com/document/d/1_vgWqlkUaRYP0BX9oK3ZOlvgB6SJO1fa/edit?usp=sharing&ouid=116341699354892317088&rtpof=true&sd=true"
PRIVACY_POLICY_URL = "https://docs.google.com/document/d/1ZAL8gRNz12jiMZVl-TdJzNw8vDBP4YRy/edit?usp=sharing&ouid=116341699354892317088&rtpof=true&sd=true"
OFERTA_URL = "https://docs.google.com/document/d/1QT5lUJ3Mt4lqeoY_DmjrHSH3xRo4vr3v/edit?usp=sharing&ouid=116341699354892317088&rtpof=true&sd=true"
BOT_USERNAME = "astro_bushido_bot"
FREE_FORECASTS_LIMIT = 4

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET

# Продамус: ссылка платёжной страницы вида https://твойшоп.payform.ru
PRODAMUS_URL = os.environ.get("PRODAMUS_URL", "").rstrip("/")
PRODAMUS_SECRET = os.environ.get("PRODAMUS_SECRET", "")
WEBHOOK_PORT = int(os.environ.get("PORT", "8080"))

SUBSCRIPTION_PLANS = {
    "full_year": {
        "name": "✨ безлимит на весь год",
        "desc": "все виды прогнозов без ограничений на 1 год — включая новые, которые буду добавлять в течение года.",
        "price": "5000.00",
        "label": "5 000 ₽",
    },
    "month": {
        "name": "🌙 безлимит на месяц",
        "desc": "все виды прогнозов без ограничений на 1 месяц.",
        "price": "500.00",
        "label": "500 ₽",
    },
    "single": {
        "name": "🔮 один прогноз",
        "desc": "один персональный прогноз на выбор.",
        "price": "100.00",
        "label": "100 ₽",
    },
}

NAME, BIRTH_DATE, BIRTH_TIME, BIRTH_CITY, MAIN_MENU = range(5)
LUNATIONS_PER_PAGE = 8

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]
SIGNS_EMOJI = {"Овен":"♈","Телец":"♉","Близнецы":"♊","Рак":"♋","Лев":"♌","Дева":"♍",
               "Весы":"♎","Скорпион":"♏","Стрелец":"♐","Козерог":"♑","Водолей":"♒","Рыбы":"♓"}

_LUNATIONS_CACHE = None
_LUNATIONS_CACHE_DATE = None
_ECLIPSES_CACHE = None
_ECLIPSES_CACHE_DATE = None


# ── ХРАНЕНИЕ ДАННЫХ ──────────────────────────────────────────────
def _ensure_data_dir():
    d = os.path.dirname(USER_DATA_FILE)
    if d:
        os.makedirs(d, exist_ok=True)

def load_user_data():
    _ensure_data_dir()
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_user_data(data):
    _ensure_data_dir()
    with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

PROMO_FILE = os.path.join(os.path.dirname(USER_DATA_FILE) or ".", "promocodes.json")

def load_promos():
    try:
        with open(PROMO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_promos(promos):
    _ensure_data_dir()
    with open(PROMO_FILE, "w", encoding="utf-8") as f:
        json.dump(promos, f, ensure_ascii=False, indent=2)

def get_user(user_id):
    return load_user_data().get(str(user_id))

def save_user(user_id, user_info):
    data = load_user_data()
    data[str(user_id)] = user_info
    save_user_data(data)

def has_subscription(user_id, plan_key):
    user = get_user(user_id)
    if not user:
        return False
    return user.get("subscriptions", {}).get(plan_key, False)

def grant_subscription(user_id, plan_key):
    data = load_user_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {}
    now = datetime.now(timezone.utc)
    subs = data[uid].setdefault("subscriptions", {})

    if plan_key == "full_year":
        subs["full_year"] = (now + timedelta(days=365)).isoformat()
    elif plan_key == "month":
        subs["month"] = (now + timedelta(days=30)).isoformat()
    elif plan_key == "single":
        data[uid]["bonus_forecasts"] = data[uid].get("bonus_forecasts", 0) + 1

    data[uid].setdefault("payments", []).append({
        "plan": plan_key,
        "at": now.isoformat()
    })
    save_user_data(data)

def has_active_unlimited(user_id):
    """Безлимит активен, если есть непросроченная подписка month или full_year."""
    user = get_user(user_id)
    if not user:
        return False
    subs = user.get("subscriptions", {})
    now = datetime.now(timezone.utc)
    for key in ("full_year", "month"):
        val = subs.get(key)
        if not val:
            continue
        # старый формат — было True (бессрочно), новый — дата окончания
        if val is True:
            return True
        try:
            if datetime.fromisoformat(val) > now:
                return True
        except (ValueError, TypeError):
            continue
    return False

def get_free_limit(user_id):
    user = get_user(user_id)
    bonus = user.get("bonus_forecasts", 0) if user else 0
    return FREE_FORECASTS_LIMIT + bonus

def get_free_used(user_id):
    user = get_user(user_id)
    return user.get("free_forecasts_used", 0) if user else 0

def can_get_free_forecast(user_id):
    if has_active_unlimited(user_id):
        return True
    return get_free_used(user_id) < get_free_limit(user_id)

def increment_forecast_counter(user_id):
    data = load_user_data()
    uid = str(user_id)
    data.setdefault(uid, {})
    data[uid]["free_forecasts_used"] = data[uid].get("free_forecasts_used", 0) + 1
    data[uid]["last_forecast_at"] = datetime.now(timezone.utc).isoformat()
    save_user_data(data)

def add_bonus_forecast(referrer_id):
    data = load_user_data()
    uid = str(referrer_id)
    if uid in data:
        data[uid]["bonus_forecasts"] = data[uid].get("bonus_forecasts", 0) + 1
        save_user_data(data)
        return True
    return False


# ── ЮКАССА ───────────────────────────────────────────────────────
def create_payment(user_id: int, plan_key: str):
    plan = SUBSCRIPTION_PLANS.get(plan_key)
    if not plan:
        return None
    try:
        payment = YooPayment.create({
            "amount": {"value": plan["price"], "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{BOT_USERNAME}?start=paid_{plan_key}_{user_id}",
            },
            "capture": True,
            "description": f"{plan['name']} — astro bushido bot",
            "metadata": {"user_id": str(user_id), "plan_key": plan_key},
        }, str(uuid4()))
        return {"payment_id": payment.id, "url": payment.confirmation.confirmation_url}
    except Exception as e:
        logger.error(f"YooKassa error: {e}")
        return None

def check_payment(payment_id: str) -> bool:
    try:
        payment = YooPayment.find_one(payment_id)
        return payment.status == "succeeded"
    except Exception:
        return False


# ── ПРОДАМУС ─────────────────────────────────────────────────────
def create_prodamus_link(user_id: int, plan_key: str):
    """Собирает ссылку на оплату Продамус. Возвращает (url, order_id)."""
    from urllib.parse import urlencode
    plan = SUBSCRIPTION_PLANS[plan_key]
    order_id = f"{user_id}-{plan_key}-{uuid4().hex[:8]}"
    params = {
        "order_id": order_id,
        "products[0][name]": f"Информационные услуги: {plan['name']}",
        "products[0][price]": plan["price"],
        "products[0][quantity]": "1",
        "customer_extra": str(user_id),
        "do": "pay",
    }
    return f"{PRODAMUS_URL}/?{urlencode(params)}", order_id


async def prodamus_webhook(request):
    """Продамус присылает сюда уведомление об оплате. Активируем подписку сами."""
    from aiohttp import web
    try:
        data = dict(await request.post())
        logger.info(f"prodamus webhook: order={data.get('order_id')} status={data.get('payment_status')}")
        if data.get("payment_status") != "success":
            return web.Response(text="OK")
        order_id = data.get("order_id", "")
        parts = order_id.split("-", 2)
        if len(parts) < 2:
            return web.Response(text="OK")
        user_id, plan_key = parts[0], parts[1]
        plan = SUBSCRIPTION_PLANS.get(plan_key)
        if not plan or not user_id.isdigit():
            return web.Response(text="OK")
        user_id = int(user_id)
        # защита: сумма должна совпадать с тарифом
        paid_sum = str(data.get("sum", "")).strip()
        if paid_sum and float(paid_sum) + 0.01 < float(plan["price"]):
            logger.warning(f"prodamus: сумма {paid_sum} меньше тарифа {plan['price']} — не активирую")
            return web.Response(text="OK")
        # не активируем дважды один и тот же заказ
        saved = get_user(user_id) or {}
        if order_id in saved.get("paid_orders", []):
            return web.Response(text="OK")
        grant_subscription(user_id, plan_key)
        data_all = load_user_data()
        data_all.setdefault(str(user_id), {}).setdefault("paid_orders", []).append(order_id)
        save_user_data(data_all)
        bot = request.app["bot"]
        await notify_admin_payment_bot(bot, user_id, plan_key)
        try:
            await bot.send_message(
                user_id,
                f"✅ оплата прошла, всё супер!\n\n{plan['name']} активирована 🎉\n\n"
                f"спасибо за доверие! все прогнозы открыты — жми /start и исследуй себя 🌙"
            )
        except Exception as e:
            logger.warning(f"prodamus notify user {user_id}: {e}")
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"prodamus webhook error: {e}")
        return web.Response(text="OK")


async def start_webhook_server(app_tg):
    """Мини-сервер: health-check для хостинга (всегда) + вебхук Продамуса."""
    try:
        from aiohttp import web

        async def health(request):
            return web.Response(text="alive")

        webapp = web.Application()
        webapp["bot"] = app_tg.bot
        webapp.router.add_get("/", health)
        webapp.router.add_get("/health", health)
        webapp.router.add_post("/prodamus", prodamus_webhook)
        runner = web.AppRunner(webapp)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
        await site.start()
        logger.info(f"web-сервер слушает порт {WEBHOOK_PORT} (health + prodamus webhook)")
    except Exception as e:
        logger.error(f"не удалось поднять web-сервер: {e}")


# ── АСТРО-РАСЧЁТЫ ─────────────────────────────────────────────────
def fmt_position(lon):
    lon = lon % 360
    return SIGNS[int(lon / 30)], round(lon % 30, 1)

def get_house(planet_lon, cusps, orb=2.0):
    planet_lon = planet_lon % 360
    for i in range(12):
        c1 = cusps[i] % 360
        c2 = cusps[(i + 1) % 12] % 360
        if c1 <= c2:
            in_house = c1 <= planet_lon < c2
        else:
            in_house = planet_lon >= c1 or planet_lon < c2
        if in_house:
            dist = (c2 - planet_lon) % 360
            if dist <= orb:
                return (i + 1) % 12 + 1
            return i + 1
    return 1

def get_coordinates(city_name):
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="astro_bushido_bot")
        location = geolocator.geocode(city_name)
        if location:
            return location.latitude, location.longitude
        return None, None
    except Exception:
        return None, None

def calculate_natal_chart(birth_date, birth_time, city, lat=None, lon_geo=None):
    try:
        # координаты берём из сохранённых (быстро и надёжно); геокодер — только если их нет
        if lat is None or lon_geo is None:
            lat, lon_geo = get_coordinates(city)
        if lat is None:
            return {"error": f"не удалось найти город: {city}"}
        tzname = get_tz(lon_geo, lat)
        if not tzname:
            return {"error": "не удалось определить часовой пояс"}
        local_dt = datetime.strptime(f"{birth_date} {birth_time}", "%d.%m.%Y %H:%M")
        local_dt = local_dt.replace(tzinfo=ZoneInfo(tzname))
        utc = local_dt.astimezone(ZoneInfo("UTC"))
        jd = swe.julday(utc.year, utc.month, utc.day, utc.hour + utc.minute / 60)
        cusps, ascmc = swe.houses(jd, lat, lon_geo, b'P')
        planet_ids = {
            "Солнце": swe.SUN, "Луна": swe.MOON, "Меркурий": swe.MERCURY,
            "Венера": swe.VENUS, "Марс": swe.MARS, "Юпитер": swe.JUPITER,
            "Сатурн": swe.SATURN, "Уран": swe.URANUS, "Нептун": swe.NEPTUNE,
            "Плутон": swe.PLUTO,
        }
        chart = {}
        retrograde = []
        for name, pid in planet_ids.items():
            res = swe.calc_ut(jd, pid)[0]
            p_lon, speed = res[0], res[3]
            sign, degree = fmt_position(p_lon)
            house = get_house(p_lon, cusps)
            is_retro = speed < 0
            chart[name] = f"{sign} {degree}°, {house} дом{' ℞' if is_retro else ''}"
            if is_retro:
                retrograde.append((name, sign, degree, house))
        asc_sign, asc_deg = fmt_position(ascmc[0])
        mc_sign, mc_deg = fmt_position(ascmc[1])
        chart["Асцендент"] = f"{asc_sign} {asc_deg}°"
        chart["MC"] = f"{mc_sign} {mc_deg}°"
        chart["_cusps"] = list(cusps)
        chart["_retrograde"] = retrograde
        chart["_lat"] = lat
        chart["_lon"] = lon_geo
        return chart
    except Exception as e:
        return {"error": str(e)}

def chart_for(u):
    """Строит карту из данных пользователя, используя сохранённые координаты."""
    return calculate_natal_chart(
        u.get("birth_date", ""), u.get("birth_time", ""), u.get("birth_city", ""),
        u.get("lat"), u.get("lon"),
    )

def _refine_lunation(lo, hi, target):
    for _ in range(50):
        mid = (lo + hi) / 2
        d = (swe.calc_ut(mid, swe.MOON)[0][0] - swe.calc_ut(mid, swe.SUN)[0][0]) % 360
        if target == 0:
            lo, hi = (mid, hi) if d > 300 else (lo, mid)
        else:
            lo, hi = (mid, hi) if d < 180 else (lo, mid)
    return (lo + hi) / 2

def _lunation_info(jd):
    moon_lon = swe.calc_ut(jd, swe.MOON)[0][0] % 360
    sign, deg = fmt_position(moon_lon)
    y, m, d, h = swe.revjul(jd)
    return sign, deg, f"{int(d):02d}.{int(m):02d}.{int(y)}"

def find_lunations_year():
    global _LUNATIONS_CACHE, _LUNATIONS_CACHE_DATE
    today = datetime.now(timezone.utc).date()
    if _LUNATIONS_CACHE is not None and _LUNATIONS_CACHE_DATE == today:
        return _LUNATIONS_CACHE
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=400)
    jd = swe.julday(start.year, start.month, start.day, 0)
    jd_end = swe.julday(end.year, end.month, end.day, 0)
    results, prev_diff, t = [], None, jd
    while t < jd_end:
        sun_lon = swe.calc_ut(t, swe.SUN)[0][0]
        moon_lon = swe.calc_ut(t, swe.MOON)[0][0]
        diff = (moon_lon - sun_lon) % 360
        if prev_diff is not None:
            if prev_diff > 300 and diff < 60:
                nm_jd = _refine_lunation(t - 0.5, t, 0)
                sign, deg, ds = _lunation_info(nm_jd)
                results.append(("НЛ", ds, sign, deg, nm_jd))
            elif prev_diff < 180 <= diff:
                fm_jd = _refine_lunation(t - 0.5, t, 180)
                sign, deg, ds = _lunation_info(fm_jd)
                results.append(("ПЛ", ds, sign, deg, fm_jd))
        prev_diff = diff
        t += 0.5
    results.sort(key=lambda x: x[4])
    _LUNATIONS_CACHE = results
    _LUNATIONS_CACHE_DATE = today
    return results

def find_eclipses_year():
    """Все солнечные и лунные затмения на ~13 месяцев вперёд (Swiss Ephemeris)."""
    global _ECLIPSES_CACHE, _ECLIPSES_CACHE_DATE
    today = datetime.now(timezone.utc).date()
    if _ECLIPSES_CACHE is not None and _ECLIPSES_CACHE_DATE == today:
        return _ECLIPSES_CACHE
    now = datetime.now(timezone.utc)
    jd_start = swe.julday(now.year, now.month, now.day, 0)
    jd_end = jd_start + 400
    raw = []
    # солнечные затмения
    t = jd_start
    for _ in range(12):
        try:
            retflag, tret = swe.sol_eclipse_when_glob(t, swe.FLG_SWIEPH, 0, False)
        except Exception:
            break
        jd_max = tret[0]
        if jd_max > jd_end:
            break
        if retflag & swe.ECL_TOTAL:
            kind = "полное"
        elif retflag & swe.ECL_ANNULAR:
            kind = "кольцевое"
        else:
            kind = "частичное"
        raw.append(("СЗ", kind, jd_max))
        t = jd_max + 1
    # лунные затмения
    t = jd_start
    for _ in range(12):
        try:
            retflag, tret = swe.lun_eclipse_when(t, swe.FLG_SWIEPH, 0, False)
        except Exception:
            break
        jd_max = tret[0]
        if jd_max > jd_end:
            break
        if retflag & swe.ECL_TOTAL:
            kind = "полное"
        elif retflag & swe.ECL_PARTIAL:
            kind = "частичное"
        else:
            kind = "полутеневое"
        raw.append(("ЛЗ", kind, jd_max))
        t = jd_max + 1
    # позиции: солнечное = точка Солнца/Луны (новолуние), лунное = точка Луны (полнолуние)
    out = []
    for etype, kind, jd_max in raw:
        body = swe.SUN if etype == "СЗ" else swe.MOON
        lon = swe.calc_ut(jd_max, body)[0][0] % 360
        sign, deg = fmt_position(lon)
        y, m, d, h = swe.revjul(jd_max)
        date_str = f"{int(d):02d}.{int(m):02d}.{int(y)}"
        out.append((etype, kind, date_str, sign, deg, jd_max, lon))
    out.sort(key=lambda x: x[5])
    _ECLIPSES_CACHE = out
    _ECLIPSES_CACHE_DATE = today
    return out

def find_mercury_retro():
    now = datetime.now(timezone.utc)
    jd = swe.julday(now.year, now.month, now.day, now.hour + now.minute / 60)
    speed_now = swe.calc_ut(jd, swe.MERCURY)[0][3]
    if speed_now < 0:
        retro_start_jd = jd - 30
        t = jd
        for _ in range(60):
            t -= 1
            if swe.calc_ut(t, swe.MERCURY)[0][3] >= 0:
                retro_start_jd = t; break
        retro_end_jd = None
        t = jd
        for _ in range(60):
            t += 1
            if swe.calc_ut(t, swe.MERCURY)[0][3] >= 0:
                retro_end_jd = t; break
        is_current = True
    else:
        retro_start_jd = retro_end_jd = None
        t, prev_spd = jd, speed_now
        for _ in range(200):
            t += 0.5
            spd = swe.calc_ut(t, swe.MERCURY)[0][3]
            if prev_spd >= 0 and spd < 0 and retro_start_jd is None:
                retro_start_jd = t
            if retro_start_jd and prev_spd < 0 and spd >= 0:
                retro_end_jd = t; break
            prev_spd = spd
        is_current = False
    if not retro_start_jd or not retro_end_jd:
        return None
    def jd_to_str(j):
        y, m, d, h = swe.revjul(j)
        return f"{int(d):02d}.{int(m):02d}.{int(y)}"
    start_lon = swe.calc_ut(retro_start_jd, swe.MERCURY)[0][0]
    end_lon = swe.calc_ut(retro_end_jd, swe.MERCURY)[0][0]
    s_sign, s_deg = fmt_position(start_lon)
    e_sign, e_deg = fmt_position(end_lon)
    return {
        "is_current": is_current,
        "start_date": jd_to_str(retro_start_jd), "end_date": jd_to_str(retro_end_jd),
        "start_sign": s_sign, "start_deg": s_deg, "end_sign": e_sign, "end_deg": e_deg,
        "start_jd": retro_start_jd, "end_jd": retro_end_jd,
    }

def get_moon_event_by_jd(event_jd, cusps):
    moon_lon = swe.calc_ut(event_jd, swe.MOON)[0][0] % 360
    sign, degree = fmt_position(moon_lon)
    house = get_house(moon_lon, cusps)
    y, m, d, h = swe.revjul(event_jd)
    return moon_lon, f"{int(d):02d}.{int(m):02d}.{int(y)}", sign, degree, house

def get_sun_transit(cusps):
    """Где сейчас транзитное солнце: знак, градус, дом натальной карты."""
    now = datetime.now(timezone.utc)
    jd = swe.julday(now.year, now.month, now.day, now.hour + now.minute / 60)
    sun_lon = swe.calc_ut(jd, swe.SUN)[0][0] % 360
    sign, degree = fmt_position(sun_lon)
    house = get_house(sun_lon, cusps)
    return sun_lon, sign, degree, house


# ── CLAUDE ────────────────────────────────────────────────────────
async def call_claude(system_prompt, user_prompt, max_tokens=1400):
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                HYDRA_API_URL,
                headers={"Authorization": f"Bearer {HYDRA_API_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL, "max_tokens": max_tokens,
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user", "content": user_prompt}]}
            )
            data = response.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            return f"ошибка api: {data}"
    except Exception as e:
        return f"ошибка подключения: {str(e)}"

SYSTEM_ASTRO = """ты — астролог катерина. пишешь короткий прогноз на фазу луны.

⛔️ СТРОГО ЗАПРЕЩЕНО:
• анализировать всю натальную карту — это не твоя задача здесь.
• перечислять планеты по домам (4 дом, 10 дом и т.д.) — не надо!
• выдумывать названия знаков зодиака. используй ТОЛЬКО эти 12 знаков: Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы.
• обращаться иначе, чем написал пользователь. если написали "Екатерина" — обращайся "Екатерина", не "Катя".

✅ ТВОЯ ЗАДАЧА:
• рассказать что означает ЭТА конкретная фаза луны (знак + дом куда она падает).
• если в доме фазы луны есть натальные планеты — упомяни их коротко (это добавляет глубину).
• НЕ анализировать остальные дома и планеты карты.

═══ ТОН ═══
• по-русски, с маленькой буквы, на ты.
• тепло и конкретно. короткие живые предложения.
• как подруга-астролог, не академический разбор.
• НО: ты не утешаешь. ты будишь. тёплая — не значит удобная.

═══ ЯСНОСТЬ ВМЕСТО УТЕШЕНИЯ (важно!) ═══
• твоя задача — не успокоить и не пнуть, а ПРОЯСНИТЬ. открыть глаза — мягко.
• трансформация происходит, когда человек относится к себе с поддержкой — и к тёмным сторонам, и к светлым. поэтому неудобное называй с теплом, без обвинения: не «ты избегаешь», а «в этой теме часто есть то, на что трудно смотреть — и это нормально».
• пустые утешения запрещены: «всё будет хорошо», «доверься процессу», «вселенная поддержит» — они ничего не проясняют. но ВЕРА уместна: «это можно прожить», «ты справлялась со сложным — и это пройдёшь». вера — мягкое и сильное одновременно, не сироп.
• не давай готовых выводов — оставляй пространство додумать. недосказанность — это приглашение, а не обрыв.
• один вопрос — ПРОЯСНЯЮЩИЙ, глубокий. не пинок в лоб, а открывающий глаза: «что станет видно, если посмотреть на это честно?», «что ты уже знаешь, но пока не готова себе сказать?», «чему в этой сфере пора дать место?»

═══ ФОРМАТ (строго) ═══
• начинай с: "твой прогноз по [новолунию/полнолунию] в [знак] на [период]"
• 2-3 коротких абзаца про эту фазу луны.
• в конце — 3 вопроса для рефлексии:
  1. вопрос про эту сферу жизни (дом фазы)
  2. один ПРОЯСНЯЮЩИЙ вопрос — глубокий, открывающий глаза (см. выше)
  3. ОБЯЗАТЕЛЬНО про телесные ощущения — где в теле чувствуешь это напряжение или лёгкость?
• после вопросов — по ситуации: если тема тяжёлая, можно ОДНУ короткую строку опоры (вера, не утешение: «это можно прожить»). если нет — оставь тишину, пусть вопросы работают.
• весь ответ — максимум 3 абзаца + 3 вопроса. не больше."""

async def get_astro_forecast(name, chart, lunation_jd, lunation_type):
    cusps = chart.pop("_cusps", None)
    retrograde = chart.pop("_retrograde", None) or []
    chart.pop("_lat", None); chart.pop("_lon", None)
    moon_lon, date_s, sign, deg, house = get_moon_event_by_jd(lunation_jd, cusps)
    type_name = "🌑 новолуние" if lunation_type == "НЛ" else "🌕 полнолуние"
    lunation_text = f"{type_name} {date_s} — луна в {sign} {deg}°, {house} дом"
    chart_text = "\n".join(f"  {k}: {v}" for k, v in chart.items())
    query = build_query(lunation_type, sign, house, [r[0] for r in retrograde] + list(chart.keys())[:4])
    source_context = await asyncio.to_thread(retrieve, query)
    sources_block = f"\n\n═══ АВТОРСКИЕ МАТЕРИАЛЫ (приоритет) ═══\n{source_context}" if source_context else ""
    return await call_claude(SYSTEM_ASTRO + sources_block, f"""имя: {name}
натальная карта (плацидус):
{chart_text}

фаза луны: {lunation_text}

составь короткий персональный разбор (макс 3 абзаца). заверши 3 вопросами для рефлексии — последний обязательно про телесные ощущения.""")

async def get_retrograde_block(name, retrograde):
    personal = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    if not personal:
        return None
    retro_list = "\n".join(f"  {n}: {s} {d}°, {h} дом ℞" for n, s, d, h in personal)
    system = """ты астролог катя. пишешь короткий бонус-блок про ретроградные личные планеты в натале.

формула: два шага вперёд, один назад. не слабость — глубина и осмысленность.
меркурий℞ — переформулирует прежде чем сказать. венера℞ — нестандартный взгляд на близость. марс℞ — действует через паузу.

формат: по-русски, по имени, на ты, тепло, коротко — 2 небольших абзаца. как вау-открытие про скрытую силу.
в конце один вопрос — как ты чувствуешь это в своей жизни и в теле прямо сейчас?"""
    return await call_claude(system, f"имя: {name}\nретро личные планеты:\n{retro_list}\n\nнапиши бонус-блок про скрытую силу через формулу «два шага вперёд, один назад».", max_tokens=500)

async def get_mercury_retro_forecast(name, chart, retro_info):
    cusps = chart.get("_cusps")
    if not cusps:
        return "не удалось рассчитать дома."
    start_lon = swe.calc_ut(retro_info["start_jd"], swe.MERCURY)[0][0]
    end_lon = swe.calc_ut(retro_info["end_jd"], swe.MERCURY)[0][0]
    start_house = get_house(start_lon % 360, cusps)
    end_house = get_house(end_lon % 360, cusps)
    status = "сейчас идёт" if retro_info["is_current"] else "предстоит"
    period = f"{retro_info['start_date']} — {retro_info['end_date']}"
    chart_text = "\n".join(f"  {k}: {v}" for k, v in chart.items() if not k.startswith("_"))
    merc_query = f"ретроградный меркурий транзит {retro_info['start_sign']} {start_house} дом"
    source_context = await asyncio.to_thread(retrieve, merc_query)
    sources_block = f"\n\n═══ АВТОРСКИЕ МАТЕРИАЛЫ (приоритет) ═══\n{source_context}" if source_context else ""
    system = """ты — астролог катерина. пишешь прогноз на период ретроградного меркурия.

⛔️ СТРОГО:
• используй ТОЛЬКО эти 12 знаков: Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы. никаких других названий.
• не анализируй всю натальную карту. только про меркурий ретро и через какой дом он идёт.
• обращайся точно тем именем, которое написал пользователь.

✅ ЗАДАЧА:
• объяснить что значит этот период ретро меркурия именно для этого человека — через какой дом он проходит.
• практические советы на этот период.
• если натальный меркурий тоже ретро — человек легче проходит этот период, упомяни.

═══ ФОРМАТ ═══
• начинай с: "твой прогноз на ретроградный меркурий [период]"
• 2-3 коротких абзаца. тепло, конкретно, на ты.
• в конце 3 вопроса для рефлексии: один по теме, один ПРОЯСНЯЮЩИЙ (открывающий глаза, не пинающий: «что ты уже знаешь, но пока не готова себе сказать?»), последний — про телесные ощущения.
• пустые утешения запрещены («всё будет хорошо», «доверься процессу»), но вера уместна: «это можно прожить». не давай готовых выводов — оставляй пространство додумать.""" + sources_block
    user = f"""имя: {name}
натальная карта:
{chart_text}

транзитный меркурий ℞ ({status}):
период: {period}
начало: {retro_info['start_sign']} {retro_info['start_deg']}°, {start_house} дом
конец: {retro_info['end_sign']} {retro_info['end_deg']}°, {end_house} дом"""
    return await call_claude(system, user)

async def get_sun_transit_forecast(name, chart):
    cusps = chart.get("_cusps")
    if not cusps:
        return "не удалось рассчитать дома."
    sun_lon, sign, deg, house = get_sun_transit(cusps)
    # планеты в этом же доме (из натальной карты)
    planets_here = []
    for k, v in chart.items():
        if k.startswith("_") or k in ("Асцендент", "MC"):
            continue
        if f", {house} дом" in v:
            planets_here.append(k)
    planets_text = (
        f"в этом доме есть натальные планеты: {', '.join(planets_here)}"
        if planets_here else "натальных планет в этом доме нет"
    )
    source_context = await asyncio.to_thread(retrieve, f"солнце {house} дом транзит солнца тема дома")
    sources_block = f"\n\n═══ АВТОРСКИЕ МАТЕРИАЛЫ (приоритет) ═══\n{source_context}" if source_context else ""
    system = """ты — астролог катерина. пишешь короткий прогноз про движение солнца сейчас (транзит солнца по дому).

⛔️ СТРОГО:
• используй ТОЛЬКО эти 12 знаков: Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы.
• не анализируй всю карту. только про солнце и дом, в котором оно сейчас.
• обращайся точно тем именем, которое написал пользователь.

✅ ЗАДАЧА:
• солнце сейчас проходит через определённый дом — расскажи какие темы этого дома поднимаются и подсвечиваются.
• если в этом доме есть натальные планеты — упомяни коротко (это усиливает темы). если нет — так и скажи.

⚠️ номер дома уже указан пользователю отдельно — НЕ называй другой номер дома, используй ровно тот, что дан в данных ниже. не пиши «в 5 доме» если дано 7.

═══ ФОРМАТ ═══
• не повторяй номер дома в начале (он уже показан). сразу переходи к темам этого дома.
• 2-3 коротких абзаца. тепло, на ты, с маленькой буквы.
• в конце 3 вопроса: про эту сферу, один ПРОЯСНЯЮЩИЙ (чему в этой теме пора дать место?), и про телесные ощущения.
• пустые утешения запрещены, но вера уместна: «это можно прожить». оставляй пространство додумать самой.""" + sources_block
    user = f"""имя: {name}
транзитное солнце сейчас: {sign} {deg}°, {house} дом натальной карты
{planets_text}

составь короткий прогноз про темы этого дома."""
    return await call_claude(system, user)

async def get_eclipse_forecast(name, chart, ecl):
    etype, kind, date_str, sign, deg, jd_max, lon = ecl
    cusps = chart.get("_cusps")
    if not cusps:
        return "не удалось рассчитать дома."
    house = get_house(lon, cusps)
    planets_here = [
        k for k, v in chart.items()
        if not k.startswith("_") and k not in ("Асцендент", "MC") and f", {house} дом" in v
    ]
    planets_text = (
        f"в этом доме есть натальные планеты: {', '.join(planets_here)} — затмение заденет их темы напрямую"
        if planets_here else "натальных планет в этом доме нет"
    )
    type_full = "солнечное затмение (усиленное новолуние)" if etype == "СЗ" else "лунное затмение (усиленное полнолуние)"
    source_context = await asyncio.to_thread(retrieve, f"затмение {sign} {house} дом луна солнце")
    sources_block = f"\n\n═══ АВТОРСКИЕ МАТЕРИАЛЫ (приоритет) ═══\n{source_context}" if source_context else ""
    system = """ты — астролог катерина. пишешь короткий персональный прогноз на затмение.

⛔️ СТРОГО:
• используй ТОЛЬКО эти 12 знаков: Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы.
• не анализируй всю карту — только точку затмения и дом, куда она попадает.
• обращайся точно тем именем, которое написал пользователь.
• НЕ пугай: никакой кармы-фатальности, «судьба решит за тебя», «опасный период». затмение — не приговор.

✅ СУТЬ:
• солнечное затмение = усиленное новолуние: в теме дома закрывается старая дверь и открывается новая. старт с глубокой перенастройкой.
• лунное затмение = усиленное полнолуние: кульминация, на свет выходит то, что зрело месяцами. видно то, что было скрыто.
• затмения — поворотные точки: события в этой сфере ускоряются, решения имеют больший вес.
• если в доме затмения есть натальные планеты — темы этих планет включаются напрямую, скажи об этом.

═══ ТОН ═══
• по-русски, с маленькой буквы, на ты. тепло, ясно, без страшилок и без сиропа.
• пустые утешения запрещены, но вера уместна: «это можно прожить».
• не давай готовых выводов — оставляй пространство додумать.

═══ ФОРМАТ ═══
• 2-3 коротких абзаца.
• в конце 3 вопроса: про сферу этого дома, один ПРОЯСНЯЮЩИЙ (что в этой теме давно просит перемены?), и про телесные ощущения.""" + sources_block
    user = f"""имя: {name}
затмение: {type_full}, {kind}, {date_str} — {sign} {deg}°, {house} дом натальной карты
{planets_text}

составь персональный прогноз на это затмение."""
    return await call_claude(system, user)


# ── ПЕЙВОЛЛ ───────────────────────────────────────────────────────
async def show_paywall(message, user_id, edit=False):
    used = get_free_used(user_id)
    limit = get_free_limit(user_id)
    text = (
        f"🌙 ты использовала все {limit} бесплатных прогноза\n\n"
        "можешь поделиться со мной отзывом — написать кате — или выбери подписку\n\n"
        "везучих и счастливых прогнозов\n\n"
        + "\n".join(f"*{p['name']}* — {p['label']}\n{p['desc']}" for p in SUBSCRIPTION_PLANS.values())
    )
    keyboard = [
        [InlineKeyboardButton(f"{p['name']} — {p['label']}", callback_data=f"buy_plan_{k}")]
        for k, p in SUBSCRIPTION_PLANS.items()
    ]
    keyboard.append([InlineKeyboardButton("← назад", callback_data="back_to_menu")])
    mk = InlineKeyboardMarkup(keyboard)
    if edit:
        await message.edit_text(text, reply_markup=mk, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=mk, parse_mode="Markdown")


# ── АВТОМАТИЧЕСКОЕ УВЕДОМЛЕНИЕ О НОВОЛУНИИ ───────────────────────
async def send_newmoon_notifications(context: ContextTypes.DEFAULT_TYPE):
    """Запускается ежедневно. Если через 3 дня новолуние — шлёт уведомление всем пользователям."""
    now = datetime.now(timezone.utc)
    target_date = (now + timedelta(days=3)).date()
    all_lun = find_lunations_year()
    upcoming_nm = None
    for ltype, date_str, sign, deg, jd in all_lun:
        if ltype == "НЛ":
            y, m, d, h = swe.revjul(jd)
            lun_date = datetime(int(y), int(m), int(d), tzinfo=timezone.utc).date()
            if lun_date == target_date:
                upcoming_nm = (date_str, sign, deg, jd)
                break
    if not upcoming_nm:
        return
    date_str, sign, deg, jd = upcoming_nm
    sign_e = SIGNS_EMOJI.get(sign, "")
    text = (
        f"🌑 через 3 дня — новолуние в {sign_e}{sign} {deg}°\n\n"
        f"это твой персональный старт нового цикла. каждое новолуние — маленькое обновление, новый посев.\n\n"
        f"нажми, чтобы получить свой прогноз 👇"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌙 мой прогноз на новолуние", callback_data=f"nm_notify_{all_lun.index((ltype, date_str, sign, deg, jd))}")
    ]])
    data = load_user_data()
    for uid_str, udata in data.items():
        if not udata.get("birth_date"):
            continue
        last_nm = udata.get("last_nm_notified")
        if last_nm == date_str:
            continue
        try:
            await context.bot.send_message(int(uid_str), text, reply_markup=keyboard)
            data[uid_str]["last_nm_notified"] = date_str
        except Exception as e:
            logger.warning(f"не удалось отправить уведомление {uid_str}: {e}")
    save_user_data(data)


# ── УВЕДОМЛЕНИЕ О ПЕРЕХОДЕ СОЛНЦА В НОВЫЙ ДОМ ────────────────────
async def send_sun_house_notifications(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневно. Если у пользователя транзитное солнце перешло в новый дом — шлёт уведомление."""
    data = load_user_data()
    changed = False
    for uid_str, udata in data.items():
        if not udata.get("birth_date"):
            continue
        chart = chart_for(udata)
        if "error" in chart:
            continue
        cusps = chart.get("_cusps")
        if not cusps:
            continue
        _, sign, deg, house = get_sun_transit(cusps)
        prev_house = udata.get("last_sun_house")
        if prev_house == house:
            continue
        # первый расчёт — просто запоминаем, не спамим
        data[uid_str]["last_sun_house"] = house
        changed = True
        if prev_house is None:
            continue
        name = udata.get("name", "")
        planets_here = [
            k for k, v in chart.items()
            if not k.startswith("_") and k not in ("Асцендент", "MC") and f", {house} дом" in v
        ]
        planets_line = (
            f"и тут у тебя есть планеты: {', '.join(planets_here)} — темы будут ярче"
            if planets_here else "натальных планет в этом доме нет — но темы всё равно подсветятся"
        )
        text = (
            f"☀️ {name}, твоё солнце перешло в {house} дом!\n\n"
            f"возможно, поднимутся темы этого дома. {planets_line}.\n\n"
            f"чувствуешь ли ты сдвиг в эту сторону? как откликается тело?\n\n"
            f"нажми, чтобы получить полный разбор 👇"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("☀️ мой прогноз по солнцу", callback_data="menu_sun")
        ]])
        try:
            await context.bot.send_message(int(uid_str), text, reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"sun-notify {uid_str}: {e}")
    if changed:
        save_user_data(data)


# ── НАПОМИНАЛКА ПОСЛЕ ОКОНЧАНИЯ БЕСПЛАТНЫХ ───────────────────────
async def send_retention_messages(context: ContextTypes.DEFAULT_TYPE):
    """Раз в неделю шлёт тёплое сообщение тем, у кого кончились бесплатные прогнозы."""
    data = load_user_data()
    now = datetime.now(timezone.utc)
    for uid_str, udata in data.items():
        if not udata.get("birth_date"):
            continue
        used = udata.get("free_forecasts_used", 0)
        limit = FREE_FORECASTS_LIMIT + udata.get("bonus_forecasts", 0)
        if used < limit:
            continue
        last_sent = udata.get("last_retention_at")
        if last_sent:
            last_dt = datetime.fromisoformat(last_sent)
            if (now - last_dt).days < 14:
                continue
        name = udata.get("name", "")
        text = (
            f"{name}, как ты? 🌙\n\n"
            "как прошёл этот лунный месяц?\n"
            "что-то изменилось, или всё идёт по-прежнему?\n\n"
            "останови на минуту и почувствуй — как сейчас твоё тело? где напряжение, где лёгкость?\n\n"
            "если захочешь продолжить исследовать себя через астрологию — я здесь 💫"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 посмотреть подписки", callback_data="menu_buy")],
            [InlineKeyboardButton("💬 написать кате", url=f"https://t.me/{KATYA_TG}")]
        ])
        try:
            await context.bot.send_message(int(uid_str), text, reply_markup=keyboard)
            data[uid_str]["last_retention_at"] = now.isoformat()
        except Exception as e:
            logger.warning(f"retention {uid_str}: {e}")
    save_user_data(data)


# ── ГЛАВНОЕ МЕНЮ ─────────────────────────────────────────────────
async def show_main_menu(message, context, name=None, edit=False):
    if name is None:
        name = context.user_data.get("name", "")
    has_retro = context.user_data.get("has_retro_personal")
    keyboard = [
        [InlineKeyboardButton("🌙 фазы луны", callback_data="menu_lunation")],
        [InlineKeyboardButton("☀️ движение солнца сейчас", callback_data="menu_sun")],
        [InlineKeyboardButton("🌘 затмения", callback_data="menu_eclipse")],
        [InlineKeyboardButton("☿ ретроградный меркурий", callback_data="menu_mercury")],
    ]
    if has_retro:
        keyboard.append([InlineKeyboardButton("⚡️ забери свой бонус", callback_data="menu_bonus")])
    keyboard += [
        [InlineKeyboardButton("🔗 поделиться с другом — получи прогноз", callback_data="menu_referral")],
        [InlineKeyboardButton("🛒 подписки", callback_data="menu_buy")],
        [InlineKeyboardButton("💬 служба заботы — написать кате", url=f"https://t.me/{KATYA_TG}")],
    ]
    text = f"привет, {name}! что хочешь узнать? 🔮"
    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ── HANDLERS ─────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    args = context.args or []

    # реферал
    if args and args[0].startswith("ref_"):
        referrer_id = args[0][4:]
        if referrer_id != str(user_id) and not get_user(user_id):
            add_bonus_forecast(referrer_id)


    # возврат после оплаты — берём тариф из сохранённого pending (надёжно)
    if args and args[0].startswith("paid_"):
        saved = get_user(user_id) or {}
        pid = saved.get("pending_payment_id")
        plan_key = saved.get("pending_plan_key")
        if pid and plan_key and check_payment(pid):
            grant_subscription(user_id, plan_key)
            await notify_admin_payment(context, user_id, plan_key)
            plan = SUBSCRIPTION_PLANS.get(plan_key, {})
            await update.message.reply_text(
                f"✅ *оплата прошла, всё супер!*\n\n"
                f"*{plan.get('name','')}* активирована 🎉\n\n"
                f"спасибо за доверие! теперь все прогнозы открыты — исследуй себя 🌙",
                parse_mode="Markdown"
            )
            context.user_data.update(get_user(user_id) or {})
            await show_main_menu(update.message, context)
            return MAIN_MENU

    saved = get_user(user_id)
    if saved:
        context.user_data.update(saved)
        keyboard = [
            [InlineKeyboardButton(f"✅ да, я {saved['name']}", callback_data="use_saved")],
            [InlineKeyboardButton("🔄 изменить данные", callback_data="new_data")]
        ]
        await update.message.reply_text(
            f"с возвращением! 🌙\n\nтвои данные: *{saved['name']}*, {saved['birth_date']}, {saved['birth_time']}, {saved['birth_city']}\n\nиспользовать их?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return MAIN_MENU

    # новый пользователь — 2 отдельных сообщения
    await update.message.reply_text(
        "привет, друг! 🌙 на связи катерина.\n\n"
        "это мой бот, где можно настроиться на космические ритмы, задать себе вопросы и отслеживать как астрология проявляется в твоей жизни каждый месяц ✨\n\n"
        "сама много лет наблюдаю за этим — это улучшает моё состояние.\n\n"
        "здесь у тебя будет персональный, точный прогноз по твоим данным, информация что происходит и на что обращать внимание.\n\n"
        "рекомендую сонастраиваться с прогнозами, как они ощущаются в теле, выдыхай и сканируй себя — это заставит любой прогноз работать на тебя."
    )
    await update.message.reply_text(
        "спасибо за твой интерес, как я могу к тебе обращаться?\n\n"
        f"оставляя имя, ты соглашаешься на [обработку персональных данных]({PDPA_URL}), "
        f"принимаешь [политику конфиденциальности]({PRIVACY_POLICY_URL}) "
        f"и [договор публичной оферты]({OFERTA_URL}).",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    return NAME

async def handle_saved_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "use_saved":
        await show_main_menu(query.message, context, edit=True)
        return MAIN_MENU
    await query.edit_message_text("хорошо, введём заново. как тебя зовут?")
    return NAME

def _cache_retro_flag(user_id, context, retrograde):
    personal = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    has_retro = bool(personal)
    context.user_data["has_retro_personal"] = has_retro
    saved = get_user(user_id) or {}
    saved["has_retro_personal"] = has_retro
    save_user(user_id, saved)

async def show_buy_menu(message, context, user_id, edit=False):
    unlimited = has_active_unlimited(user_id)
    keyboard = []
    for k, p in SUBSCRIPTION_PLANS.items():
        # безлимит активен — год/месяц показываем как активные
        if unlimited and k in ("full_year", "month"):
            keyboard.append([InlineKeyboardButton(f"✅ {p['name']} — активна", callback_data="noop")])
        else:
            keyboard.append([InlineKeyboardButton(f"{p['name']} — {p['label']}", callback_data=f"buy_plan_{k}")])
    keyboard.append([InlineKeyboardButton("← назад", callback_data="back_to_menu")])
    used = get_free_used(user_id)
    limit = get_free_limit(user_id)
    if unlimited:
        text = "🛒 *подписки*\n\n✨ у тебя активен безлимит — все прогнозы доступны.\n\nможно докупить:"
    else:
        text = (
            f"🛒 *подписки*\n\n"
            f"бесплатно использовано: {used} из {limit} прогнозов\n\n"
            "расширенные возможности:\n\n"
            "у меня в планах добавить затмения, другие планеты и модуль с human design, "
            "а из ближайшего — осенью нас ожидает ретроградная венера"
        )
    mk = InlineKeyboardMarkup(keyboard)
    if edit:
        await message.edit_text(text, reply_markup=mk, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=mk, parse_mode="Markdown")
    return MAIN_MENU

async def show_lunation_choice(message, context, edit=False, page=0):
    all_lun = find_lunations_year()
    total = len(all_lun)
    total_pages = max(1, (total + LUNATIONS_PER_PAGE - 1) // LUNATIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * LUNATIONS_PER_PAGE
    keyboard = []
    for i in range(start_idx, min(start_idx + LUNATIONS_PER_PAGE, total)):
        ltype, date, sign, deg, jd = all_lun[i]
        emoji = "🌑" if ltype == "НЛ" else "🌕"
        sign_e = SIGNS_EMOJI.get(sign, "")
        keyboard.append([InlineKeyboardButton(f"{emoji} {date} {sign_e}{sign} {deg}°", callback_data=f"lun_{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← назад", callback_data=f"lun_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("вперёд →", callback_data=f"lun_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("← меню", callback_data="back_to_menu")])
    text = f"🔮 выбери фазу луны:\n_страница {page+1} из {total_pages}_"
    mk = InlineKeyboardMarkup(keyboard)
    if edit:
        await message.edit_text(text, reply_markup=mk, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=mk, parse_mode="Markdown")
    return MAIN_MENU

async def show_eclipse_choice(message, context, edit=False):
    ecls = find_eclipses_year()
    keyboard = []
    for i, e in enumerate(ecls):
        etype, kind, date, sign, deg, jd, lon = e
        emoji = "🌑" if etype == "СЗ" else "🌕"
        tname = "солнечное" if etype == "СЗ" else "лунное"
        sign_e = SIGNS_EMOJI.get(sign, "")
        keyboard.append([InlineKeyboardButton(f"{emoji} {date} {tname} {sign_e}{sign} {deg}°", callback_data=f"ecl_{i}")])
    keyboard.append([InlineKeyboardButton("← меню", callback_data="back_to_menu")])
    text = (
        "🌘 ближайшие затмения — выбери для персонального разбора:\n\n"
        "_затмения — усиленные новолуния и полнолуния, поворотные точки года_"
    )
    mk = InlineKeyboardMarkup(keyboard)
    if edit:
        await message.edit_text(text, reply_markup=mk, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=mk, parse_mode="Markdown")
    return MAIN_MENU

async def handle_eclipse_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = update.effective_user.id
    idx = int(query.data.split("_")[1])
    ecls = find_eclipses_year()
    if idx >= len(ecls):
        await query.edit_message_text("что-то пошло не так. /start")
        return MAIN_MENU
    if not can_get_free_forecast(user_id):
        await show_paywall(query.message, user_id)
        return MAIN_MENU
    ecl = ecls[idx]
    etype, kind, date_str, sign, deg, jd_max, lon = ecl
    tname = "🌑 солнечное затмение" if etype == "СЗ" else "🌕 лунное затмение"
    name = context.user_data.get("name", "")
    await query.edit_message_text(
        f"выбрано: {tname} ({kind}) {date_str} — {sign} {deg}°\n\n✨ считаю карту и готовлю разбор...\n🔮 займёт около минуты"
    )
    chart = chart_for(context.user_data)
    if "error" in chart:
        await query.message.reply_text(f"ошибка расчёта: {chart['error']}\n/start")
        return MAIN_MENU
    _cache_retro_flag(user_id, context, chart.get("_retrograde", []))
    increment_forecast_counter(user_id)
    # дом затмения — из кода, факт
    house = get_house(lon, chart.get("_cusps"))
    header = f"{tname} ({kind})\n📅 {date_str} — {SIGNS_EMOJI.get(sign,'')}{sign} {deg}°, *{house} дом* твоей карты\n\n"
    forecast = await get_eclipse_forecast(name, chart, ecl)
    await query.message.reply_text(header + forecast, parse_mode="Markdown")
    used = get_free_used(user_id)
    limit = get_free_limit(user_id)
    remaining = limit - used
    keyboard = [
        [InlineKeyboardButton("🌘 другое затмение", callback_data="menu_eclipse")],
        [InlineKeyboardButton("← главное меню", callback_data="back_to_menu")],
    ]
    footer = f"\n\n_осталось бесплатных прогнозов: {max(0, remaining)}_"
    await query.message.reply_text("хочешь перейти в другой прогноз?" + footer, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MAIN_MENU

async def handle_lunation_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = update.effective_user.id
    # callback может быть lun_N или nm_notify_N
    idx = int(query.data.split("_")[-1])
    all_lun = find_lunations_year()
    if idx >= len(all_lun):
        await query.edit_message_text("что-то пошло не так. /start")
        return ConversationHandler.END

    # новолунные уведомления — всегда бесплатно
    is_nm_notify = query.data.startswith("nm_notify_")
    ltype = all_lun[idx][0]
    is_free_nm = is_nm_notify and ltype == "НЛ"

    if not is_free_nm and not can_get_free_forecast(user_id):
        await show_paywall(query.message, user_id)
        return MAIN_MENU

    ltype, date, sign, deg, jd = all_lun[idx]
    type_name = "🌑 новолуние" if ltype == "НЛ" else "🌕 полнолуние"
    name = context.user_data.get("name", "")

    await query.edit_message_text(
        f"выбрано: {type_name} {date} — {sign} {deg}°\n\n✨ считаю карту и готовлю разбор...\n🔮 займёт около минуты"
    )

    chart = chart_for(context.user_data)
    if "error" in chart:
        await query.message.reply_text(f"ошибка расчёта: {chart['error']}\n/start")
        return ConversationHandler.END

    retrograde = chart.get("_retrograde", [])
    personal_retro = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    _cache_retro_flag(user_id, context, retrograde)

    if not is_free_nm:
        increment_forecast_counter(user_id)

    forecast = await get_astro_forecast(name, chart, jd, ltype)
    await query.message.reply_text(forecast)

    used = get_free_used(user_id)
    limit = get_free_limit(user_id)
    remaining = limit - used

    keyboard = [[InlineKeyboardButton("🔮 другая фаза луны", callback_data="another_lunation")]]
    if personal_retro:
        keyboard.append([InlineKeyboardButton("⚡️ забери свой бонус", callback_data="menu_bonus")])
    keyboard.append([InlineKeyboardButton("← главное меню", callback_data="back_to_menu")])

    if not is_free_nm:
        footer = f"\n\n_осталось бесплатных прогнозов: {max(0, remaining)}_"
    else:
        footer = "\n\n_этот прогноз всегда бесплатный — подарок к каждому новолунию 🌙_"
    await query.message.reply_text("хочешь перейти в другой прогноз?" + footer, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MAIN_MENU

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "noop":
        return MAIN_MENU

    elif query.data == "menu_referral":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        used = get_free_used(user_id)
        limit = get_free_limit(user_id)
        keyboard = [[InlineKeyboardButton("← назад", callback_data="back_to_menu")]]
        await query.edit_message_text(
            f"🔗 *твоя реферальная ссылка*\n\n`{ref_link}`\n\n"
            f"поделись с подругой — когда она зарегистрируется, ты получишь +1 бесплатный прогноз 🎁\n\n"
            f"сейчас у тебя: {used} из {limit} использовано",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return MAIN_MENU

    elif query.data == "menu_buy":
        return await show_buy_menu(query.message, context, user_id, edit=True)

    elif query.data.startswith("buy_plan_"):
        plan_key = query.data.replace("buy_plan_", "")
        plan = SUBSCRIPTION_PLANS.get(plan_key)
        if not plan:
            return MAIN_MENU
        # Продамус — приоритетный способ (чеки в налоговую идут автоматически)
        if PRODAMUS_URL:
            pay_url, order_id = create_prodamus_link(user_id, plan_key)
            saved = get_user(user_id) or {}
            saved["pending_order_id"] = order_id
            save_user(user_id, saved)
            keyboard = [
                [InlineKeyboardButton(f"💳 оплатить {plan['label']}", url=pay_url)],
                [InlineKeyboardButton("← назад", callback_data="menu_buy")],
            ]
            await query.edit_message_text(
                f"*{plan['name']}*\n\n{plan['desc']}\n\nстоимость: *{plan['label']}*\n\n"
                f"после оплаты доступ откроется автоматически в течение минуты — я пришлю сообщение 🌙",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
            return MAIN_MENU
        # Продамус — основной способ оплаты
        if PRODAMUS_URL:
            url, order_id = create_prodamus_link(user_id, plan_key)
            saved = get_user(user_id) or {}
            saved["pending_order_id"] = order_id
            save_user(user_id, saved)
            keyboard = [
                [InlineKeyboardButton(f"💳 оплатить {plan['label']}", url=url)],
                [InlineKeyboardButton("← назад", callback_data="menu_buy")],
            ]
            await query.edit_message_text(
                f"*{plan['name']}*\n\n{plan['desc']}\n\nстоимость: *{plan['label']}*\n\n"
                f"после оплаты всё активируется автоматически в течение минуты — я пришлю подтверждение 🌙",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return MAIN_MENU
        # оплата ещё не подключена
        if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET):
            keyboard = [
                [InlineKeyboardButton("💬 написать кате", url=f"https://t.me/{KATYA_TG}")],
                [InlineKeyboardButton("← главное меню", callback_data="back_to_menu")],
            ]
            await query.edit_message_text(
                "💫 оплата скоро подключится!\n\nа пока — напиши кате напрямую, и она всё оформит вручную 🌙",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return MAIN_MENU
        await query.edit_message_text("⏳ создаю ссылку на оплату...")
        result = create_payment(user_id, plan_key)
        if not result:
            keyboard = [[InlineKeyboardButton("← главное меню", callback_data="back_to_menu")]]
            await query.message.reply_text(
                "не получилось создать ссылку на оплату 😔 попробуй позже или напиши кате.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return MAIN_MENU
        saved = get_user(user_id) or {}
        saved["pending_payment_id"] = result["payment_id"]
        saved["pending_plan_key"] = plan_key
        save_user(user_id, saved)
        keyboard = [
            [InlineKeyboardButton(f"💳 оплатить {plan['label']}", url=result["url"])],
            [InlineKeyboardButton("✅ я оплатила", callback_data=f"check_payment_{result['payment_id']}_{plan_key}")],
            [InlineKeyboardButton("← назад", callback_data="menu_buy")],
        ]
        await query.message.reply_text(
            f"*{plan['name']}*\n\n{plan['desc']}\n\nстоимость: *{plan['label']}*\n\nнажми кнопку для оплаты, после — *«я оплатила»*.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return MAIN_MENU

    elif query.data.startswith("check_payment_"):
        parts = query.data.split("_", 3)
        payment_id, plan_key = parts[2], parts[3]
        if check_payment(payment_id):
            grant_subscription(user_id, plan_key)
            plan = SUBSCRIPTION_PLANS.get(plan_key, {})
            await notify_admin_payment(context, user_id, plan_key)
            await query.edit_message_text(
                f"✅ *оплата прошла, всё супер!*\n\n"
                f"*{plan.get('name','')}* активирована 🎉\n\n"
                f"спасибо за доверие! теперь все прогнозы открыты — исследуй себя 🌙",
                parse_mode="Markdown"
            )
            # сразу показываем главное меню — можно пользоваться
            await show_main_menu(query.message, context)
        else:
            keyboard = [
                [InlineKeyboardButton("🔄 проверить ещё раз", callback_data=query.data)],
                [InlineKeyboardButton("← назад", callback_data="menu_buy")],
            ]
            await query.edit_message_text(
                "⏳ платёж пока не найден. если уже оплатила — подожди минуту и проверь ещё раз.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return MAIN_MENU

    elif query.data == "menu_mercury":
        if not can_get_free_forecast(user_id):
            await show_paywall(query.message, user_id, edit=True)
            return MAIN_MENU
        await query.edit_message_text("☿ считаю ближайший ретроградный меркурий...")
        retro_info = find_mercury_retro()
        if not retro_info:
            await query.message.reply_text("не удалось найти данные о ретроградном меркурии.")
            return MAIN_MENU
        name = context.user_data.get("name", "")
        status = "🔴 сейчас идёт" if retro_info["is_current"] else "🟡 предстоит"
        await query.edit_message_text(
            f"☿ *ретроградный меркурий*\n\n{status}\n📅 {retro_info['start_date']} — {retro_info['end_date']}\n\n🔮 готовлю разбор для {name}...",
            parse_mode="Markdown"
        )
        chart = chart_for(context.user_data)
        if "error" in chart:
            await query.message.reply_text(f"ошибка расчёта: {chart['error']}")
            return MAIN_MENU
        retrograde = chart.get("_retrograde", [])
        _cache_retro_flag(user_id, context, retrograde)
        increment_forecast_counter(user_id)
        forecast = await get_mercury_retro_forecast(name, chart, retro_info)
        await query.message.reply_text(forecast)
        if any(r[0] == "Меркурий" for r in retrograde):
            await query.message.reply_text(
                "⚡️ у тебя натальный меркурий ℞ — ты прирождённый навигатор в этом периоде. пока другие спотыкаются, ты работаешь в родной стихии."
            )
        used = get_free_used(user_id)
        limit = get_free_limit(user_id)
        remaining = limit - used
        keyboard = [[InlineKeyboardButton("← вернуться в меню", callback_data="back_to_menu")]]
        footer = f"\n\n_осталось бесплатных прогнозов: {max(0, remaining)}_"
        await query.message.reply_text("что-то ещё?" + footer, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return MAIN_MENU

    elif query.data == "menu_sun":
        # движение солнца — всегда бесплатно, не считается в лимит
        name = context.user_data.get("name", "")
        await query.edit_message_text("☀️ смотрю где сейчас твоё солнце...")
        chart = chart_for(context.user_data)
        if "error" in chart:
            await query.message.reply_text(f"ошибка расчёта: {chart['error']}")
            return MAIN_MENU
        _cache_retro_flag(user_id, context, chart.get("_retrograde", []))
        # номер дома — из кода (факт), нейросеть только описывает темы
        _, sun_sign, sun_deg, sun_house = get_sun_transit(chart.get("_cusps"))
        header = f"☀️ {name}, сейчас твоё солнце в *{sun_house} доме* ({SIGNS_EMOJI.get(sun_sign,'')}{sun_sign} {sun_deg}°)\n\n"
        forecast = await get_sun_transit_forecast(name, chart)
        await query.message.reply_text(header + forecast, parse_mode="Markdown")
        keyboard = [[InlineKeyboardButton("← вернуться в меню", callback_data="back_to_menu")]]
        await query.message.reply_text(
            "что дальше?\n\n_солнце доступно тебе всегда ☀️_",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return MAIN_MENU

    elif query.data == "menu_lunation":
        return await show_lunation_choice(query.message, context, edit=True)

    elif query.data == "menu_eclipse":
        return await show_eclipse_choice(query.message, context, edit=True)

    elif query.data.startswith("ecl_"):
        return await handle_eclipse_choice(update, context)

    elif query.data == "menu_bonus":
        name = context.user_data.get("name", "")
        await query.edit_message_text("⚡️ считаю ретроградные планеты в твоей карте...")
        chart = chart_for(context.user_data)
        if "error" in chart:
            await query.message.reply_text(f"ошибка расчёта: {chart['error']}")
            return MAIN_MENU
        retrograde = chart.get("_retrograde", [])
        _cache_retro_flag(user_id, context, retrograde)
        retro_block = await get_retrograde_block(name, retrograde)
        if retro_block:
            await query.message.reply_text("⚡️ <b>твоя скрытая сила — ретроградные планеты</b>\n\n" + retro_block, parse_mode="HTML")
        keyboard = [[InlineKeyboardButton("← главное меню", callback_data="back_to_menu")]]
        await query.message.reply_text("что дальше?", reply_markup=InlineKeyboardMarkup(keyboard))
        return MAIN_MENU

    elif query.data == "back_to_menu":
        await show_main_menu(query.message, context, edit=True)
        return MAIN_MENU

    elif query.data == "another_lunation":
        return await show_lunation_choice(query.message, context, edit=True)

    elif query.data.startswith("lun_page_"):
        return await show_lunation_choice(query.message, context, edit=True, page=int(query.data.split("_")[2]))

    elif query.data.startswith("lun_") or query.data.startswith("nm_notify_"):
        return await handle_lunation_choice(update, context)

    return MAIN_MENU


# ── ОНБОРДИНГ ────────────────────────────────────────────────────
async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    context.user_data["name"] = name
    context.user_data["consent_at"] = datetime.now(timezone.utc).isoformat()
    await update.message.reply_text(
        f"{name}, садись поудобнее 🌙\n\nвведи дату рождения в формате дд.мм.гггг\nнапример: 31.03.1997"
    )
    return BIRTH_DATE

async def get_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_text = update.message.text.strip()
    try:
        datetime.strptime(date_text, "%d.%m.%Y")
        context.user_data["birth_date"] = date_text
        await update.message.reply_text("теперь время рождения в формате чч:мм\nнапример: 4:44\n\nне знаешь точно — пиши 12:00, но лучше сделать ректификацию у специалиста. для точных рабочих прогнозов время очень важно.")
        return BIRTH_TIME
    except ValueError:
        await update.message.reply_text("формат: дд.мм.гггг, например 31.03.1997")
        return BIRTH_DATE

async def get_birth_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_text = update.message.text.strip().replace(".", ":")
    try:
        datetime.strptime(time_text, "%H:%M")
        context.user_data["birth_time"] = time_text
        await update.message.reply_text("и город рождения:\n\nлучше подписывай с областью, если у тебя маленький город")
        return BIRTH_CITY
    except ValueError:
        await update.message.reply_text("формат: чч:мм, например 23:48")
        return BIRTH_TIME

async def get_birth_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = update.message.text.strip()
    await update.message.reply_text("🔍 ищу твой город...")
    # ищем координаты ОДИН РАЗ при регистрации и сохраняем навсегда
    lat, lon = await asyncio.to_thread(get_coordinates, city)
    if lat is None:
        await update.message.reply_text(
            "не нашла такой город 😔 попробуй написать иначе — например, с областью:\n«никольск пензенская область»"
        )
        return BIRTH_CITY
    context.user_data["birth_city"] = city
    context.user_data["lat"] = lat
    context.user_data["lon"] = lon
    user_id = update.effective_user.id
    context.user_data["tg_id"] = user_id
    save_user(user_id, {
        "name": context.user_data["name"],
        "birth_date": context.user_data["birth_date"],
        "birth_time": context.user_data["birth_time"],
        "birth_city": city,
        "lat": lat,
        "lon": lon,
        "consent_at": context.user_data.get("consent_at", ""),
        "tg_id": user_id,
        "free_forecasts_used": 0,
        "bonus_forecasts": 0,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    })
    await update.message.reply_text("✨ данные сохранены — больше не придётся вводить заново.\n\nначинаем исследовать тебя 🌙")
    await show_main_menu(update.message, context, context.user_data["name"])
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("отменено. /start чтобы начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_user_data()
    uid = str(user_id)
    if uid in data:
        del data[uid]
        save_user_data(data)
    context.user_data.clear()
    await update.message.reply_text("🔄 данные удалены. напиши /start чтобы начать заново.")


# ── СТАТИСТИКА (только для админа) ───────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_user_data()
    total = len(data)
    registered = sum(1 for u in data.values() if u.get("birth_date"))
    total_payments = sum(len(u.get("payments", [])) for u in data.values())
    plan_counts = {}
    for u in data.values():
        for p in u.get("payments", []):
            plan_counts[p["plan"]] = plan_counts.get(p["plan"], 0) + 1
    total_forecasts = sum(u.get("free_forecasts_used", 0) for u in data.values())
    paywall_hits = sum(1 for u in data.values() if u.get("free_forecasts_used", 0) >= FREE_FORECASTS_LIMIT + u.get("bonus_forecasts", 0))

    lines = [
        f"📊 *статистика бота*\n",
        f"👥 всего пользователей: {total}",
        f"✅ с заполненной картой: {registered}",
        f"📈 всего прогнозов выдано: {total_forecasts}",
        f"🚧 уперлись в пейволл: {paywall_hits}",
        f"💳 оплат всего: {total_payments}",
    ]
    if plan_counts:
        lines.append("\nпо тарифам:")
        for k, v in plan_counts.items():
            plan_name = SUBSCRIPTION_PLANS.get(k, {}).get("name", k)
            lines.append(f"  {plan_name}: {v}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def notify_admin_payment_bot(bot, user_id, plan_key):
    """Мгновенное уведомление админу о новой оплате (через объект bot)."""
    if not ADMIN_ID:
        return
    user = get_user(user_id) or {}
    name = user.get("name", "—")
    plan = SUBSCRIPTION_PLANS.get(plan_key, {})
    when = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💰 новая оплата!\n\n"
            f"👤 {name} (id {user_id})\n"
            f"📦 {plan.get('name', plan_key)} — {plan.get('label', '')}\n"
            f"🕐 {when}"
        )
    except Exception as e:
        logger.warning(f"admin payment notify: {e}")

async def notify_admin_payment(context, user_id, plan_key):
    await notify_admin_payment_bot(context.bot, user_id, plan_key)

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список пользователей — только для админа."""
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_user_data()
    registered = [(uid, u) for uid, u in data.items() if u.get("name")]
    if not registered:
        await update.message.reply_text("пока нет пользователей.")
        return
    lines = [f"👥 *пользователи ({len(registered)})*\n"]
    for uid, u in registered:
        reg = u.get("registered_at", "")[:10]
        subs = u.get("subscriptions", {})
        active = "💎" if has_active_unlimited(int(uid)) else ""
        lines.append(
            f"{active} *{u.get('name','—')}* — {u.get('birth_date','?')} {u.get('birth_time','')}, {u.get('birth_city','')}"
            + (f"\n  рег: {reg}" if reg else "")
        )
    # телеграм ограничивает длину — режем на части по 3500 символов
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i+3500], parse_mode="Markdown")

async def payments_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех оплат — только для админа."""
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_user_data()
    all_pays = []
    for uid, u in data.items():
        for p in u.get("payments", []):
            all_pays.append((p.get("at", ""), u.get("name", "—"), uid, p.get("plan", "")))
    all_pays.sort(reverse=True)
    if not all_pays:
        await update.message.reply_text("пока нет оплат.")
        return
    lines = [f"💳 *оплаты ({len(all_pays)})*\n"]
    for at, name, uid, plan_key in all_pays:
        plan = SUBSCRIPTION_PLANS.get(plan_key, {})
        when = at[:16].replace("T", " ")
        lines.append(f"*{name}* — {plan.get('name', plan_key)} {plan.get('label','')}\n  {when}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i+3500], parse_mode="Markdown")


# ── ПРОМОКОДЫ ────────────────────────────────────────────────────
async def activate_promo(context, user_id, code) -> str:
    """Активирует промокод. Возвращает текст ответа пользователю."""
    promos = load_promos()
    promo = promos.get(code)
    if not promo:
        return "такого промокода нет 😔 проверь написание."
    if user_id in promo.get("used_by", []):
        return "ты уже активировала этот промокод 🌙"
    if promo.get("uses_left", 0) <= 0:
        return "этот промокод уже исчерпан 😔"
    # активируем год безлимита
    data = load_user_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {}
    now = datetime.now(timezone.utc)
    data[uid].setdefault("subscriptions", {})["full_year"] = (now + timedelta(days=365)).isoformat()
    save_user_data(data)
    promo["uses_left"] = promo.get("uses_left", 0) - 1
    promo.setdefault("used_by", []).append(user_id)
    save_promos(promos)
    # уведомляем админа
    if ADMIN_ID:
        user = get_user(user_id) or {}
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"🎁 промокод {code} активирован!\n👤 {user.get('name','—')} (id {user_id})\nосталось использований: {promo['uses_left']}"
            )
        except Exception:
            pass
    return "🎁 *промокод активирован!*\n\nу тебя целый год безлимита — все прогнозы открыты 🎉\n\nисследуй себя 🌙"

async def promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Активация промокода: /promo КОД"""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("введи код после команды, например:\n/promo LUNA2026")
        return
    code = context.args[0].strip().upper()
    result_text = await activate_promo(context, user_id, code)
    await update.message.reply_text(result_text, parse_mode="Markdown")
    if "активирован" in result_text:
        context.user_data.update(get_user(user_id) or {})
        await show_main_menu(update.message, context)

async def promo_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание промокода (админ): /promo_add КОД [кол-во использований]"""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("формат: /promo_add КОД [сколько раз можно использовать]\nнапример: /promo_add ЛУНА2026 5")
        return
    code = context.args[0].strip().upper()
    uses = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 1
    promos = load_promos()
    promos[code] = {"plan": "full_year", "uses_left": uses, "used_by": []}
    save_promos(promos)
    await update.message.reply_text(
        f"✅ промокод создан!\n\nкод: `{code}`\nдаёт: год безлимита\nиспользований: {uses}\n\n"
        f"человек активирует его так:\n/promo {code}",
        parse_mode="Markdown"
    )

async def promo_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список промокодов (админ): /promo_list"""
    if update.effective_user.id != ADMIN_ID:
        return
    promos = load_promos()
    if not promos:
        await update.message.reply_text("промокодов пока нет. создай: /promo_add КОД 5")
        return
    lines = ["🎁 *промокоды*\n"]
    for code, p in promos.items():
        used = len(p.get("used_by", []))
        lines.append(f"`{code}` — осталось {p.get('uses_left',0)}, активировали {used}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шпаргалка по командам: /help"""
    user_lines = [
        "🌙 *команды бота*\n",
        "/start — запуск / главное меню",
        "/reset — удалить свои данные и начать заново",
        "/cancel — отменить текущий ввод",
    ]
    admin_lines = [
        "\n👑 *только для тебя (админ)*\n",
        "/stats — статистика: пользователи, прогнозы, оплаты",
        "/users — список пользователей с данными",
        "/payments — все оплаты по времени",
        "/promo\\_add КОД 5 — создать промокод (год безлимита, 5 использований)",
        "/promo\\_list — все промокоды и их остатки",
        "/promo КОД — активировать промокод (эта команда есть и у пользователей, но о ней знают только те, кому дала код)",
        "/rag — проверить, подключены ли авторские материалы",
        "/help — эта шпаргалка",
        "\n📩 *приходят автоматически:*",
        "💰 уведомление о каждой оплате",
        "🎁 уведомление об активации промокода",
    ]
    lines = user_lines + (admin_lines if update.effective_user.id == ADMIN_ID else [])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def rag_check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка подключения авторских материалов (админ): /rag"""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔍 проверяю подключение к базе материалов...")
    result = await asyncio.to_thread(retrieve, "новолуние луна дом")
    if result:
        preview = result[:400]
        await update.message.reply_text(
            f"✅ материалы подключены!\n\nнайдено {len(result)} символов контекста.\n\nначало ответа:\n\n{preview}..."
        )
    else:
        await update.message.reply_text(
            "❌ материалы НЕ подключены — база не отвечает или ключ неверный.\n\n"
            "проверь переменную CHROMA_API_KEY на сервере. прогнозы сейчас идут без авторских материалов!"
        )


# ── ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ─────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("ошибка в обработчике:", exc_info=context.error)
    try:
        if isinstance(update, Update):
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← главное меню", callback_data="back_to_menu")]])
            msg = "что-то пошло не так 😔 давай вернёмся в меню — нажми /start если кнопка не сработает."
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.message.reply_text(msg, reply_markup=keyboard)
            elif update.message:
                await update.message.reply_text(msg, reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"error_handler не смог ответить: {e}")


# ── ЗАПУСК ───────────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        # щедрые таймауты — сеть до telegram может быть медленной
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(60)
        .post_init(start_webhook_server)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            BIRTH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birth_date)],
            BIRTH_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birth_time)],
            BIRTH_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birth_city)],
            MAIN_MENU: [
                CallbackQueryHandler(handle_saved_choice, pattern="^(use_saved|new_data)$"),
                CallbackQueryHandler(handle_main_menu),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("payments", payments_cmd))
    app.add_handler(CommandHandler("promo", promo_cmd))
    app.add_handler(CommandHandler("promo_add", promo_add_cmd))
    app.add_handler(CommandHandler("promo_list", promo_list_cmd))
    app.add_handler(CommandHandler("rag", rag_check_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_error_handler(error_handler)

    # планировщик (если job_queue доступен — нужен extra [job-queue] в requirements)
    if app.job_queue is not None:
        # ежедневная проверка новолуний (10:00 UTC)
        app.job_queue.run_daily(send_newmoon_notifications, time=datetime.strptime("10:00", "%H:%M").time().replace(tzinfo=timezone.utc))
        # ежедневная проверка перехода солнца в новый дом
        app.job_queue.run_daily(send_sun_house_notifications, time=datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=timezone.utc))
        # напоминалка раз в неделю
        app.job_queue.run_repeating(send_retention_messages, interval=timedelta(days=7), first=timedelta(hours=1))
    else:
        logger.warning("job_queue недоступен — уведомления отключены. добавь python-telegram-bot[job-queue] в requirements.")

    print("🌙 astro bushido bot запущен")
    # bootstrap_retries=-1: при таймауте сети не падаем, а пробуем подключиться снова
    app.run_polling(bootstrap_retries=-1)

if __name__ == "__main__":
    main()

