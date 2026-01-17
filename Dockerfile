# =========================================
# Hyper-Tube X — Production Dockerfile (2026)
# Fly.io + yt-dlp + aria2 + ffmpeg
# =========================================

FROM python:3.11-slim-bookworm

# ===============================
# Environment (Performance + Stability)
# ===============================
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# ===============================
# System Dependencies (CRITICAL)
# ===============================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    ca-certificates \
    build-essential \
    tini \
    && rm -rf /var/lib/apt/lists/*

# ===============================
# Non-root user (Security)
# ===============================
RUN useradd -m -u 1000 hyper

WORKDIR /app

# ===============================
# Python Dependencies
# ===============================
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ===============================
# Application Code
# ===============================
COPY . .

# ===============================
# Permissions
# ===============================
RUN chown -R hyper:hyper /app
USER hyper

# ===============================
# Network
# ===============================
EXPOSE 8080

# ===============================
# Init + Run (Fly.io Compatible)
# ===============================
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "uvloop", "--proxy-headers"]
