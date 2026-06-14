FROM python:3.11-slim

# Системная библиотека, которую требует pyswisseph (Swiss Ephemeris)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libsqlite3-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — для кэширования слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код
COPY . .

CMD ["python", "bot.py"]
