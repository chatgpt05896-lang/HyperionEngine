# -*- coding: utf-8 -*-
# Hyperion Media Server | Enterprise Grade Ultimate Edition
# Features: Auth, Uploads, API Keys, In-Browser Player, Fast Caching
# -------------------------------------------------------------------------

import os
import sys
import time
import uuid
import shutil
import asyncio
import logging
import platform
import secrets
from typing import Dict, Any, List
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import uvicorn
import psutil
from fastapi import FastAPI, Request, HTTPException, Form, Depends, UploadFile, File, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from cachetools import TTLCache
from yt_dlp import YoutubeDL

# =========================================================================
# [CORE] CONFIGURATION
# =========================================================================

class Config:
    PROJECT_NAME = "Hyperion Ultimate"
    VERSION = "10.0-Titanium"
    HOST = "0.0.0.0"
    PORT = int(os.environ.get("PORT", 8080))
    WORKERS = 4
    CACHE_TTL = 1800  # 30 Minutes
    CACHE_MAX_SIZE = 2000
    
    # Auth Credentials
    ADMIN_USER = "admin"
    ADMIN_PASS = "asdfghjkl05896"

    # Paths
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
    COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
    UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

    # Ensure directories exist
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(TEMPLATE_DIR, exist_ok=True)

# =========================================================================
# [CORE] STATE & STORAGE
# =========================================================================

class SystemState:
    def __init__(self):
        self.is_active = True
        self.download_speed_cap = 0
        self.start_timestamp = time.time()
        self.request_counter = 0
        self.download_counter = 0
        self.uploaded_files_count = len(os.listdir(Config.UPLOAD_DIR))
        self.api_keys = {"default_key": "active"}  # Simple in-memory key store

    @property
    def uptime(self):
        return str(timedelta(seconds=int(time.time() - self.start_timestamp)))

STATE = SystemState()
RESULT_CACHE = TTLCache(maxsize=Config.CACHE_MAX_SIZE, ttl=Config.CACHE_TTL)
security = HTTPBasic()
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("Hyperion")

# =========================================================================
# [MODULE] ENGINE
# =========================================================================

class ExtractionEngine:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=16) # Boosted workers

    def _get_opts(self, mode):
        opts = {
            "quiet": True, "no_warnings": True, "geo_bypass": True,
            "nocheckcertificate": True, "socket_timeout": 15,
            "extractor_args": {"youtube": {"skip": ["dash", "hls"], "player_client": ["android", "ios", "web"]}}
        }
        if os.path.exists(Config.COOKIES_FILE): opts["cookiefile"] = Config.COOKIES_FILE
        if STATE.download_speed_cap > 0: opts["ratelimit"] = f"{STATE.download_speed_cap}K"
        
        if mode == "audio": opts["format"] = "bestaudio/best"
        elif mode == "video": opts["format"] = "best[ext=mp4]/best"
        elif mode == "search": 
            opts["format"] = "bestaudio/best"
            opts["noplaylist"] = True
            opts["extract_flat"] = True
        return opts

    def _sync_extract(self, url, mode):
        with YoutubeDL(self._get_opts(mode)) as ydl:
            if mode == "search": return ydl.extract_info(f"ytsearch10:{url}", download=False) # Increased to 10 results
            return ydl.extract_info(url, download=False)

    async def extract(self, url, mode="audio"):
        if not STATE.is_active: raise HTTPException(503, "Service Suspended")
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(self.executor, self._sync_extract, url, mode)
            STATE.request_counter += 1
            return res
        except Exception as e:
            LOGGER.error(f"Error: {e}")
            raise e

ENGINE = ExtractionEngine()

# =========================================================================
# [APP] FASTAPI
# =========================================================================

app = FastAPI(title=Config.PROJECT_NAME, docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=Config.UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=Config.TEMPLATE_DIR)

