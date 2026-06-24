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
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
HYDRA_API_KEY = os.environ["HYDRA_API_KEY"]
YOOKASSA_SHOP_ID = os.environ["YOOKASSA_SHOP_ID"]
YOOKASSA_SECRET = os.environ["YOOKASSA_SECRET"]
HYDRA_API_URL = "https://api.hydraai.ru/v1/chat/completions"
MODEL = "claude-opus-4.6"
USER_DATA_FILE = "user_data.json"
COMMUNITY_LINK = "https://t.me/astro_bushido_bot"  # заменить на реальную ссылку
PRIVACY_POLICY_URL = "https://telegra.ph/privacy-astro-bushido"  # заменить после публикации

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET

SUBSCRIPTION_PLANS = {
    "mercury_year": {
        "name": "☿ Все Меркурии на год",
        "desc": "Персональный разбор каждого ретроградного Меркурия 2026 — все периоды сразу.",
        "price": "990.00",
        "label": "990 ₽",
    },
    "planets_year": {
        "name": "🪐 Все ретроградные планеты",
        "desc": "Венера, Марс, Юпитер, Сатурн и другие — персональный разбор на весь год.",
        "price": "1990.00",
        "label": "1 990 ₽",
    },
    "full_year": {
        "name": "✨ Полный годовой прогноз",
        "desc": "Все фазы луны + все ретроградности + транзиты на 2026.",
        "price": "3990.00",
        "label": "3 990 ₽",
    },
}

NAME, CONSENT, BIRTH_DATE, BIRTH_TIME, BIRTH_CITY, MAIN_MENU, CHOOSE_LUNATION = range(7)
LUNATIONS_PER_PAGE = 8

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]
SIGNS_EMOJI = {"Овен":"♈","Телец":"♉","Близнецы":"♊","Рак":"♋","Лев":"♌","Дева":"♍",
               "Весы":"♎","Скорпион":"♏","Стрелец":"♐","Козерог":"♑","Водолей":"♒","Рыбы":"♓"}

_LUNATIONS_CACHE = None
_LUNATIONS_CACHE_DATE = None


# ========================
# ХРАНЕНИЕ ДАННЫХ
# ========================
def load_user_data():
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_user_data(data):
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
    if "subscriptions" not in data[uid]:
        data[uid]["subscriptions"] = {}
    data[uid]["subscriptions"][plan_key] = True
    save_user_data(data)


