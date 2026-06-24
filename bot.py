import os
import logging
import json
import httpx
import swisseph as swe
from geopy.geocoders import Nominatim
from tzfpy import get_tz
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
HYDRA_API_KEY = os.environ["HYDRA_API_KEY"]
HYDRA_API_URL = "https://api.hydraai.ru/v1/chat/completions"
MODEL = "claude-opus-4.6"
USER_DATA_FILE = "user_data.json"
COMMUNITY_LINK = "https://t.me/astro_bushido_bot"  # заглушка

NAME, CONSENT, BIRTH_DATE, BIRTH_TIME, BIRTH_CITY, MAIN_MENU, CHOOSE_LUNATION = range(7)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]
SIGNS_EMOJI = {"Овен":"♈","Телец":"♉","Близнецы":"♊","Рак":"♋","Лев":"♌","Дева":"♍",
               "Весы":"♎","Скорпион":"♏","Стрелец":"♐","Козерог":"♑","Водолей":"♒","Рыбы":"♓"}


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

def find_mercury_retro():
    """Найти ближайший ретроградный период Меркурия (текущий или будущий)"""
    now = datetime.now(timezone.utc)
    jd = swe.julday(now.year, now.month, now.day, now.hour + now.minute/60)
    speed_now = swe.calc_ut(jd, swe.MERCURY)[0][3]
    
    # Если сейчас уже ретро — ищем когда закончился и когда начался
    if speed_now < 0:
        # Ищем начало (назад) и конец (вперёд)
        # Начало
        t = jd
        for _ in range(60):
            t -= 1
            spd = swe.calc_ut(t, swe.MERCURY)[0][3]
            if spd >= 0:
                retro_start_jd = t
                break
        # Конец
        t = jd
        for _ in range(60):
            t += 1
            spd = swe.calc_ut(t, swe.MERCURY)[0][3]
            if spd >= 0:
                retro_end_jd = t
                break
        is_current = True
    else:
        # Ищем вперёд
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

def find_lunations_around_now(weeks_back=3):
    now = datetime.now(timezone.utc)
    start = now - timedelta(weeks=weeks_back)
    jd_start = swe.julday(start.year, start.month, start.day, start.hour + start.minute/60)
    results = {"НЛ": [], "ПЛ": []}
    t = jd_start
    prev_diff = None
    for _ in range(300):
        sun = swe.calc_ut(t, swe.SUN)[0][0]
        moon = swe.calc_ut(t, swe.MOON)[0][0]
        diff = (moon - sun) % 360
        if prev_diff is not None:
            if prev_diff > 300 and diff < 60 and len(results["НЛ"]) < 2:
                lo, hi = t - 0.5, t
                for _ in range(50):
                    mid = (lo + hi) / 2
                    d = (swe.calc_ut(mid, swe.MOON)[0][0] - swe.calc_ut(mid, swe.SUN)[0][0]) % 360
                    if d > 300: lo = mid
                    else: hi = mid
                nm_jd = (lo + hi) / 2
                moon_lon = swe.calc_ut(nm_jd, swe.MOON)[0][0] % 360
                sign, deg = fmt_position(moon_lon)
                y, mo, d, h = swe.revjul(nm_jd)
                results["НЛ"].append((f"{int(d):02d}.{int(mo):02d}.{int(y)}", sign, deg, nm_jd))
            if diff < 180 <= prev_diff and len(results["ПЛ"]) < 2:
                lo, hi = t - 0.5, t
                for _ in range(50):
                    mid = (lo + hi) / 2
                    d = (swe.calc_ut(mid, swe.MOON)[0][0] - swe.calc_ut(mid, swe.SUN)[0][0]) % 360
                    if d < 180: lo = mid
                    else: hi = mid
                fm_jd = (lo + hi) / 2
                moon_lon = swe.calc_ut(fm_jd, swe.MOON)[0][0] % 360
                sign, deg = fmt_position(moon_lon)
                y, mo, d, h = swe.revjul(fm_jd)
                results["ПЛ"].append((f"{int(d):02d}.{int(mo):02d}.{int(y)}", sign, deg, fm_jd))
        if len(results["НЛ"]) >= 2 and len(results["ПЛ"]) >= 2:
            break
        prev_diff = diff
        t += 0.5
    return results

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
• Если планеты нет в доме лунации — так и говори, не придумывай несуществующие соединения.
• Дом лунации указан точно — бери именно его, не пересчитывай.
• Перед каждым утверждением «у тебя планета X в доме Y» — проверь, что это ТОЧНО есть в данных карты.

═══ ТВОЙ ВЗГЛЯД ═══
• Астрология — язык энергий, не предсказание. Интерпретация идёт от событий жизни к карте.
• Лунации завершают или запускают тренды, которые уже идут.
• НОВОЛУНИЕ — начало цикла, посев. ПОЛНОЛУНИЕ — кульминация, что завершается.