# --- SECURITY ---
def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    is_correct_user = secrets.compare_digest(credentials.username, Config.ADMIN_USER)
    is_correct_pass = secrets.compare_digest(credentials.password, Config.ADMIN_PASS)
    if not (is_correct_user and is_correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- FRONTEND ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, username: str = Depends(check_auth)):
    return templates.TemplateResponse("interface.html", {"request": request, "user": username})

# --- ADMIN API ---
@app.get("/api/v1/telemetry")
async def get_telemetry(username: str = Depends(check_auth)):
    mem = psutil.virtual_memory()
    return {
        "status": "OPERATIONAL 🟢" if STATE.is_active else "HALTED 🔴",
        "uptime": STATE.uptime,
        "cpu": f"{psutil.cpu_percent()}%",
        "ram": f"{mem.percent}%",
        "req_handled": STATE.request_counter,
        "downloads": STATE.download_counter,
        "uploads": STATE.uploaded_files_count,
        "speed_limit": "UNLIMITED" if STATE.download_speed_cap == 0 else f"{STATE.download_speed_cap} KB/s",
        "api_keys": list(STATE.api_keys.keys())
    }

@app.post("/api/v1/admin/toggle")
async def toggle_service(username: str = Depends(check_auth)):
    STATE.is_active = not STATE.is_active
    return {"ok": True, "active": STATE.is_active}

@app.post("/api/v1/admin/throttle")
async def throttle(speed: int = Form(...), username: str = Depends(check_auth)):
    STATE.download_speed_cap = speed
    return {"ok": True}

@app.post("/api/v1/keys/create")
async def create_key(username: str = Depends(check_auth)):
    new_key = f"hk_{secrets.token_hex(8)}"
    STATE.api_keys[new_key] = "active"
    return {"key": new_key}

@app.post("/api/v1/keys/delete")
async def delete_key(key: str = Form(...), username: str = Depends(check_auth)):
    if key in STATE.api_keys: del STATE.api_keys[key]
    return {"ok": True}

# --- PUBLIC API / BOT API ---
@app.get("/search")
async def search(q: str):
    # Cache Check
    if q in RESULT_CACHE: return RESULT_CACHE[q]
    
    try:
        data = await ENGINE.extract(q, "search")
        results = []
        for e in data.get('entries', []):
            if e: results.append({
                "id": e.get('id'), "title": e.get('title'),
                "duration": e.get('duration'), "thumb": e.get('thumbnail'),
                "uploader": e.get('uploader')
            })
        resp = {"status": "ok", "results": results}
        RESULT_CACHE[q] = resp
        return resp
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/song/{video_id}")
async def get_audio(video_id: str):
    cache_key = f"aud:{video_id}"
    if cache_key in RESULT_CACHE: 
        STATE.download_counter += 1
        return RESULT_CACHE[cache_key]
    try:
        data = await ENGINE.extract(f"https://youtu.be/{video_id}", "audio")
        resp = {"status": "done", "title": data.get('title'), "link": data.get('url'), "duration": data.get('duration')}
        RESULT_CACHE[cache_key] = resp
        STATE.download_counter += 1
        return resp
    except: return JSONResponse(status_code=400, content={"error": "Failed"})

@app.get("/video/{video_id}")
async def get_video(video_id: str):
    try:
        data = await ENGINE.extract(f"https://youtu.be/{video_id}", "video")
        STATE.download_counter += 1
        return {"status": "done", "title": data.get('title'), "link": data.get('url')}
    except: return JSONResponse(status_code=400, content={"error": "Failed"})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), username: str = Depends(check_auth)):
    try:
        file_ext = file.filename.split('.')[-1]
        unique_name = f"{uuid.uuid4().hex[:8]}.{file_ext}"
        file_path = os.path.join(Config.UPLOAD_DIR, unique_name)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        STATE.uploaded_files_count += 1
        # Return full URL
        return {"status": "ok", "filename": file.filename, "url": f"/uploads/{unique_name}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    uvicorn.run("server:app", host=Config.HOST, port=Config.PORT, workers=Config.WORKERS, log_level="error")
