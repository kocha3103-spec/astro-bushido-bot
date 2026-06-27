import os
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
MODEL = "claude-sonnet-4-6"
USER_DATA_FILE = os.environ.get("USER_DATA_FILE", "/data/user_data.json")
COMMUNITY_LINK = "https://t.me/astro_bushido"
KATYA_TG = "katerinakocha"
PRIVACY_POLICY_URL = "https://telegra.ph/privacy-astro-bushido"
BOT_USERNAME = "astro_bushido_bot"
FREE_FORECASTS_LIMIT = 4

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET

SUBSCRIPTION_PLANS = {
    "mercury_year": {
        "name": "☿ все меркурии на год",
        "desc": "персональный разбор каждого ретроградного меркурия 2026 — все периоды сразу.",
        "price": "990.00",
        "label": "990 ₽",
    },
    "planets_year": {
        "name": "🪐 все ретроградные планеты",
        "desc": "венера, марс, юпитер, сатурн и другие — персональный разбор на весь год.",
        "price": "1990.00",
        "label": "1 990 ₽",
    },
    "full_year": {
        "name": "✨ полный годовой прогноз",
        "desc": "все фазы луны + все ретроградности + транзиты на 2026.",
        "price": "3990.00",
        "label": "3 990 ₽",
    },
}

NAME, CONSENT, BIRTH_DATE, BIRTH_TIME, BIRTH_CITY, MAIN_MENU = range(6)
LUNATIONS_PER_PAGE = 8

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]
SIGNS_EMOJI = {"Овен":"♈","Телец":"♉","Близнецы":"♊","Рак":"♋","Лев":"♌","Дева":"♍",
               "Весы":"♎","Скорпион":"♏","Стрелец":"♐","Козерог":"♑","Водолей":"♒","Рыбы":"♓"}

_LUNATIONS_CACHE = None
_LUNATIONS_CACHE_DATE = None


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
    data[uid].setdefault("subscriptions", {})[plan_key] = True
    data[uid].setdefault("payments", []).append({
        "plan": plan_key,
        "at": datetime.now(timezone.utc).isoformat()
    })
    save_user_data(data)

def get_free_limit(user_id):
    user = get_user(user_id)
    bonus = user.get("bonus_forecasts", 0) if user else 0
    return FREE_FORECASTS_LIMIT + bonus

def get_free_used(user_id):
    user = get_user(user_id)
    return user.get("free_forecasts_used", 0) if user else 0

def can_get_free_forecast(user_id):
    if has_subscription(user_id, "full_year"):
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

def calculate_natal_chart(birth_date, birth_time, city):
    try:
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

SYSTEM_ASTRO = """ты — астролог катя. подход глубокий, тёплый, честный. не предсказываешь — помогаешь увидеть и прожить.

⛔️ ВАЖНО — НИКОГДА НЕ ВЫДУМЫВАЙ:
• используй ТОЛЬКО те планеты и дома, которые явно указаны в карте. не добавляй ничего лишнего.
• если планеты нет в доме фазы — так и скажи.

═══ ВЗГЛЯД ═══
• новолуние — начало цикла, посев. полнолуние — кульминация, что завершается.
• астрология — язык энергий, не предсказание.

═══ ТОН ═══
• по-русски, с маленькой буквы, на ты — как близкая подруга, которая разбирается в астрологии.
• тепло и честно. без гороскопного позитива и без пустых слов.
• короткие живые предложения.

═══ ФОРМАТ (строго) ═══
• по имени, на ты.
• максимум 3 коротких абзаца на разбор.
• только реальные планеты из карты.
• в конце — 3 вопроса для рефлексии:
  1. вопрос по теме прогноза (что происходит в этой сфере?)
  2. вопрос про жизнь прямо сейчас (как это проявляется у тебя?)
  3. ОБЯЗАТЕЛЬНО вопрос про телесные ощущения — как ты чувствуешь это в теле? где замечаешь напряжение или лёгкость?
• весь ответ — не длиннее 4 абзацев + вопросы."""

