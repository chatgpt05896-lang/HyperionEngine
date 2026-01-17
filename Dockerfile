# =========================================
# Hyper-Tube X - Production Dockerfile
# =========================================

FROM python:3.11-slim-bookworm

# ===============================
# إعداد متغيرات البيئة
# ===============================
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# ===============================
# تثبيت متطلبات النظام (CRITICAL)
# ===============================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ===============================
# إنشاء مستخدم غير root (أمان)
# ===============================
RUN useradd -m -u 1000 hyper

WORKDIR /app

# ===============================
# نسخ ملف المكتبات
# ===============================
COPY requirements.txt .

# ===============================
# تثبيت مكتبات بايثون
# ===============================
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ===============================
# نسخ الكود
# ===============================
COPY . .

# ===============================
# صلاحيات المستخدم
# ===============================
RUN chown -R hyper:hyper /app

USER hyper

# ===============================
# فتح البورت
# ===============================
EXPOSE 8000

# ===============================
# تشغيل السيرفر
# ===============================
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
