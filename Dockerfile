FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# نظامية dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libopus0 libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

# نسخة عمل
WORKDIR /app
COPY . /app

# تثبيت الباكيجات
RUN python -m pip install --upgrade pip setuptools wheel
RUN python -m pip install -r requirements.txt

# إعداد متغير للـ bot callable (عدّله حسب سورسك)
ENV BOT_PLAY_CALLABLE="AnnieXMedia.core.call:join_call"
ENV PORT=8080

# تشغيل
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