# ========================
# ЮКАССА — СОЗДАНИЕ ПЛАТЕЖА
# ========================
def create_payment(user_id: int, plan_key: str) -> dict | None:
    plan = SUBSCRIPTION_PLANS.get(plan_key)
    if not plan:
        return None
    try:
        payment = YooPayment.create({
            "amount": {"value": plan["price"], "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/astro_bushido_bot?start=paid_{plan_key}_{user_id}",
            },
            "capture": True,
            "description": f"{plan['name']} — Astro Bushido Bot",
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


# ========================
# АСТРОЛОГИЧЕСКИЕ РАСЧЁТЫ
# ========================
def fmt_position(lon):
    lon = lon % 360
    return SIGNS[int(lon / 30)], round(lon % 30, 1)

def get_house(planet_lon, cusps, orb=2.0):
    planet_lon = planet_lon % 360
    for i in range(12):
        c1 = cusps[i] % 360
        c2 = cusps[(i + 1) % 12] % 360
        dist_to_next = (c2 - planet_lon) % 360
        if c1 <= c2:
            in_house = c1 <= planet_lon < c2
        else:
            in_house = planet_lon >= c1 or planet_lon < c2
        if in_house:
            if dist_to_next <= orb:
                return (i + 1) % 12 + 1
            return i + 1
    return 1

def get_coordinates(city_name):
    try:
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
            return {"error": f"Не удалось найти город: {city}"}
        tzname = get_tz(lon_geo, lat)
        if not tzname:
            return {"error": "Не удалось определить часовой пояс"}
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
            retro_mark = " ℞" if is_retro else ""
            chart[name] = f"{sign} {degree}°, {house} дом{retro_mark}"
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
            if d > 300:
                lo = mid
            else:
                hi = mid
        else:
            if d < 180:
                lo = mid
            else:
                hi = mid
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

    results = []
    prev_diff = None
    t = jd

    while t < jd_end:
        sun_lon = swe.calc_ut(t, swe.SUN)[0][0]
        moon_lon = swe.calc_ut(t, swe.MOON)[0][0]
        diff = (moon_lon - sun_lon) % 360

        if prev_diff is not None:
            if prev_diff > 300 and diff < 60:
                nm_jd = _refine_lunation(t - 0.5, t, target=0)
                sign, deg, date_str = _lunation_info(nm_jd)
                results.append(("НЛ", date_str, sign, deg, nm_jd))
            elif prev_diff < 180 <= diff:
                fm_jd = _refine_lunation(t - 0.5, t, target=180)
                sign, deg, date_str = _lunation_info(fm_jd)
                results.append(("ПЛ", date_str, sign, deg, fm_jd))

        prev_diff = diff
        t += 0.5

    results.sort(key=lambda x: x[4])
    _LUNATIONS_CACHE = results
    _LUNATIONS_CACHE_DATE = today
    return results

def find_mercury_retro():
    now = datetime.now(timezone.utc)
    jd = swe.julday(now.year, now.month, now.day, now.hour + now.minute/60)
    speed_now = swe.calc_ut(jd, swe.MERCURY)[0][3]

    if speed_now < 0:
        retro_start_jd = jd - 30
        t = jd
        for _ in range(60):
            t -= 1
            spd = swe.calc_ut(t, swe.MERCURY)[0][3]
            if spd >= 0:
                retro_start_jd = t
                break
        retro_end_jd = None
        t = jd
        for _ in range(60):
            t += 1
            spd = swe.calc_ut(t, swe.MERCURY)[0][3]
            if spd >= 0:
                retro_end_jd = t
                break
        is_current = True
    else:
        retro_start_jd = None
        retro_end_jd = None
        t = jd
        prev_spd = speed_now
        for _ in range(200):
            t += 0.5
            spd = swe.calc_ut(t, swe.MERCURY)[0][3]
            if prev_spd >= 0 and spd < 0 and retro_start_jd is None:
                retro_start_jd = t
            if retro_start_jd and prev_spd < 0 and spd >= 0:
                retro_end_jd = t
                break
            prev_spd = spd
        is_current = False

    if not retro_start_jd or not retro_end_jd:
        return None

    def jd_to_str(j):
        y, m, d, h = swe.revjul(j)
        return f"{int(d):02d}.{int(m):02d}.{int(y)}"

    start_lon = swe.calc_ut(retro_start_jd, swe.MERCURY)[0][0]
    end_lon = swe.calc_ut(retro_end_jd, swe.MERCURY)[0][0]
    start_sign, start_deg = fmt_position(start_lon)
    end_sign, end_deg = fmt_position(end_lon)

    return {
        "is_current": is_current,
        "start_date": jd_to_str(retro_start_jd),
        "end_date": jd_to_str(retro_end_jd),
        "start_sign": start_sign,
        "start_deg": start_deg,
        "end_sign": end_sign,
        "end_deg": end_deg,
        "start_jd": retro_start_jd,
        "end_jd": retro_end_jd,
    }

def get_moon_event_by_jd(event_jd, cusps):
    moon_lon = swe.calc_ut(event_jd, swe.MOON)[0][0] % 360
    sign, degree = fmt_position(moon_lon)
    house = get_house(moon_lon, cusps)
    y, m, d, h = swe.revjul(event_jd)
    return moon_lon, f"{int(d):02d}.{int(m):02d}.{int(y)}", sign, degree, house


# ========================
# ЗАПРОСЫ К CLAUDE
# ========================
async def call_claude(system_prompt, user_prompt, max_tokens=2500):
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
            return f"Ошибка API: {data}"
    except Exception as e:
        return f"Ошибка подключения: {str(e)}"

SYSTEM_ASTRO = """Ты — астролог Екатерина. Подход глубокий, тёплый, честный. Не предсказываешь — помогаешь увидеть и прожить.

⛔️ КРИТИЧЕСКИ ВАЖНО — НИКОГДА НЕ ВЫДУМЫВАЙ:
• Используй ТОЛЬКО те планеты и дома, которые ЯВНО указаны в карте ниже. НЕ добавляй планеты в дома, где их нет.
• Если планеты нет в доме фазы луны — так и говори, не придумывай несуществующие соединения.
• Дом фазы луны указан точно — бери именно его, не пересчитывай.
• Перед каждым утверждением «у тебя планета X в доме Y» — проверь, что это ТОЧНО есть в данных карты.

═══ ТВОЙ ВЗГЛЯД ═══
• Астрология — язык энергий, не предсказание. Интерпретация идёт от событий жизни к карте.
• Фазы луны завершают или запускают тренды, которые уже идут.
• НОВОЛУНИЕ — начало цикла, посев. ПОЛНОЛУНИЕ — кульминация, что завершается.

═══ ДОМА (кратко) ═══
1: личность, тело, инициатива. 2: деньги, ресурс энергии/здоровья. 3: общение, учёба, поездки. 4: семья, дом, корни. 5: творчество, дети, любовь, бизнес. 6: работа, быт, здоровье. 7: партнёры, брак, публика. 8: чужие ресурсы, трансформация, кризисы. 9: знание, статус, путешествия. 10: карьера, статус, власть. 11: планы, коллективы, известность. 12: тайна, бессознательное, эзотерика.

═══ ЛУНА ═══
Автопилот, внутренний ребёнок, источник энергии, психосоматика. ДИГЕСТИЯ: негативный опыт нельзя блокировать — надо ПРОЖИТЬ.

═══ ТОН ═══
Бережно подталкивай к рефлексии. Касайся уязвимого мягко. Через прожитое чувство — ресурс. Честная и тёплая, без гороскопного позитива.

═══ ФОРМАТ (СТРОГО) ═══
• По-русски, по имени.
• КОРОТКО: максимум 3 абзаца на саму фазу луны. Без воды.
• Знак + градус + дом — только реальные из карты.
• В конце — 2-3 коротких вопроса для рефлексии на лунный месяц.
• Весь ответ — не длиннее 4 абзацев + вопросы."""

async def get_astro_forecast(name, chart, lunation_jd, lunation_type):
    cusps = chart.pop("_cusps", None)
    retrograde = chart.pop("_retrograde", None) or []
    chart.pop("_lat", None)
    chart.pop("_lon", None)
    moon_lon, date_s, sign, deg, house = get_moon_event_by_jd(lunation_jd, cusps)
    type_name = "🌑 Новолуние" if lunation_type == "НЛ" else "🌕 Полнолуние"
    lunation_text = f"{type_name} {date_s} — Луна в {sign} {deg}°, {house} дом"
    chart_text = "\n".join([f"  {k}: {v}" for k, v in chart.items()])

    # Ищем релевантные куски из авторских материалов
    planet_names = [r[0] for r in retrograde] + list(chart.keys())[:4]
    query = build_query(lunation_type, sign, house, planet_names)
    source_context = retrieve(query)
    sources_block = f"\n\n═══ АВТОРСКИЕ МАТЕРИАЛЫ (использовать в приоритете) ═══\n{source_context}" if source_context else ""

    return await call_claude(SYSTEM_ASTRO + sources_block, f"""Имя: {name}
Натальная карта (Плацидус, Swiss Ephemeris) — используй ТОЛЬКО эти данные, ничего не добавляй:
{chart_text}

Фаза луны: {lunation_text}

Составь КОРОТКИЙ персональный разбор (максимум 3-4 абзаца). Упоминай только те планеты, что РЕАЛЬНО есть в доме/знаке фазы луны по данным карты. Если в доме фазы луны нет натальных планет — так и скажи. Заверши 2-3 вопросами для рефлексии.""", max_tokens=1400)

async def get_retrograde_block(name, retrograde):
    personal = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    if not personal:
        return None
    retro_list = "\n".join([f"  {n}: {s} {d}°, {h} дом ℞" for n, s, d, h in personal])
    system = """Ты астролог Екатерина. Пишешь КОРОТКИЙ блок про ретроградные планеты в натале.

ТВОЯ ФОРМУЛА РЕТРОГРАДНОСТИ:
• Ретро = всегда ПЕРЕСМОТР и нерешительность. Планета сначала действует, потом откатывается назад пересмотреть.
• Главный принцип: ДВА ШАГА ВПЕРЁД, ОДИН НАЗАД. Человек движется небыстро, но очень осмысленно — потому что всегда делает шаг назад и переосмысляет.
• Это не слабость, а глубина: пока другие несутся, ретро-человек выверяет каждый шаг.
• Меркурий℞: пересмотр в говорении и письме — думает и переформулирует, прежде чем сказать.
• Венера℞: пересмотр в деньгах и отношениях — нестандартное отношение к близости и ценностям.
• Марс℞: пересмотр в действиях — энергия идёт через паузу и переосмысление, действует обдуманно.

ФОРМАТ: по-русски, по имени, ТЕПЛО, но КОРОТКО — максимум 2 небольших абзаца. Без воды. Как ВАУ-бонус про скрытую силу."""
    return await call_claude(system, f"Имя: {name}\nРетро личные планеты:\n{retro_list}\n\nНапиши короткий (2 абзаца) блок про их ретроградность через формулу «два шага вперёд, один назад». Только реальные планеты из списка.", max_tokens=600)

async def get_mercury_retro_forecast(name, chart, retro_info):
    cusps = chart.get("_cusps")
    if not cusps:
        return "Не удалось рассчитать дома."

    start_lon = swe.calc_ut(retro_info["start_jd"], swe.MERCURY)[0][0]
    end_lon = swe.calc_ut(retro_info["end_jd"], swe.MERCURY)[0][0]
    start_house = get_house(start_lon % 360, cusps)
    end_house = get_house(end_lon % 360, cusps)

    status = "сейчас идёт" if retro_info["is_current"] else "предстоит"
    period = f"{retro_info['start_date']} — {retro_info['end_date']}"
    start_pos = f"{retro_info['start_sign']} {retro_info['start_deg']}°, {start_house} дом"
    end_pos = f"{retro_info['end_sign']} {retro_info['end_deg']}°, {end_house} дом"
    chart_text = "\n".join([f"  {k}: {v}" for k, v in chart.items() if not k.startswith("_")])

    merc_query = f"ретроградный меркурий транзит {retro_info['start_sign']} {start_house} дом меркурий"
    source_context = retrieve(merc_query)
    sources_block = (
        f"\n\n═══ АВТОРСКИЕ МАТЕРИАЛЫ — использовать в приоритете ═══\n{source_context}"
        if source_context else ""
    )

    system = """Ты — астролог Екатерина. Пишешь разбор ТРАНЗИТНОГО ретроградного Меркурия.

КЛЮЧЕВОЕ:
• Транзитный ретро Меркурий — это период пересмотра, возвратов, переосмысления. Не катастрофа, а пауза для переработки информации.
• Смотри через какой дом натальной карты он проходит — именно в этой сфере жизни будет пересмотр.
• Если у человека натальный Меркурий тоже ретро — он проживает этот период легче и глубже других (поздравь его).
• Тон: конкретный, честный, тёплый. Не страшилки, но без розовых очков.

ФОРМАТ:
• По имени, по-русски
• Период + по каким домам проходит + что это значит в жизни
• Практические советы на этот период (что делать / чего избегать)
• 3-4 абзаца""" + sources_block

    user = f"""Имя: {name}
Натальная карта:
{chart_text}

Транзитный Меркурий ℞ ({status}):
Период: {period}
Начало: {start_pos}
Конец: {end_pos}

Составь персональный разбор этого периода."""

    return await call_claude(system, user, max_tokens=1500)


# ========================
# TELEGRAM HANDLERS
# ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    # Обработка возврата после оплаты: /start paid_mercury_year_12345
    args = context.args
    if args and args[0].startswith("paid_"):
        parts = args[0].split("_", 2)
        if len(parts) == 3:
            plan_key = parts[1] + "_" + parts[2].split("_")[0]
            # Проверяем последний платёж пользователя
            saved = get_user(user_id) or {}
            payment_id = saved.get("pending_payment_id")
            if payment_id and check_payment(payment_id):
                grant_subscription(user_id, plan_key)
                plan = SUBSCRIPTION_PLANS.get(plan_key, {})
                await update.message.reply_text(
                    f"✅ Оплата прошла успешно!\n\n*{plan.get('name', '')}* активирована. Наслаждайся!",
                    parse_mode="Markdown"
                )

    saved = get_user(user_id)
    if saved:
        context.user_data.update(saved)
        keyboard = [
            [InlineKeyboardButton(f"✅ Да, я {saved['name']}", callback_data="use_saved")],
            [InlineKeyboardButton("🔄 Изменить данные", callback_data="new_data")]
        ]
        await update.message.reply_text(
            f"🌙 С возвращением!\n\nТвои данные: *{saved['name']}*, {saved['birth_date']}, {saved['birth_time']}, {saved['birth_city']}\n\nИспользовать их?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return MAIN_MENU
    else:
        await update.message.reply_text(
            "🌙 Привет! Я *Astro Bushido Bot*.\n\nПомогу понять что происходит в твоей жизни через астрологию.\n\nКак тебя зовут?",
            parse_mode="Markdown"
        )
        return NAME

async def show_main_menu(message, context, name=None, edit=False):
    if name is None:
        name = context.user_data.get("name", "")
    has_retro = context.user_data.get("has_retro_personal")

    keyboard = [
        [InlineKeyboardButton("🌙 Фазы луны", callback_data="menu_lunation")],
        [InlineKeyboardButton("☿ Ретроградный Меркурий", callback_data="menu_mercury")],
    ]
    if has_retro:
        keyboard.append([InlineKeyboardButton("⚡️ Забери свой бонус", callback_data="menu_bonus")])
    keyboard.append([InlineKeyboardButton("🛒 Подписки и покупки", callback_data="menu_buy")])
    keyboard.append([InlineKeyboardButton("👥 Сообщество", callback_data="menu_community")])

    text = f"Привет, {name}! Что хочешь узнать? 🔮"
    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_saved_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "use_saved":
        name = context.user_data.get("name", "")
        await show_main_menu(query.message, context, name=name, edit=True)
        return MAIN_MENU
    elif query.data == "new_data":
        await query.edit_message_text("Хорошо, введём заново. Как тебя зовут?")
        return NAME

def _cache_retro_flag(user_id, context, retrograde):
    personal = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    has_retro = bool(personal)
    if context.user_data.get("has_retro_personal") != has_retro:
        context.user_data["has_retro_personal"] = has_retro
        saved = get_user(user_id) or {}
        saved["has_retro_personal"] = has_retro
        save_user(user_id, saved)

async def show_buy_menu(message, context, edit=False):
    user_id = context.user_data.get("tg_id")
    keyboard = []
    for plan_key, plan in SUBSCRIPTION_PLANS.items():
        if user_id and has_subscription(user_id, plan_key):
            label = f"✅ {plan['name']} — активна"
            keyboard.append([InlineKeyboardButton(label, callback_data="noop")])
        else:
            keyboard.append([InlineKeyboardButton(
                f"{plan['name']} — {plan['label']}",
                callback_data=f"buy_plan_{plan_key}"
            )])
    keyboard.append([InlineKeyboardButton("← Назад", callback_data="back_to_menu")])

    text = (
        "🛒 *Подписки и покупки*\n\n"
        "Бесплатно доступно:\n"
        "• 🌙 Фазы луны — новолуния и полнолуния\n"
        "• ☿ Ретроградный Меркурий\n"
        "• ⚡️ Бонус по ретроградным планетам\n\n"
        "Расширенные возможности:"
    )
    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MAIN_MENU

async def handle_bonus_retro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    name = context.user_data.get("name", "")
    birth_date = context.user_data.get("birth_date", "")
    birth_time = context.user_data.get("birth_time", "")
    birth_city = context.user_data.get("birth_city", "")

    await query.edit_message_text("⚡️ Считаю ретроградные планеты в твоей карте...")

    chart = calculate_natal_chart(birth_date, birth_time, birth_city)
    if "error" in chart:
        keyboard = [[InlineKeyboardButton("← Назад", callback_data="back_to_menu")]]
        await query.message.reply_text(f"Ошибка расчёта: {chart['error']}", reply_markup=InlineKeyboardMarkup(keyboard))
        return MAIN_MENU

    retrograde = chart.get("_retrograde", [])
    personal = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    _cache_retro_flag(update.effective_user.id, context, retrograde)

    if not personal:
        keyboard = [[InlineKeyboardButton("← Назад", callback_data="back_to_menu")]]
        await query.message.reply_text(
            "У тебя нет ретроградных личных планет (Меркурий, Венера, Марс) в натальной карте.\n\nЭто тоже ценно — твои личные планеты действуют напрямую, без задержки и пересмотра.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return MAIN_MENU

    retro_block = await get_retrograde_block(name, retrograde)
    if retro_block:
        await query.message.reply_text(
            "⚡️ <b>Твоя скрытая сила — ретроградные планеты</b>\n\n" + retro_block,
            parse_mode="HTML"
        )

    keyboard = [[InlineKeyboardButton("← Главное меню", callback_data="back_to_menu")]]
    await query.message.reply_text("Что дальше?", reply_markup=InlineKeyboardMarkup(keyboard))
    return MAIN_MENU

async def show_lunation_choice(message, context, edit=False, page=0):
    all_lun = find_lunations_year()
    total = len(all_lun)
    total_pages = max(1, (total + LUNATIONS_PER_PAGE - 1) // LUNATIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * LUNATIONS_PER_PAGE
    end_idx = min(start_idx + LUNATIONS_PER_PAGE, total)

    keyboard = []
    for i in range(start_idx, end_idx):
        ltype, date, sign, deg, jd = all_lun[i]
        emoji = "🌑" if ltype == "НЛ" else "🌕"
        sign_e = SIGNS_EMOJI.get(sign, "")
        label = f"{emoji} {date} {sign_e}{sign} {deg}°"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"lun_{i}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"lun_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд →", callback_data=f"lun_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("← Меню", callback_data="back_to_menu")])

    text = f"🔮 Выбери фазу луны для разбора:\n_страница {page + 1} из {total_pages}_"
    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MAIN_MENU

async def handle_lunation_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    idx = int(query.data.split("_")[1])
    all_lun = find_lunations_year()

    if idx >= len(all_lun):
        await query.edit_message_text("Что-то пошло не так. /start")
        return ConversationHandler.END

    ltype, date, sign, deg, jd = all_lun[idx]
    type_name = "🌑 Новолуние" if ltype == "НЛ" else "🌕 Полнолуние"
    name = context.user_data.get("name", "")

    await query.edit_message_text(
        f"Выбрано: {type_name} {date} — {sign} {deg}°\n\n✨ Считаю карту и готовлю разбор...\n🔮 Это займёт около минуты"
    )

    chart = calculate_natal_chart(
        context.user_data.get("birth_date", ""),
        context.user_data.get("birth_time", ""),
        context.user_data.get("birth_city", "")
    )
    if "error" in chart:
        await query.message.reply_text(f"Ошибка расчёта: {chart['error']}\n/start")
        return ConversationHandler.END

    retrograde = chart.get("_retrograde", [])
    personal_retro = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    _cache_retro_flag(update.effective_user.id, context, retrograde)

    forecast = await get_astro_forecast(name, chart, jd, ltype)
    await query.message.reply_text(forecast)

    keyboard = [
        [InlineKeyboardButton("🔮 Другая фаза луны", callback_data="another_lunation")],
    ]
    if personal_retro:
        keyboard.append([InlineKeyboardButton("⚡️ Забери свой бонус", callback_data="menu_bonus")])
    keyboard.append([InlineKeyboardButton("← Главное меню", callback_data="back_to_menu")])

    await query.message.reply_text("Что дальше?", reply_markup=InlineKeyboardMarkup(keyboard))
    return MAIN_MENU

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        return MAIN_MENU

    elif query.data == "menu_community":
        keyboard = [
            [InlineKeyboardButton("👥 Перейти в сообщество", url=COMMUNITY_LINK)],
            [InlineKeyboardButton("← Назад", callback_data="back_to_menu")]
        ]
        await query.edit_message_text(
            "👥 *Сообщество Astro Bushido*\n\nЗдесь мы разбираем лунные циклы, говорим о трансформации и поддерживаем друг друга 🌙\n\nПрисоединяйся!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return MAIN_MENU

    elif query.data == "menu_buy":
        return await show_buy_menu(query.message, context, edit=True)

    elif query.data.startswith("buy_plan_"):
        plan_key = query.data.replace("buy_plan_", "")
        plan = SUBSCRIPTION_PLANS.get(plan_key)
        if not plan:
            return MAIN_MENU

        user_id = update.effective_user.id
        await query.edit_message_text(f"⏳ Создаю ссылку на оплату...")

        result = create_payment(user_id, plan_key)
        if not result:
            await query.message.reply_text("Ошибка создания платежа. Попробуй позже.")
            return MAIN_MENU

        # Сохраняем payment_id для проверки после возврата
        saved = get_user(user_id) or {}
        saved["pending_payment_id"] = result["payment_id"]
        save_user(user_id, saved)

        keyboard = [
            [InlineKeyboardButton(f"💳 Оплатить {plan['label']}", url=result["url"])],
            [InlineKeyboardButton("✅ Я оплатила", callback_data=f"check_payment_{result['payment_id']}_{plan_key}")],
            [InlineKeyboardButton("← Назад", callback_data="menu_buy")],
        ]
        await query.message.reply_text(
            f"*{plan['name']}*\n\n{plan['desc']}\n\nСтоимость: *{plan['label']}*\n\n"
            f"Нажми кнопку ниже для оплаты. После оплаты нажми *«Я оплатила»*.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return MAIN_MENU

    elif query.data.startswith("check_payment_"):
        parts = query.data.split("_", 3)
        payment_id = parts[2]
        plan_key = parts[3]
        user_id = update.effective_user.id

        if check_payment(payment_id):
            grant_subscription(user_id, plan_key)
            plan = SUBSCRIPTION_PLANS.get(plan_key, {})
            keyboard = [[InlineKeyboardButton("← Главное меню", callback_data="back_to_menu")]]
            await query.edit_message_text(
                f"✅ *Оплата подтверждена!*\n\n*{plan.get('name', '')}* активирована.\nСпасибо! 🌙",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            keyboard = [
                [InlineKeyboardButton("🔄 Проверить ещё раз", callback_data=query.data)],
                [InlineKeyboardButton("← Назад", callback_data="menu_buy")],
            ]
            await query.edit_message_text(
                "⏳ Платёж пока не найден. Если ты уже оплатила — подожди минуту и проверь ещё раз.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return MAIN_MENU

    elif query.data == "menu_mercury":
        await query.edit_message_text("☿ Считаю ближайший ретроградный Меркурий...")
        retro_info = find_mercury_retro()
        if not retro_info:
            await query.message.reply_text("Не удалось найти данные о ретроградном Меркурии.")
            return MAIN_MENU

        name = context.user_data.get("name", "")
        birth_date = context.user_data.get("birth_date", "")
        birth_time = context.user_data.get("birth_time", "")
        birth_city = context.user_data.get("birth_city", "")

        status = "🔴 Сейчас идёт" if retro_info["is_current"] else "🟡 Предстоит"
        preview = (
            f"☿ *Ретроградный Меркурий*\n\n"
            f"{status}\n"
            f"📅 {retro_info['start_date']} — {retro_info['end_date']}\n"
            f"Начало: {retro_info['start_sign']} {retro_info['start_deg']}°\n"
            f"Конец: {retro_info['end_sign']} {retro_info['end_deg']}°\n\n"
            f"🔮 Готовлю персональный разбор для {name}..."
        )
        await query.edit_message_text(preview, parse_mode="Markdown")

        chart = calculate_natal_chart(birth_date, birth_time, birth_city)
        if "error" in chart:
            await query.message.reply_text(f"Ошибка расчёта карты: {chart['error']}")
            return MAIN_MENU

        retrograde = chart.get("_retrograde", [])
        _cache_retro_flag(update.effective_user.id, context, retrograde)

        forecast = await get_mercury_retro_forecast(name, chart, retro_info)
        await query.message.reply_text(forecast)

        has_natal_merc_retro = any(r[0] == "Меркурий" for r in retrograde)
        if has_natal_merc_retro:
            await query.message.reply_text(
                "⚡️ *Особый момент:* у тебя натальный Меркурий ℞ — ты прирождённый навигатор в этом периоде. Пока другие спотыкаются, ты работаешь в родной стихии. Это твоё время.",
                parse_mode="Markdown"
            )

        keyboard = [[InlineKeyboardButton("← Вернуться в меню", callback_data="back_to_menu")]]
        await query.message.reply_text("Что-то ещё?", reply_markup=InlineKeyboardMarkup(keyboard))
        return MAIN_MENU

    elif query.data == "menu_lunation":
        return await show_lunation_choice(query.message, context, edit=True)

    elif query.data == "menu_bonus":
        return await handle_bonus_retro(update, context)

    elif query.data == "back_to_menu":
        name = context.user_data.get("name", "")
        await show_main_menu(query.message, context, name=name, edit=True)
        return MAIN_MENU

    elif query.data == "another_lunation":
        return await show_lunation_choice(query.message, context, edit=True)

    elif query.data.startswith("lun_page_"):
        page = int(query.data.split("_")[2])
        return await show_lunation_choice(query.message, context, edit=True, page=page)

    elif query.data.startswith("lun_"):
        return await handle_lunation_choice(update, context)

    return MAIN_MENU

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        f"Приятно познакомиться, {context.user_data['name']}! 🌟\n\n"
        f"Для натальной карты мне нужны данные рождения.\n\n"
        f"Нажимая кнопку *«Соглашаюсь»*, ты даёшь согласие на обработку персональных данных "
        f"в соответствии с [политикой обработки персональных данных]({PRIVACY_POLICY_URL}), "
        f"а также даёшь согласие на получение рекламной и информационной рассылки.\n\n"
        f"Нажми *«Соглашаюсь»*, чтобы продолжить.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Соглашаюсь", callback_data="consent_yes")],
            [InlineKeyboardButton("❌ Не соглашаюсь", callback_data="consent_no")],
        ]),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    return CONSENT

async def get_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "consent_yes":
        context.user_data["consent_at"] = datetime.now(timezone.utc).isoformat()
        await query.edit_message_text(
            "Отлично! 🙏\n\nВведи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 31.03.1997"
        )
        return BIRTH_DATE
    else:
        await query.edit_message_text("Хорошо. Если передумаешь — /start")
        return ConversationHandler.END

async def get_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_text = update.message.text.strip()
    try:
        datetime.strptime(date_text, "%d.%m.%Y")
        context.user_data["birth_date"] = date_text
        await update.message.reply_text("Теперь время рождения в формате ЧЧ:ММ\nНапример: 23:48\n\nНе знаешь точно — пиши 12:00")
        return BIRTH_TIME
    except ValueError:
        await update.message.reply_text("Формат: ДД.ММ.ГГГГ, например 31.03.1997")
        return BIRTH_DATE

async def get_birth_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_text = update.message.text.strip().replace(".", ":")
    try:
        datetime.strptime(time_text, "%H:%M")
        context.user_data["birth_time"] = time_text
        await update.message.reply_text("И город рождения:")
        return BIRTH_CITY
    except ValueError:
        await update.message.reply_text("Формат: ЧЧ:ММ, например 23:48")
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
    })
    await update.message.reply_text("✨ Данные сохранены — больше не придётся вводить заново.")
    await show_main_menu(update.message, context, context.user_data["name"])
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. /start чтобы начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ========================
# ЗАПУСК
# ========================
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
    print("🌙 Astro Bushido Bot — фазы луны + Меркурий ℞ + бонус + ЮКасса")
    app.run_polling()

if __name__ == "__main__":
    main()
