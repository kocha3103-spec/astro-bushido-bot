import logging
import os
import httpx
import ephem
from geopy.geocoders import Nominatim
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ========================
# НАСТРОЙКИ
# ========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
HYDRA_API_KEY = os.environ.get("HYDRA_API_KEY", "")
HYDRA_API_URL = "https://api.hydraai.ru/v1/chat/completions"
MODEL = "claude-haiku-4.5"

NAME, CONSENT, BIRTH_DATE, BIRTH_TIME, BIRTH_CITY = range(5)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]

def get_coordinates(city_name):
        try:
                    geolocator = Nominatim(user_agent="astro_bushido_bot")
                    location = geolocator.geocode(city_name)
                    if location:
                                    return location.latitude, location.longitude
                                return None, None
except Exception:
        return None, None

def get_sign(lon_deg):
        lon_deg = lon_deg % 360
        return SIGNS[int(lon_deg / 30)], round(lon_deg % 30, 1)

def calculate_natal_chart(birth_date, birth_time, city):
        try:
                    lat, lon = get_coordinates(city)
                    if lat is None:
                                    return {"error": f"Не удалось найти город: {city}"}
                                dt = datetime.strptime(f"{birth_date} {birth_time}", "%d.%m.%Y %H:%M")
                    observer = ephem.Observer()
                    observer.lat = str(lat)
                    observer.lon = str(lon)
                    observer.date = dt.strftime("%Y/%m/%d %H:%M:%S")
                    observer.epoch = ephem.J2000
                    planets_list = {
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
                    lst = float(observer.sidereal_time()) * 180 / 3.14159265
                    asc_lon = (lst + float(observer.lon) * 180 / 3.14159265) % 360
                    asc_sign, asc_deg = get_sign(asc_lon)
                    def get_house(planet_lon):
                                    diff = (planet_lon % 360 - asc_lon) % 360
                                    return int(diff / 30) + 1
                                chart = {}
                    for name, planet in planets_list.items():
                                    p_lon = float(planet.hlong) * 180 / 3.14159265
                                    sign, degree = get_sign(p_lon)
                                    house = get_house(p_lon)
                                    chart[name] = f"{sign} {degree}°, {house} дом"
                                chart["Асцендент"] = f"{asc_sign} {asc_deg}°"
                    chart["Город"] = city
                    chart["Дата"] = birth_date
                    chart["Время"] = birth_time
                    return chart
except Exception as e:
        return {"error": str(e)}

def get_next_new_moon():
        next_nm = ephem.next_new_moon(ephem.now())
        dt = ephem.Date(next_nm).datetime()
        obs = ephem.Observer()
        obs.date = next_nm
        moon = ephem.Moon(obs)
        lon = float(moon.hlong) * 180 / 3.14159265
        sign, degree = get_sign(lon)
        return f"{dt.strftime('%d.%m.%Y')}, Луна в {sign} {degree}°"

def get_last_full_moon():
        last_fm = ephem.previous_full_moon(ephem.now())
        dt = ephem.Date(last_fm).datetime()
        obs = ephem.Observer()
        obs.date = last_fm
        moon = ephem.Moon(obs)
        lon = float(moon.hlong) * 180 / 3.14159265
        sign, degree = get_sign(lon)
        return f"{dt.strftime('%d.%m.%Y')}, Луна в {sign} {degree}°"

async def get_astro_forecast(name, chart):
        new_moon = get_next_new_moon()
        full_moon = get_last_full_moon()
        chart_text = "\n".join([f"  {k}: {v}" for k, v in chart.items()])
        system_prompt = """Ты астрологический ассистент по методологии Екатерины.

    ПРАВИЛА:
    1. Всегда указывай: знак зодиака + градус + номер дома (например: Луна в Близнецах 24°, 7 дом)
    2. Астрология — язык энергетических взаимодействий, не предсказание
    3. Интерпретация идёт от реальных событий жизни к карте
    4. Транзиты подтверждают тренды которые уже идут
    5. Смотри на взаимодействие планет

    ФОРМАТ:
    - По-русски, тепло и глубоко
    - Обращайся по имени
    - Знак + градус + дом всегда
    - 4-5 абзацев максимум"""
        user_prompt = f"""Имя: {name}

    Натальная карта:
    {chart_text}

    Следующее новолуние: {new_moon}
    Предыдущее полнолуние: {full_moon}

    Составь персональный прогноз по лунациям. Укажи в каком доме натальной карты происходит каждое событие."""
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
        time_text = update.message.text.strip()
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
    context.user_data["birth_city"] = city
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
                                "Попробуй написать город по-английски или начни заново /start"
                )
                return ConversationHandler.END
            await update.message.reply_text("🌙 Запрашиваю прогноз у звёзд...")
    forecast = await get_astro_forecast(name, chart)
    await update.message.reply_text(forecast)
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
        print("🌙 Astro Bushido Bot запущен!")
        app.run_polling()

if __name__ == "__main__":
        main()