═══ ДОМА (кратко) ═══
1: личность, тело, инициатива. 2: деньги, ресурс энергии/здоровья. 3: общение, учёба, поездки. 4: семья, дом, корни. 5: творчество, дети, любовь, бизнес. 6: работа, быт, здоровье. 7: партнёры, брак, публика. 8: чужие ресурсы, трансформация, кризисы. 9: знание, статус, путешествия. 10: карьера, статус, власть. 11: планы, коллективы, известность. 12: тайна, бессознательное, эзотерика.

═══ ЛУНА ═══
Автопилот, внутренний ребёнок, источник энергии, психосоматика. ДИГЕСТИЯ: негативный опыт нельзя блокировать — надо ПРОЖИТЬ.

═══ ТОН ═══
Бережно подталкивай к рефлексии. Касайся уязвимого мягко. Через прожитое чувство — ресурс. Честная и тёплая, без гороскопного позитива.

═══ ФОРМАТ (СТРОГО) ═══
• По-русски, по имени.
• КОРОТКО: максимум 3 абзаца на саму лунацию. Без воды.
• Знак + градус + дом — только реальные из карты.
• В конце — 2-3 коротких вопроса для рефлексии на лунный месяц.
• Весь ответ — не длиннее 4 абзацев + вопросы."""

async def get_astro_forecast(name, chart, lunation_jd, lunation_type):
    cusps = chart.pop("_cusps", None)
    chart.pop("_retrograde", None)
    chart.pop("_lat", None)
    chart.pop("_lon", None)
    moon_lon, date_s, sign, deg, house = get_moon_event_by_jd(lunation_jd, cusps)
    type_name = "🌑 Новолуние" if lunation_type == "НЛ" else "🌕 Полнолуние"
    lunation_text = f"{type_name} {date_s} — Луна в {sign} {deg}°, {house} дом"
    chart_text = "\n".join([f"  {k}: {v}" for k, v in chart.items()])
    return await call_claude(SYSTEM_ASTRO, f"""Имя: {name}
Натальная карта (Плацидус, Swiss Ephemeris) — используй ТОЛЬКО эти данные, ничего не добавляй:
{chart_text}

Лунация: {lunation_text}

Составь КОРОТКИЙ персональный разбор (максимум 3-4 абзаца). Упоминай только те планеты, что РЕАЛЬНО есть в доме/знаке лунации по данным карты. Если в доме лунации нет натальных планет — так и скажи. Заверши 2-3 вопросами для рефлексии.""", max_tokens=1400)

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

    # Определяем по каким домам пройдёт транзитный Меркурий
    start_lon = swe.calc_ut(retro_info["start_jd"], swe.MERCURY)[0][0]
    end_lon = swe.calc_ut(retro_info["end_jd"], swe.MERCURY)[0][0]
    start_house = get_house(start_lon % 360, cusps)
    end_house = get_house(end_lon % 360, cusps)

    status = "сейчас идёт" if retro_info["is_current"] else "предстоит"
    period = f"{retro_info['start_date']} — {retro_info['end_date']}"
    start_pos = f"{retro_info['start_sign']} {retro_info['start_deg']}°, {start_house} дом"
    end_pos = f"{retro_info['end_sign']} {retro_info['end_deg']}°, {end_house} дом"

    chart_text = "\n".join([f"  {k}: {v}" for k, v in chart.items() if not k.startswith("_")])

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
• 3-4 абзаца"""

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

