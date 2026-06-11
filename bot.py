import logging
import httpx
import ephem
from geopy.geocoders import Nominatim
from datetime import datetime, timezone
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ========================
# НАСТРОЙКИ — ЗАМЕНИ ЗДЕСЬ
# ========================
TELEGRAM_TOKEN = "8505031201:AAFu5Y0R9hzYkAMTPP1McRbvKUz02ENMBHA"
HYDRA_API_KEY = "sk-hydra-ai-H8xe9tWhpBerfw8uOGv8ylvFMEDgS7hIqna7npENOQaPHL0Y_do2TTq6n_k0_vOv"
HYDRA_API_URL = "https://api.hydraai.ru/v1/chat/completions"
MODEL = "claude-haiku-4.5"

# ========================
# СОСТОЯНИЯ ДИАЛОГА
# ========================
NAME, CONSENT, BIRTH_DATE, BIRTH_TIME, BIRTH_CITY = range(5)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ========================
# РАСЧЁТ НАТАЛЬНОЙ КАРТЫ
# ========================
def get_coordinates(city_name: str):
    """Получить координаты города"""
    try:
        geolocator = Nominatim(user_agent="astro_bushido_bot")
        location = geolocator.geocode(city_name)
        if location:
            return location.latitude, location.longitude
        return None, None
    except Exception:
        return None, None


def calculate_natal_chart(birth_date: str, birth_time: str, city: str) -> dict:
    """Рассчитать натальную карту через Swiss Ephemeris (ephem)"""
    try:
        lat, lon = get_coordinates(city)
        if lat is None:
            return {"error": f"Не удалось найти город: {city}"}

        # Парсим дату и время
        dt_str = f"{birth_date} {birth_time}"
        dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")

        # Создаём наблюдателя
        observer = ephem.Observer()
        observer.lat = str(lat)
        observer.lon = str(lon)
        observer.date = dt.strftime("%Y/%m/%d %H:%M:%S")
        observer.epoch = ephem.J2000

        # Планеты
        planets = {
            "Солнце": ephem.Sun(observer),
            "Луна": ephem.Moon(observer),
            "Меркурий": ephem.Mercury(observer),
            "Венера": ephem.Venus(observer),
            "Марс": ephem.Mars(observer),
            "Юпитер": ephem.Jupiter(observer),
            "Сатурн": ephem.Saturn(observer),
            "Уран": ephem.Uranus(observer),
            "Нептун": ephem.Neptune(observer),
            "Плутон": ephem.Pluto(observer),
        }

        SIGNS = [
            "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
            "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"
        ]

        def get_sign_and_degree(planet_obj):
            lon_deg = float(planet_obj.hlong) * 180 / 3.14159265
            lon_deg = lon_deg % 360
            sign_idx = int(lon_deg / 30)
            degree = lon_deg % 30
            return SIGNS[sign_idx], round(degree, 1)

        # Асцендент (приблизительно через LST)
        lst = float(observer.sidereal_time()) * 180 / 3.14159265
        asc_lon = (lst + float(observer.lon) * 180 / 3.14159265) % 360
        asc_sign_idx = int(asc_lon / 30)
        asc_degree = round(asc_lon % 30, 1)
        ascendant = f"{SIGNS[asc_sign_idx]} {asc_degree}°"

        # Дома (упрощённо через равнодомную систему от Асцендента)
        def get_house(planet_lon_deg):
            asc = asc_lon
            diff = (planet_lon_deg - asc) % 360
            house = int(diff / 30) + 1
            return house

        chart = {}
        for name, planet in planets.items():
            sign, degree = get_sign_and_degree(planet)
            p_lon = float(planet.hlong) * 180 / 3.14159265 % 360
            house = get_house(p_lon)
            chart[name] = f"{sign} {degree}°, {house} дом"

        chart["Асцендент"] = ascendant
        chart["Город"] = city
        chart["Дата"] = birth_date
        chart["Время"] = birth_time
        chart["Координаты"] = f"{round(lat,2)}°N, {round(lon,2)}°E"

        return chart

    except Exception as e:
        return {"error": str(e)}


def get_next_new_moon() -> str:
    """Найти следующее новолуние"""
    now = ephem.now()
    next_nm = ephem.next_new_moon(now)
    dt = ephem.Date(next_nm).datetime()
    
    # Знак луны на новолуние
    observer = ephem.Observer()
    observer.date = next_nm
    moon = ephem.Moon(observer)
    SIGNS = [
        "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
        "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"
    ]
    lon_deg = float(moon.hlong) * 180 / 3.14159265 % 360
    sign = SIGNS[int(lon_deg / 30)]
    degree = round(lon_deg % 30, 1)
    
    return f"{dt.strftime('%d.%m.%Y')}, Луна в {sign} {degree}°"


