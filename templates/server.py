# -*- coding: utf-8 -*-
# Hyperion Media Server | Enterprise Grade Extractor
# Architecture: Async IO / FastAPI / In-Memory Caching / Signal Processing
# -------------------------------------------------------------------------

import os
import sys
import time
import json
import ujson
import psutil
import shutil
import asyncio
import logging
import platform
import contextlib
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Form, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache
from yt_dlp import YoutubeDL

# =========================================================================
# [CORE] CONFIGURATION & ENVIRONMENT
# =========================================================================

class Config:
    PROJECT_NAME = "Hyperion Engine"
    VERSION = "9.0.2-Stable"
    HOST = "0.0.0.0"
    PORT = int(os.environ.get("PORT", 8080))
    WORKERS = 4
    # إعدادات الكاش (يحفظ نتائج البحث لمدة 30 دقيقة لتسريع الاستجابة)
    CACHE_TTL = 1800  
    CACHE_MAX_SIZE = 1000
    # مسارات النظام
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
    COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

# =========================================================================
# [CORE] LOGGING & OBSERVABILITY
# =========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | HYPERION | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
LOGGER = logging.getLogger("Hyperion")

# =========================================================================
# [CORE] STATE MANAGEMENT (THE BRAIN)
# =========================================================================

class SystemState:
    """
    مخزن الحالة الحية للسيرفر
    """
    def __init__(self):
        self.is_active: bool = True
        self.download_speed_cap: int = 0  # 0 = Unlimited
        self.start_timestamp: float = time.time()
        self.request_counter: int = 0
        self.error_counter: int = 0
        self.bytes_transferred: int = 0
        self.active_processes: int = 0
        self.last_latency: float = 0.0

    @property
    def uptime(self) -> str:
        delta = timedelta(seconds=int(time.time() - self.start_timestamp))
        return str(delta)

STATE = SystemState()

# الذاكرة المؤقتة للنتائج (Speed Booster)
RESULT_CACHE = TTLCache(maxsize=Config.CACHE_MAX_SIZE, ttl=Config.CACHE_TTL)

# =========================================================================
# [MODULE] YOUTUBE EXTRACTION ENGINE
# =========================================================================

