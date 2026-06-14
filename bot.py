import logging
import httpx
import swisseph as swe
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ========================
# НАСТРОЙКИ
# ========================
TELEGRAM_TOKEN = "8505031201:AAFu5Y0R9hzYkAMTPP1McRbvKUz02ENMBHA"
HYDRA_API_KEY = "sk-hydra-ai-H8xe9tWhpBerfw8uOGv8ylvFMEDgS7hIqna7npENOQaPHL0Y_do2TTq6n_k0_vOv"
HYDRA_API_URL = "https://api.hydraai.ru/v1/chat/completions"
MODEL = "claude-haiku-4.5"

NAME, CONSENT, BIRTH_DATE, BIRTH_TIME, BIRTH_CITY = range(5)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]

tf = TimezoneFinder()


def fmt_position(lon):
    """Долгота -> 'Знак градус°'"""
    lon = lon % 360
    return SIGNS[int(lon / 30)], round(lon % 30, 1)


def get_house(planet_lon, cusps):
    """Определить дом планеты по куспидам Плацидуса"""
    planet_lon = planet_lon % 360
    for i in range(12):
        c1 = cusps[i] % 360
        c2 = cusps[(i + 1) % 12] % 360
        if c1 <= c2:
            if c1 <= planet_lon < c2:
                return i + 1
        else:  # переход через 0° Овна
            if planet_lon >= c1 or planet_lon < c2:
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
    """Натальная карта: Swiss Ephemeris + дома Плацидуса + автоопределение часового пояса"""
    try:
        lat, lon_geo = get_coordinates(city)
        if lat is None:
            return {"error": f"Не удалось найти город: {city}"}

        # Часовой пояс по координатам (с историей переходов времени)
        tzname = tf.timezone_at(lat=lat, lng=lon_geo)
        if not tzname:
            return {"error": "Не удалось определить часовой пояс"}

        local_dt = datetime.strptime(f"{birth_date} {birth_time}", "%d.%m.%Y %H:%M")
        local_dt = local_dt.replace(tzinfo=ZoneInfo(tzname))
        utc = local_dt.astimezone(ZoneInfo("UTC"))

        jd = swe.julday(utc.year, utc.month, utc.day, utc.hour + utc.minute / 60 + utc.second / 3600)

        # Дома Плацидуса
        cusps, ascmc = swe.houses(jd, lat, lon_geo, b'P')

        planet_ids = {
            "Солнце": swe.SUN, "Луна": swe.MOON, "Меркурий": swe.MERCURY,
            "Венера": swe.VENUS, "Марс": swe.MARS, "Юпитер": swe.JUPITER,
            "Сатурн": swe.SATURN, "Уран": swe.URANUS, "Нептун": swe.NEPTUNE,
            "Плутон": swe.PLUTO,
        }

        chart = {}
        planet_lons = {}
        retrograde = []  # список ретроградных планет
        for name, pid in planet_ids.items():
            res = swe.calc_ut(jd, pid)[0]
            p_lon = res[0]
            speed = res[3]  # скорость по долготе: <0 = ретроградная
            planet_lons[name] = p_lon
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
        chart["Город"] = f"{city} ({tzname})"
        chart["Дата"] = birth_date
        chart["Время"] = f"{birth_time} (местное)"
        chart["_cusps"] = list(cusps)  # для расчёта домов лунаций
        chart["_retrograde"] = retrograde  # для блока ретроградности

        return chart
    except Exception as e:
        return {"error": str(e)}


def get_moon_event(event_jd):
    """Позиция Луны в момент лунации"""
    moon_lon = swe.calc_ut(event_jd, swe.MOON)[0][0]
    sign, degree = fmt_position(moon_lon)
    y, m, d, h = swe.revjul(event_jd)
    return moon_lon, f"{int(d):02d}.{int(m):02d}.{int(y)}", sign, degree


def find_next_new_moon():
    """Следующее новолуние (поиск по фазе)"""
    now = datetime.utcnow()
    jd = swe.julday(now.year, now.month, now.day, now.hour + now.minute / 60)
    step = 0.5
    prev_diff = None
    for i in range(120):
        t = jd + i * step
        sun = swe.calc_ut(t, swe.SUN)[0][0]
        moon = swe.calc_ut(t, swe.MOON)[0][0]
        diff = (moon - sun) % 360
        if prev_diff is not None and prev_diff > 300 and diff < 60:
            # уточняем
            lo, hi = t - step, t
            for _ in range(50):
                mid = (lo + hi) / 2
                s = swe.calc_ut(mid, swe.SUN)[0][0]
                m = swe.calc_ut(mid, swe.MOON)[0][0]
                d = (m - s) % 360
                if d > 300:
                    lo = mid
                else:
                    hi = mid
            return (lo + hi) / 2
        prev_diff = diff
    return None