async def get_astro_forecast(name, chart, lunation_jd, lunation_type):
    cusps = chart.pop("_cusps", None)
    retrograde = chart.pop("_retrograde", None) or []
    chart.pop("_lat", None); chart.pop("_lon", None)
    moon_lon, date_s, sign, deg, house = get_moon_event_by_jd(lunation_jd, cusps)
    type_name = "🌑 новолуние" if lunation_type == "НЛ" else "🌕 полнолуние"
    lunation_text = f"{type_name} {date_s} — луна в {sign} {deg}°, {house} дом"
    chart_text = "\n".join(f"  {k}: {v}" for k, v in chart.items())
    query = build_query(lunation_type, sign, house, [r[0] for r in retrograde] + list(chart.keys())[:4])
    source_context = retrieve(query)
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
    source_context = retrieve(merc_query)
    sources_block = f"\n\n═══ АВТОРСКИЕ МАТЕРИАЛЫ (приоритет) ═══\n{source_context}" if source_context else ""
    system = """ты — астролог катя. пишешь разбор транзитного ретроградного меркурия.
• не катастрофа, а пауза для переосмысления. смотришь через какой дом он проходит.
• если натальный меркурий тоже ретро — человек проживает этот период легче (скажи об этом).
• тон: конкретный, тёплый, на ты, с маленькой буквы.
• формат: 3 коротких абзаца + 3 вопроса для рефлексии (последний про телесные ощущения).""" + sources_block
    user = f"""имя: {name}
натальная карта:
{chart_text}

транзитный меркурий ℞ ({status}):
период: {period}
начало: {retro_info['start_sign']} {retro_info['start_deg']}°, {start_house} дом
конец: {retro_info['end_sign']} {retro_info['end_deg']}°, {end_house} дом"""
    return await call_claude(system, user)


# ── ПЕЙВОЛЛ ───────────────────────────────────────────────────────
async def show_paywall(message, user_id, edit=False):
    used = get_free_used(user_id)
    limit = get_free_limit(user_id)
    text = (
        f"🌙 ты использовала все {limit} бесплатных прогноза\n\n"
        "чтобы продолжить — выбери подписку:\n\n"
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
        [InlineKeyboardButton("☿ ретроградный меркурий", callback_data="menu_mercury")],
    ]
    if has_retro:
        keyboard.append([InlineKeyboardButton("⚡️ забери свой бонус", callback_data="menu_bonus")])
    keyboard += [
        [InlineKeyboardButton("🔗 пригласи подругу — получи прогноз", callback_data="menu_referral")],
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

    # возврат после оплаты
    if args and args[0].startswith("paid_"):
        parts = args[0].split("_", 2)
        if len(parts) == 3:
            plan_key = parts[1] + "_" + parts[2].split("_")[0]
            saved = get_user(user_id) or {}
            pid = saved.get("pending_payment_id")
            if pid and check_payment(pid):
                grant_subscription(user_id, plan_key)
                plan = SUBSCRIPTION_PLANS.get(plan_key, {})
                await update.message.reply_text(f"✅ оплата прошла! *{plan.get('name','')}* активирована 🌙", parse_mode="Markdown")

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
        "привет, мои дорогие! 🌙 на связи катя.\n\n"
        "это мой бот, где можно настроиться на лунные и планетарные ритмы, задать себе вопросы и отслеживать как астрология проявляется в твоей жизни каждый месяц ✨\n\n"
        "я сама много лет наблюдаю за этим — это правда улучшает качество жизни и помогает лучше понимать себя."
    )
    await update.message.reply_text("как тебя зовут?")
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
    keyboard = []
    for k, p in SUBSCRIPTION_PLANS.items():
        if has_subscription(user_id, k):
            keyboard.append([InlineKeyboardButton(f"✅ {p['name']} — активна", callback_data="noop")])
        else:
            keyboard.append([InlineKeyboardButton(f"{p['name']} — {p['label']}", callback_data=f"buy_plan_{k}")])
    keyboard.append([InlineKeyboardButton("← назад", callback_data="back_to_menu")])
    used = get_free_used(user_id)
    limit = get_free_limit(user_id)
    text = f"🛒 *подписки*\n\nбесплатно использовано: {used} из {limit} прогнозов\n\nрасширенные возможности:"
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

    chart = calculate_natal_chart(
        context.user_data.get("birth_date", ""),
        context.user_data.get("birth_time", ""),
        context.user_data.get("birth_city", "")
    )
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
    await query.message.reply_text("что дальше?" + footer, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
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
        await query.edit_message_text("⏳ создаю ссылку на оплату...")
        result = create_payment(user_id, plan_key)
        if not result:
            await query.message.reply_text("ошибка создания платежа. попробуй позже.")
            return MAIN_MENU
        saved = get_user(user_id) or {}
        saved["pending_payment_id"] = result["payment_id"]
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
            keyboard = [[InlineKeyboardButton("← главное меню", callback_data="back_to_menu")]]
            await query.edit_message_text(
                f"✅ *оплата подтверждена!*\n\n*{plan.get('name','')}* активирована. спасибо! 🌙",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
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
        chart = calculate_natal_chart(
            context.user_data.get("birth_date", ""),
            context.user_data.get("birth_time", ""),
            context.user_data.get("birth_city", "")
        )
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

    elif query.data == "menu_lunation":
        return await show_lunation_choice(query.message, context, edit=True)

    elif query.data == "menu_bonus":
        name = context.user_data.get("name", "")
        await query.edit_message_text("⚡️ считаю ретроградные планеты в твоей карте...")
        chart = calculate_natal_chart(
            context.user_data.get("birth_date", ""),
            context.user_data.get("birth_time", ""),
            context.user_data.get("birth_city", "")
        )
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
    await update.message.reply_text(
        f"{name}, садись поудобнее 🌙\n\n"
        "для персональных прогнозов мне нужны данные твоего рождения. "
        f"оставляя их, ты соглашаешься на [обработку персональных данных]({PRIVACY_POLICY_URL}).",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ соглашаюсь", callback_data="consent_yes")],
            [InlineKeyboardButton("❌ не соглашаюсь", callback_data="consent_no")],
        ]),
        parse_mode="Markdown", disable_web_page_preview=True
    )
    return CONSENT

