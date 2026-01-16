# استخدام نسخة بايثون خفيفة وسريعة
FROM python:3.9-slim

# تحديث النظام وتثبيت FFmpeg (مهم جداً لـ yt-dlp)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# إعداد مسار العمل
WORKDIR /app

# نسخ ملف المكتبات وتثبيتها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع
COPY . .

# فتح المنفذ 8080
EXPOSE 8080

# الأمر الرسمي لتشغيل السيرفر (Uvicorn)
# server:app معناها: شغل ملف server.py وهات منه المتغير app
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