def get_last_full_moon() -> str:
    """Найти предыдущее полнолуние"""
    now = ephem.now()
    last_fm = ephem.previous_full_moon(now)
    dt = ephem.Date(last_fm).datetime()
    
    observer = ephem.Observer()
    observer.date = last_fm
    moon = ephem.Moon(observer)
    SIGNS = [
        "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
        "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"
    ]
    lon_deg = float(moon.hlong) * 180 / 3.14159265 % 360
    sign = SIGNS[int(lon_deg / 30)]
    degree = round(lon_deg % 30, 1)
    
    return f"{dt.strftime('%d.%m.%Y')}, Луна в {sign} {degree}°"


# ========================
# ЗАПРОС К CLAUDE
# ========================
async def get_astro_forecast(name: str, chart: dict) -> str:
    """Получить прогноз от Claude"""
    
    new_moon = get_next_new_moon()
    full_moon = get_last_full_moon()
    
    chart_text = "\n".join([f"  {k}: {v}" for k, v in chart.items()])
    
    system_prompt = """Ты астрологический ассистент, работающий по методологии астролога Екатерины.

ТВОИ ПРАВИЛА:
1. Всегда указывай точное положение планеты: знак зодиака + градус + номер дома (например: Луна в Близнецах 24°, 7 дом)
2. Астрология — это язык энергетических взаимодействий, не предсказание
3. Интерпретация идёт от реальных событий жизни к карте, а не наоборот
4. Транзиты подтверждают тренды которые уже идут — они не создают события с нуля
5. Всегда смотри на взаимодействие планет, а не на каждую по отдельности

ФОРМАТ ОТВЕТА:
- Пиши по-русски, тепло и глубоко
- Обращайся к человеку по имени
- Всегда конкретно: знак + градус + дом
- Максимум 4-5 абзацев
- Сначала общая энергетика карты, потом прогноз по новолунию и полнолунию"""

    user_prompt = f"""Имя: {name}

Натальная карта:
{chart_text}

Следующее новолуние: {new_moon}
Предыдущее полнолуние: {full_moon}

Составь персональный астрологический прогноз по этим лунациям. Обязательно укажи в каком доме натальной карты происходит каждое событие и что это значит для этого человека."""

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
            elif "error" in data:
                return f"Ошибка API: {data['error']}"
            else:
                return f"Неожиданный ответ: {data}"
    except Exception as e:
        return f"Ошибка подключения: {str(e)}"


# ========================
# HANDLERS TELEGRAM
# ========================
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
            "Хорошо, без данных не могу составить прогноз.\n"
            "Если передумаешь — напиши /start",
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
            "Если не знаешь точное время — напиши приблизительное или 12:00"
        )
        return BIRTH_TIME
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Введи дату так: ДД.ММ.ГГГГ\nНапример: 31.03.1997"
        )
        return BIRTH_DATE


async def get_birth_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_text = update.message.text.strip()
    try:
        datetime.strptime(time_text, "%H:%M")
        context.user_data["birth_time"] = time_text
        await update.message.reply_text(
            "И последнее — город рождения:\nНапример: Москва или Nikol'sk"
        )
        return BIRTH_CITY
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Введи время так: ЧЧ:ММ\nНапример: 23:48"
        )
        return BIRTH_TIME


async def get_birth_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = update.message.text.strip()
    context.user_data["birth_city"] = city
    name = context.user_data["name"]
    
    await update.message.reply_text(
        f"✨ Считаю натальную карту для {name}...\n\nЭто займёт несколько секунд 🔮"
    )
    
    # Считаем карту
    chart = calculate_natal_chart(
        context.user_data["birth_date"],
        context.user_data["birth_time"],
        city
    )
    
    if "error" in chart:
        await update.message.reply_text(
            f"Не удалось рассчитать карту: {chart['error']}\n"
            "Попробуй написать город по-английски или начни заново /start"
        )
        return ConversationHandler.END
    
    await update.message.reply_text("🌙 Запрашиваю прогноз у звёзд...")
    
    # Получаем прогноз
    forecast = await get_astro_forecast(name, chart)
    
    await update.message.reply_text(forecast)
    await update.message.reply_text(
        "🌟 Если хочешь новый прогноз — напиши /start"
    )
    
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Отменено. Напиши /start чтобы начать заново.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ========================
# ЗАПУСК БОТА
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
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    app.add_handler(conv_handler)
    
    print("🌙 Astro Bushido Bot запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