async def get_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "consent_yes":
        context.user_data["consent_at"] = datetime.now(timezone.utc).isoformat()
        await query.edit_message_text("отлично! 🙏\n\nвведи дату рождения в формате дд.мм.гггг\nнапример: 31.03.1997")
        return BIRTH_DATE
    await query.edit_message_text("хорошо. если передумаешь — /start")
    return ConversationHandler.END

async def get_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_text = update.message.text.strip()
    try:
        datetime.strptime(date_text, "%d.%m.%Y")
        context.user_data["birth_date"] = date_text
        await update.message.reply_text("теперь время рождения в формате чч:мм\nнапример: 23:48\n\nне знаешь точно — пиши 12:00")
        return BIRTH_TIME
    except ValueError:
        await update.message.reply_text("формат: дд.мм.гггг, например 31.03.1997")
        return BIRTH_DATE

async def get_birth_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_text = update.message.text.strip().replace(".", ":")
    try:
        datetime.strptime(time_text, "%H:%M")
        context.user_data["birth_time"] = time_text
        await update.message.reply_text("и город рождения:")
        return BIRTH_CITY
    except ValueError:
        await update.message.reply_text("формат: чч:мм, например 23:48")
        return BIRTH_TIME

async def get_birth_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = update.message.text.strip()
    context.user_data["birth_city"] = city
    user_id = update.effective_user.id
    context.user_data["tg_id"] = user_id
    save_user(user_id, {
        "name": context.user_data["name"],
        "birth_date": context.user_data["birth_date"],
        "birth_time": context.user_data["birth_time"],
        "birth_city": city,
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


# ── ЗАПУСК ───────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            CONSENT: [CallbackQueryHandler(get_consent, pattern="^consent_")],
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

    # ежедневная проверка новолуний (10:00 UTC)
    app.job_queue.run_daily(send_newmoon_notifications, time=datetime.strptime("10:00", "%H:%M").time().replace(tzinfo=timezone.utc))
    # напоминалка раз в неделю
    app.job_queue.run_repeating(send_retention_messages, interval=timedelta(days=7), first=timedelta(hours=1))

    print("🌙 astro bushido bot запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