def find_prev_full_moon():
    """Предыдущее полнолуние"""
    now = datetime.utcnow()
    jd = swe.julday(now.year, now.month, now.day, now.hour + now.minute / 60)
    step = 0.5
    prev_diff = None
    for i in range(120):
        t = jd - i * step
        sun = swe.calc_ut(t, swe.SUN)[0][0]
        moon = swe.calc_ut(t, swe.MOON)[0][0]
        diff = (moon - sun) % 360
        if prev_diff is not None and diff < 180 <= prev_diff:
            lo, hi = t, t + step
            for _ in range(50):
                mid = (lo + hi) / 2
                s = swe.calc_ut(mid, swe.SUN)[0][0]
                m = swe.calc_ut(mid, swe.MOON)[0][0]
                d = (m - s) % 360
                if d < 180:
                    lo = mid
                else:
                    hi = mid
            return (lo + hi) / 2
        prev_diff = diff
    return None


async def get_retrograde_block(name, retrograde):
    """Отдельный блок про ретроградные планеты (Меркурий/Венера/Марс) — бонус-фича"""
    # Берём только личные планеты — они дают самое заметное 'не как у всех'
    personal = [r for r in retrograde if r[0] in ("Меркурий", "Венера", "Марс")]
    if not personal:
        return None

    retro_list = "\n".join([f"  {n}: {s} {d}°, {h} дом ℞" for n, s, d, h in personal])

    system_prompt = """Ты астролог Екатерина. Пишешь блок про РЕТРОГРАДНЫЕ планеты в натальной карте.

ТВОЙ ВЗГЛЯД НА РЕТРОГРАДНОСТЬ (это ключевое):
- Ретроградная планета УСИЛЕНА, а не сломана — в момент рождения она была ближе всего к Земле, её энергия концентрированная
- Она работает ВНУТРЬ, а не наружу: тема переживается глубже, интенсивнее, более лично
- Это скрытая суперсила, которую сам человек обычно считает своей слабостью
- Человеку с ретро-планетой нужно больше времени, он обрабатывает иначе — и в этом его особость, а не дефект
- Это то, что отличает его от людей без ретроградности — тихая особенность, которая есть не у всех

ЧТО ОЗНАЧАЕТ КАЖДАЯ:
- Меркурий ℞: мышление и речь работают внутрь — человек думает глубже, переосмысляет, ему нужно проговорить про себя прежде чем вовне. Часто кажется, что «медленно соображает», а на деле обрабатывает глубже всех.
- Венера ℞: любовь и ценности переживаются внутрь — нестандартное отношение к близости, деньгам, красоте. Человек любит иначе, ценит иначе, ему сложно с шаблонной романтикой.
- Марс ℞: действие и желание развёрнуты внутрь — энергия не выплёскивается сразу, копится. Кажется слабостью («не могу пробить, не агрессивный»), но это стратегическая, выдержанная сила.

ФОРМАТ:
- По-русски, тепло, обращайся по имени
- Подай как ВАУ-бонус, скрытую особенность
- Знак + градус + дом для каждой
- 2-3 абзаца, не больше"""

    user_prompt = f"""Имя: {name}

Ретроградные личные планеты в карте:
{retro_list}

Напиши тёплый, глубокий блок про эти ретроградные планеты как про скрытую суперсилу этого человека. Покажи, чем он отличается от людей без ретроградности."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                HYDRA_API_URL,
                headers={
                    "Authorization": f"Bearer {HYDRA_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": MODEL,
                    "max_tokens": 800,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                }
            )
            data = response.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None


async def get_astro_forecast(name, chart):
    cusps = chart.pop("_cusps", None)
    chart.pop("_retrograde", None)  # убираем служебное поле из текста карты

    nm_jd = find_next_new_moon()
    fm_jd = find_prev_full_moon()

    nm_text, fm_text = "", ""
    if nm_jd and cusps:
        moon_lon, date_s, sign, deg = get_moon_event(nm_jd)
        house = get_house(moon_lon, cusps)
        nm_text = f"{date_s}, Луна в {sign} {deg}°, попадает в {house} дом натальной карты"
    if fm_jd and cusps:
        moon_lon, date_s, sign, deg = get_moon_event(fm_jd)
        house = get_house(moon_lon, cusps)
        fm_text = f"{date_s}, Луна в {sign} {deg}°, попадает в {house} дом натальной карты"

    chart_text = "\n".join([f"  {k}: {v}" for k, v in chart.items()])

    system_prompt = """Ты астрологический ассистент по методологии Екатерины.