async def show_main_menu(message, name, edit=False):
    keyboard = [
        [InlineKeyboardButton("🌙 Прогноз по лунациям", callback_data="menu_lunation")],
        [InlineKeyboardButton("☿ Ретроградный Меркурий", callback_data="menu_mercury")],
        [InlineKeyboardButton("👥 Вступить в комьюнити", callback_data="menu_community")],
    ]
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
        await show_main_menu(query.message, name, edit=True)
        return MAIN_MENU
    elif query.data == "new_data":
        await query.edit_message_text("Хорошо, введём заново. Как тебя зовут?")
        return NAME

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "menu_community":
        keyboard = [[InlineKeyboardButton("👥 Перейти в комьюнити", url=COMMUNITY_LINK)],
                    [InlineKeyboardButton("← Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(
            "👥 *Комьюнити Astro Bushido*\n\nСкоро здесь будет ссылка на наше сообщество — место где мы разбираем лунные циклы, говорим про трансформацию и поддерживаем друг друга. 🌙",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
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

        forecast = await get_mercury_retro_forecast(name, chart, retro_info)
        await query.message.reply_text(forecast)

        # Если у человека натальный Меркурий ретро — особое поздравление
        retro = chart.get("_retrograde", [])
        has_natal_merc_retro = any(r[0] == "Меркурий" for r in retro)
        if has_natal_merc_retro:
            await query.message.reply_text(
                "⚡️ *Особый момент:* у тебя натальный Меркурий ℞ — ты прирождённый навигатор в этом периоде. Пока другие спотыкаются, ты работаешь в родной стихии. Это твоё время.",
                parse_mode="Markdown"
            )

        keyboard = [
            [InlineKeyboardButton("← Вернуться в меню", callback_data="back_to_menu")]
        ]
        await query.message.reply_text("Что-то ещё?", reply_markup=InlineKeyboardMarkup(keyboard))
        return MAIN_MENU

    elif query.data == "menu_lunation":
        return await show_lunation_choice(query.message, context, edit=True)

    elif query.data == "back_to_menu":
        name = context.user_data.get("name", "")
        await show_main_menu(query.message, name, edit=True)
        return MAIN_MENU

    elif query.data == "another_lunation":
        return await show_lunation_choice(query.message, context, edit=True)

    elif query.data.startswith("НЛ_") or query.data.startswith("ПЛ_"):
        return await handle_lunation_choice(update, context)

    return MAIN_MENU

async def show_lunation_choice(message, context, edit=False):
    lunations = find_lunations_around_now(weeks_back=3)
    context.user_data["lunations"] = {}
    for i, (date, sign, deg, jd) in enumerate(lunations["НЛ"]):
        context.user_data["lunations"][f"НЛ_{i}"] = (date, sign, deg, jd)
    for i, (date, sign, deg, jd) in enumerate(lunations["ПЛ"]):
        context.user_data["lunations"][f"ПЛ_{i}"] = (date, sign, deg, jd)

    keyboard = []
    for i, (date, sign, deg, jd) in enumerate(lunations["НЛ"]):
        e = SIGNS_EMOJI.get(sign, "")
        keyboard.append([InlineKeyboardButton(f"🌑 Новолуние {date} {e}{sign} {deg}°", callback_data=f"НЛ_{i}")])
    for i, (date, sign, deg, jd) in enumerate(lunations["ПЛ"]):
        e = SIGNS_EMOJI.get(sign, "")
        keyboard.append([InlineKeyboardButton(f"🌕 Полнолуние {date} {e}{sign} {deg}°", callback_data=f"ПЛ_{i}")])
    keyboard.append([InlineKeyboardButton("← Назад", callback_data="back_to_menu")])

    text = "🔮 Выбери лунацию для персонального разбора:"
    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return MAIN_MENU

async def handle_lunation_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lunation_key = query.data
    lunations = context.user_data.get("lunations", {})
    lunation = lunations.get(lunation_key)
    if not lunation:
        await query.edit_message_text("Что-то пошло не так. /start")
        return ConversationHandler.END

    date, sign, deg, jd = lunation
    lunation_type = lunation_key.split("_")[0]
    type_name = "🌑 Новолуние" if lunation_type == "НЛ" else "🌕 Полнолуние"
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
    forecast = await get_astro_forecast(name, chart, jd, lunation_type)
    await query.message.reply_text(forecast)

    retro_block = await get_retrograde_block(name, retrograde)
    if retro_block:
        await query.message.reply_text("⚡️ <b>Твоя скрытая особенность — ретроградные планеты</b>\n\n" + retro_block, parse_mode="HTML")

    keyboard = [
        [InlineKeyboardButton("🔮 Другая лунация", callback_data="another_lunation")],
        [InlineKeyboardButton("← Главное меню", callback_data="back_to_menu")]
    ]
    await query.message.reply_text("Что дальше?", reply_markup=InlineKeyboardMarkup(keyboard))
    return MAIN_MENU

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    keyboard = [["✅ Да, согласна"], ["❌ Нет"]]
    await update.message.reply_text(
        f"Приятно познакомиться, {context.user_data['name']}! 🌟\n\nДля натальной карты мне нужны данные рождения.\nТы согласна на обработку персональных данных?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return CONSENT

async def get_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "Да" in update.message.text:
        await update.message.reply_text("Отлично! 🙏\n\nВведи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 31.03.1997", reply_markup=ReplyKeyboardRemove())
        return BIRTH_DATE
    else:
        await update.message.reply_text("Хорошо. Если передумаешь — /start", reply_markup=ReplyKeyboardRemove())
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
    save_user(user_id, {
        "name": context.user_data["name"],
        "birth_date": context.user_data["birth_date"],
        "birth_time": context.user_data["birth_time"],
        "birth_city": city
    })
    await update.message.reply_text("✨ Данные сохранены — больше не придётся вводить заново.")
    await show_main_menu(update.message, context.user_data["name"])
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
            CONSENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_consent)],
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
    print("🌙 Astro Bushido Bot v3 — меню + лунации + Меркурий ℞ + сохранение данных")
    app.run_polling()

if __name__ == "__main__":
    main()