class ExtractionEngine:
    """
    المسؤول عن التعامل مع yt-dlp بذكاء وتخطي الحماية
    """
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=10)

    def _get_base_opts(self) -> Dict[str, Any]:
        """
        بناء إعدادات التنزيل ديناميكياً بناءً على حالة السيرفر
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "geo_bypass_country": "US",
            "nocheckcertificate": True,
            "socket_timeout": 10,
            "retries": 5,
            "extractor_args": {
                "youtube": {
                    "skip": ["dash", "hls"],
                    "player_client": ["android", "ios", "web"]
                }
            }
        }
        
        # حقن الكوكيز لو موجودة
        if os.path.exists(Config.COOKIES_FILE) and os.path.getsize(Config.COOKIES_FILE) > 0:
            opts["cookiefile"] = Config.COOKIES_FILE
        
        # تفعيل محدد السرعة
        if STATE.download_speed_cap > 0:
            opts["ratelimit"] = f"{STATE.download_speed_cap}K"
            
        return opts

    def _process_sync(self, url: str, mode: str) -> Dict[str, Any]:
        """
        العملية المتزامنة التي يتم تشغيلها داخل Thread منفصل
        """
        opts = self._get_base_opts()
        
        if mode == "audio":
            opts["format"] = "bestaudio/best"
        elif mode == "video":
            opts["format"] = "best[ext=mp4]/best"
        elif mode == "search":
            opts["format"] = "bestaudio/best"
            opts["noplaylist"] = True
            opts["extract_flat"] = True

        with YoutubeDL(opts) as ydl:
            if mode == "search":
                return ydl.extract_info(f"ytsearch5:{url}", download=False)
            return ydl.extract_info(url, download=False)

    async def extract(self, url: str, mode: str = "audio") -> Dict[str, Any]:
        """
        الواجهة غير المتزامنة (Async Wrapper)
        """
        if not STATE.is_active:
            raise HTTPException(status_code=503, detail="HYPERION_SERVICE_SUSPENDED")

        loop = asyncio.get_running_loop()
        start_time = time.perf_counter()
        
        try:
            STATE.active_processes += 1
            # تشغيل في خلفية المعالج
            result = await loop.run_in_executor(self.executor, self._process_sync, url, mode)
            
            end_time = time.perf_counter()
            STATE.last_latency = end_time - start_time
            STATE.request_counter += 1
            return result
            
        except Exception as e:
            STATE.error_counter += 1
            LOGGER.error(f"Extraction Error [{mode}]: {str(e)}")
            raise e
        finally:
            STATE.active_processes = max(0, STATE.active_processes - 1)

ENGINE = ExtractionEngine()

# =========================================================================
# [MODULE] SYSTEM DIAGNOSTICS
# =========================================================================

class Diagnostics:
    @staticmethod
    def get_full_report():
        # Memory
        mem = psutil.virtual_memory()
        # CPU
        cpu = psutil.cpu_percent(interval=None)
        # Network
        net = psutil.net_io_counters()
        # Cookies Health
        cookie_status = "MISSING 🔴"
        if os.path.exists(Config.COOKIES_FILE):
            sz = os.path.getsize(Config.COOKIES_FILE)
            cookie_status = "HEALTHY 🟢" if sz > 100 else "CORRUPTED ⚠️"

        return {
            "status": "OPERATIONAL 🟢" if STATE.is_active else "HALTED 🔴",
            "uptime": STATE.uptime,
            "cpu_load": f"{cpu}%",
            "ram_usage": f"{mem.percent}%",
            "ram_details": f"{mem.used >> 20}MB / {mem.total >> 20}MB",
            "net_sent": f"{net.bytes_sent >> 20} MB",
            "net_recv": f"{net.bytes_recv >> 20} MB",
            "requests_handled": STATE.request_counter,
            "failed_requests": STATE.error_counter,
            "latency_ms": f"{STATE.last_latency * 1000:.1f}ms",
            "cookies": cookie_status,
            "speed_limit": "UNLIMITED ⚡" if STATE.download_speed_cap == 0 else f"{STATE.download_speed_cap} KB/s",
            "active_threads": STATE.active_processes,
            "system_os": f"{platform.system()} {platform.release()}"
        }

# =========================================================================
# [FRAMEWORK] FASTAPI APPLICATION
# =========================================================================

app = FastAPI(
    title=Config.PROJECT_NAME,
    version=Config.VERSION,
    docs_url=None, # إخفاء التوثيق للأمان
    redoc_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory=Config.TEMPLATE_DIR)

# =========================================================================
# [ENDPOINTS] FRONTEND & DASHBOARD
# =========================================================================

@app.get("/", response_class=HTMLResponse)
async def root_interface(request: Request):
    return templates.TemplateResponse("interface.html", {"request": request})

@app.get("/api/v1/telemetry")
async def get_telemetry():
    return Diagnostics.get_full_report()

@app.post("/api/v1/admin/toggle")
async def toggle_service():
    STATE.is_active = not STATE.is_active
    LOGGER.warning(f"ADMIN: Service state changed to {STATE.is_active}")
    return {"ok": True, "active": STATE.is_active}

@app.post("/api/v1/admin/throttle")
async def throttle_service(speed: int = Form(...)):
    STATE.download_speed_cap = speed
    LOGGER.warning(f"ADMIN: Speed limit set to {speed} KB/s")
    return {"ok": True, "limit": speed}

# =========================================================================
# [ENDPOINTS] BOT INTEGRATION (THE API)
# =========================================================================

@app.get("/song/{video_id}")
async def fetch_audio(video_id: str):
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        # Check Cache
        cache_key = f"audio:{video_id}"
        if cache_key in RESULT_CACHE:
            LOGGER.info(f"Cache Hit: {video_id}")
            return RESULT_CACHE[cache_key]

        data = await ENGINE.extract(url, "audio")
        
        response = {
            "status": "done",
            "title": data.get('title'),
            "link": data.get('url'),
            "duration": data.get('duration'),
            "format": "webm/opus",
            "engine": "Hyperion"
        }
        
        # Update Cache
        RESULT_CACHE[cache_key] = response
        return response

    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

@app.get("/video/{video_id}")
async def fetch_video(video_id: str):
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        data = await ENGINE.extract(url, "video")
        
        return {
            "status": "done",
            "title": data.get('title'),
            "link": data.get('url'),
            "format": "mp4",
            "quality": "HD",
            "engine": "Hyperion"
        }
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

@app.get("/search")
async def perform_search(q: str):
    try:
        # Smart caching for search
        cache_key = f"search:{q}"
        if cache_key in RESULT_CACHE:
            return RESULT_CACHE[cache_key]

        data = await ENGINE.extract(q, "search")
        results = []
        
        if 'entries' in data:
            for entry in data['entries']:
                if entry:
                    results.append({
                        "id": entry.get('id'),
                        "title": entry.get('title'),
                        "duration": entry.get('duration'),
                        "thumb": entry.get('thumbnail'),
                        "uploader": entry.get('uploader')
                    })
        
        resp = {"status": "ok", "results": results}
        RESULT_CACHE[cache_key] = resp
        return resp
        
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# =========================================================================
# [CORE] SERVER LIFECYCLE
# =========================================================================

@app.on_event("startup")
async def startup_sequence():
    LOGGER.info(">>> HYPERION ENGINE INITIALIZING...")
    LOGGER.info(f"Detected Cores: {psutil.cpu_count()}")
    if os.path.exists(Config.COOKIES_FILE):
        LOGGER.info("✅ Cookies File Loaded Successfully.")
    else:
        LOGGER.warning("⚠️ CRITICAL: Cookies File Not Found! Performance will be degraded.")

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=Config.HOST,
        port=Config.PORT,
        workers=Config.WORKERS,
        log_level="warning",
        timeout_keep_alive=30
    )