ПРАВИЛА:
1. Всегда указывай: знак зодиака + градус + номер дома (например: Луна в Близнецах 24°, 7 дом)
2. Астрология — язык энергетических взаимодействий, не предсказание
3. Интерпретация идёт от реальных событий жизни к карте
4. Лунации завершают/запускают тренды которые уже идут — они не создают события с нуля
5. Смотри на взаимодействие лунации с натальными планетами: если лунация близко к натальной планете (орб до 6°) — обязательно укажи это соединение

ФОРМАТ:
- По-русски, тепло и глубоко
- Обращайся по имени
- Знак + градус + дом всегда — дома лунаций уже рассчитаны точно, используй именно их
- 4-5 абзацев максимум"""

    user_prompt = f"""Имя: {name}

Натальная карта (дома Плацидуса, рассчитано Swiss Ephemeris):
{chart_text}

Предыдущее полнолуние: {fm_text}
Следующее новолуние: {nm_text}

Составь персональный прогноз по этим лунациям. Дом каждой лунации уже указан точно — опирайся на него. Проверь соединения лунаций с натальными планетами."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                HYDRA_API_URL,
                headers={
                    "Authorization": f"Bearer {HYDRA_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": MODEL,
                    "max_tokens": 1500,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                }
            )
            data = response.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            else:
                return f"Ошибка: {data}"
    except Exception as e:
        return f"Ошибка подключения: {str(e)}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🌙 Привет! Я Astro Bushido Bot.\n\n"
        "Составлю твой персональный астрологический прогноз на ближайшее новолуние и последнее полнолуние.\n\n"
        "Как тебя зовут?"
    )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    keyboard = [["✅ Да, согласна"], ["❌ Нет"]]
    await update.message.reply_text(
        f"Приятно познакомиться, {context.user_data['name']}! 🌟\n\n"
        "Для составления натальной карты мне нужны твои данные рождения.\n"
        "Ты согласна на обработку персональных данных?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return CONSENT


async def get_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "Да" in update.message.text:
        await update.message.reply_text(
            "Отлично! 🙏\n\nВведи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 31.03.1997",
            reply_markup=ReplyKeyboardRemove()
        )
        return BIRTH_DATE
    else:
        await update.message.reply_text(
            "Хорошо. Если передумаешь — напиши /start",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END


async def get_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_text = update.message.text.strip()
    try:
        datetime.strptime(date_text, "%d.%m.%Y")
        context.user_data["birth_date"] = date_text
        await update.message.reply_text(
            "Теперь введи время рождения в формате ЧЧ:ММ\nНапример: 23:48\n\n"
            "Если не знаешь точное время — напиши 12:00"
        )
        return BIRTH_TIME
    except ValueError:
        await update.message.reply_text("Неверный формат. Введи дату так: ДД.ММ.ГГГГ\nНапример: 31.03.1997")
        return BIRTH_DATE


async def get_birth_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_text = update.message.text.strip().replace(".", ":")
    try:
        datetime.strptime(time_text, "%H:%M")
        context.user_data["birth_time"] = time_text
        await update.message.reply_text("И последнее — город рождения:\nНапример: Москва")
        return BIRTH_CITY
    except ValueError:
        await update.message.reply_text("Неверный формат. Введи время так: ЧЧ:ММ\nНапример: 23:48")
        return BIRTH_TIME


async def get_birth_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = update.message.text.strip()
    name = context.user_data["name"]

    await update.message.reply_text(f"✨ Считаю натальную карту для {name}...\n\nЭто займёт несколько секунд 🔮")

    chart = calculate_natal_chart(
        context.user_data["birth_date"],
        context.user_data["birth_time"],
        city
    )

    if "error" in chart:
        await update.message.reply_text(
            f"Не удалось рассчитать карту: {chart['error']}\n"
            "Попробуй написать город иначе или начни заново /start"
        )
        return ConversationHandler.END

    # Достаём ретро ДО того как get_astro_forecast очистит служебные поля
    retrograde = chart.get("_retrograde", [])

    await update.message.reply_text("🌙 Запрашиваю прогноз у звёзд...")
    forecast = await get_astro_forecast(name, chart)
    await update.message.reply_text(forecast)

    # Бонус-блок: ретроградные планеты (если есть личные ретро)
    retro_block = await get_retrograde_block(name, retrograde)
    if retro_block:
        await update.message.reply_text(
            "⚡️ <b>Твоя скрытая особенность — ретроградные планеты</b>\n\n" + retro_block,
            parse_mode="HTML"
        )

    await update.message.reply_text("🌟 Если хочешь новый прогноз — напиши /start")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. Напиши /start чтобы начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


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
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)
    print("🌙 Astro Bushido Bot запущен (Swiss Ephemeris + Placidus)!")
    app.run_polling()


if __name__ == "__main__":
    main()
