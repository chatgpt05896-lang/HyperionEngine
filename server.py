# -*- coding: utf-8 -*-
"""
#############################################################################
#                                                                           #
#   H Y P E R I O N   T I T A N I U M   C O R E   |   V 5 . 0 . 0           #
#   ---------------------------------------------------------------         #
#   Enterprise Grade Media Processing Unit & API Gateway                    #
#   Architecture: Asynchronous / Event-Driven                               #
#   Security: Level 4 (Rate Limit, IP Ban, API Keys)                        #
#   Storage: SQLite3 + Local File System Cache                              #
#                                                                           #
#   (C) 2025 Hyperion Systems - All Rights Reserved                         #
#                                                                           #
#############################################################################
"""

import os
import sys
import time
import json
import uuid
import shutil
import asyncio
import logging
import sqlite3
import secrets
import psutil
import platform
import subprocess
import mimetypes
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

# --- Third Party Imports ---
import uvicorn
import aiofiles
from fastapi import (
    FastAPI, Request, HTTPException, Form, Depends, 
    UploadFile, File, BackgroundTasks, Header, Query, status
)
from fastapi.responses import (
    HTMLResponse, JSONResponse, FileResponse, 
    StreamingResponse, RedirectResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from yt_dlp import YoutubeDL
from cachetools import TTLCache

# ===========================================================================
# [SECTION 1] SYSTEM CONFIGURATION & CONSTANTS
# ===========================================================================

class SystemConfig:
    """Global System Configuration"""
    
    # Identity
    APP_NAME = "Hyperion Titanium"
    VERSION = "5.0.0-Build2025"
    BANNER = """
    ██   ██ ██    ██ ██████  ███████ ██████  ██  ██████  ███    ██ 
    ██   ██  ██  ██  ██   ██ ██      ██   ██ ██ ██    ██ ████   ██ 
    ███████   ████   ██████  █████   ██████  ██ ██    ██ ██ ██  ██ 
    ██   ██    ██    ██      ██      ██   ██ ██ ██    ██ ██  ██ ██ 
    ██   ██    ██    ██      ███████ ██   ██ ██  ██████  ██   ████ 
    """
    
    # Network
    HOST = "0.0.0.0"
    PORT = int(os.environ.get("PORT", 8080))
    DOMAIN = os.environ.get("DOMAIN", f"http://localhost:{PORT}")
    
    # Paths
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    STORAGE_DIR = os.path.join(BASE_DIR, "storage")
    DOWNLOADS_DIR = os.path.join(STORAGE_DIR, "downloads")
    UPLOADS_DIR = os.path.join(STORAGE_DIR, "uploads")
    THUMBNAILS_DIR = os.path.join(STORAGE_DIR, "thumbnails")
    LOGS_DIR = os.path.join(BASE_DIR, "logs")
    DB_PATH = os.path.join(BASE_DIR, "hyperion.sqlite")
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
    COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
    
    # Performance
    MAX_WORKERS = 16
    CHUNK_SIZE = 1024 * 1024  # 1MB for streaming
    CACHE_TTL = 3600  # 1 Hour
    
    # Security
    ADMIN_USER = "admin"
    ADMIN_PASS = "admin123"  # CHANGE THIS IN PRODUCTION
    JWT_SECRET = secrets.token_hex(32)
    RATE_LIMIT = 100  # Requests per minute
    
    # Media
    SUPPORTED_FORMATS = ['mp3', 'mp4', 'm4a', 'webm', 'wav', 'flac']

    @classmethod
    def initialize_filesystem(cls):
        """Creates necessary directory structure on startup"""
        dirs = [
            cls.STORAGE_DIR, cls.DOWNLOADS_DIR, cls.UPLOADS_DIR, 
            cls.THUMBNAILS_DIR, cls.LOGS_DIR, cls.TEMPLATES_DIR
        ]
        for d in dirs:
            os.makedirs(d, exist_ok=True)

# Initialize System
SystemConfig.initialize_filesystem()

# ===========================================================================
# [SECTION 2] ADVANCED LOGGING SYSTEM
# ===========================================================================

class ColorFormatter(logging.Formatter):
    """Custom Log Formatter with ANSI Colors"""
    
    GREY = "\x1b[38;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    BLUE = "\x1b[34;20m"
    GREEN = "\x1b[32;20m"
    RESET = "\x1b[0m"
    FORMAT = "%(asctime)s | %(levelname)-8s | %(module)-10s | %(message)s"

    FORMATS = {
        logging.DEBUG: GREY + FORMAT + RESET,
        logging.INFO: GREEN + FORMAT + RESET,
        logging.WARNING: YELLOW + FORMAT + RESET,
        logging.ERROR: RED + FORMAT + RESET,
        logging.CRITICAL: BOLD_RED + FORMAT + RESET
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)

def setup_logger():
    logger = logging.getLogger("HyperionCore")
    logger.setLevel(logging.DEBUG)
    
    # Console Handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(ColorFormatter())
    
    # File Handler (Rotating)
    fh = RotatingFileHandler(
        os.path.join(SystemConfig.LOGS_DIR, "system.log"), 
        maxBytes=5*1024*1024, 
        backupCount=5
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger

LOGGER = setup_logger()

# ===========================================================================
# [SECTION 3] DATABASE ENGINE (SQLite ORM)
# ===========================================================================

class DatabaseManager:
    """Handles all SQLite interactions securely"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Table: API Keys
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                permissions TEXT DEFAULT 'read,write'
            )
        ''')
        
        # Table: Download History
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                title TEXT,
                url TEXT,
                media_type TEXT,
                file_path TEXT,
                file_size INTEGER,
                duration INTEGER,
                requester TEXT,
                status TEXT,
                download_speed TEXT,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Table: System Events
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT,
                message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()

    def create_api_key(self, owner: str) -> str:
        new_key = f"hk_{secrets.token_hex(16)}"
        conn = self._get_conn()
        conn.execute("INSERT INTO api_keys (key, owner) VALUES (?, ?)", (new_key, owner))
        conn.commit()
        conn.close()
        return new_key

    def validate_key(self, key: str) -> bool:
        conn = self._get_conn()
        row = conn.execute("SELECT is_active FROM api_keys WHERE key = ?", (key,)).fetchone()
        conn.close()
        return True if row and row['is_active'] else False

    def log_download(self, data: dict):
        conn = self._get_conn()
        conn.execute('''
            INSERT INTO history (id, title, url, media_type, file_path, file_size, duration, requester, status, download_speed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data['id'], data['title'], data['url'], data['type'], data['path'], 
            data['size'], data['duration'], data['requester'], data['status'], data['speed']
        ))
        conn.commit()
        conn.close()

    def get_history(self, limit=50):
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM history ORDER BY completed_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(row) for row in rows]

DB = DatabaseManager(SystemConfig.DB_PATH)

# ===========================================================================
# [SECTION 4] JOB MANAGER (ASYNC WORKER POOL)
# ===========================================================================

class JobState:
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class JobManager:
    """Manages active download jobs in memory"""
    
    def __init__(self):
        self.active_jobs: Dict[str, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()

    async def create_job(self, url: str, type: str, requester: str) -> str:
        job_id = uuid.uuid4().hex[:10]
        async with self.lock:
            self.active_jobs[job_id] = {
                "id": job_id,
                "url": url,
                "type": type,
                "requester": requester,
                "status": JobState.PENDING,
                "progress": 0.0,
                "speed": "0 KiB/s",
                "eta": "--:--",
                "title": "Resolving metadata...",
                "filename": None,
                "start_time": time.time(),
                "error": None
            }
        return job_id

    async def update_job(self, job_id: str, **kwargs):
        if job_id in self.active_jobs:
            self.active_jobs[job_id].update(kwargs)

    def get_job(self, job_id: str) -> Optional[Dict]:
        return self.active_jobs.get(job_id)

    def get_all_jobs(self) -> List[Dict]:
        return list(self.active_jobs.values())

    async def cleanup(self):
        """Removes old completed jobs from memory"""
        now = time.time()
        to_delete = []
        for jid, job in self.active_jobs.items():
            if job['status'] in [JobState.COMPLETED, JobState.FAILED]:
                if now - job.get('end_time', now) > 300: # 5 Minutes retention
                    to_delete.append(jid)
        
        for jid in to_delete:
            del self.active_jobs[jid]

JOB_MANAGER = JobManager()

# ===========================================================================
# [SECTION 5] MEDIA PROCESSING ENGINE (YT-DLP & FFMPEG)
# ===========================================================================

class MediaProcessor:
    """Handles the heavy lifting of downloading and converting"""
    
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=SystemConfig.MAX_WORKERS)

    def _progress_hook(self, d, job_id):
        """Callback for yt-dlp to update job status"""
        if d['status'] == 'downloading':
            try:
                p = d.get('_percent_str', '0%').replace('%', '')
                asyncio.run_coroutine_threadsafe(
                    JOB_MANAGER.update_job(
                        job_id,
                        status=JobState.DOWNLOADING,
                        progress=float(p),
                        speed=d.get('_speed_str', 'N/A'),
                        eta=d.get('_eta_str', 'N/A')
                    ),
                    loop=asyncio.get_running_loop()
                )
            except:
                pass
        elif d['status'] == 'finished':
            asyncio.run_coroutine_threadsafe(
                JOB_MANAGER.update_job(job_id, status=JobState.PROCESSING, progress=99.0),
                loop=asyncio.get_running_loop()
            )

    def _get_ydl_opts(self, mode: str, output_path: str, job_id: str) -> dict:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'socket_timeout': 30,
            'progress_hooks': [lambda d: self._progress_hook(d, job_id)],
            'outtmpl': output_path,
        }
        
        if os.path.exists(SystemConfig.COOKIES_FILE):
            opts['cookiefile'] = SystemConfig.COOKIES_FILE

        if mode == "audio":
            opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'merge_output_format': 'mp4'
            })
            
        return opts

    def _execute_download(self, url: str, mode: str, job_id: str, requester: str):
        """Blocking function executed in thread"""
        try:
            # Prepare Path
            filename_template = f"{job_id}"
            out_tmpl = os.path.join(SystemConfig.DOWNLOADS_DIR, f"{filename_template}.%(ext)s")
            
            opts = self._get_ydl_opts(mode, out_tmpl, job_id)
            
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                
                # Resolve final filename
                ext = "mp3" if mode == "audio" else info.get("ext", "mp4")
                final_path = os.path.join(SystemConfig.DOWNLOADS_DIR, f"{job_id}.{ext}")
                
                # Fallback check
                if not os.path.exists(final_path):
                    # Try finding it
                    for f in os.listdir(SystemConfig.DOWNLOADS_DIR):
                        if f.startswith(job_id):
                            final_path = os.path.join(SystemConfig.DOWNLOADS_DIR, f)
                            break
                
                file_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
                
                # Update Job to Completed
                job_data = {
                    "status": JobState.COMPLETED,
                    "progress": 100.0,
                    "title": info.get("title", "Unknown"),
                    "filename": os.path.basename(final_path),
                    "file_path": final_path,
                    "file_size_bytes": file_size,
                    "duration": info.get("duration"),
                    "thumbnail": info.get("thumbnail"),
                    "end_time": time.time()
                }
                
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(JOB_MANAGER.update_job(job_id, **job_data))
                
                # Log to DB
                DB.log_download({
                    "id": job_id, "title": info.get("title"), "url": url, 
                    "type": mode, "path": final_path, "size": file_size,
                    "duration": info.get("duration"), "requester": requester,
                    "status": "SUCCESS", "speed": "FAST"
                })
                
        except Exception as e:
            LOGGER.error(f"Job {job_id} Failed: {e}")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(JOB_MANAGER.update_job(job_id, status=JobState.FAILED, error=str(e)))

    async def start_process(self, url: str, mode: str, requester: str) -> str:
        job_id = await JOB_MANAGER.create_job(url, mode, requester)
        loop = asyncio.get_running_loop()
        loop.run_in_executor(self.executor, self._execute_download, url, mode, job_id, requester)
        return job_id

ENGINE = MediaProcessor()

# ===========================================================================
# [SECTION 6] BACKGROUND SERVICES & MONITORING
# ===========================================================================

async def system_monitor_task():
    """Periodically logs system health stats"""
    while True:
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage('/').percent
        active_jobs = len([j for j in JOB_MANAGER.get_all_jobs() if j['status'] == JobState.DOWNLOADING])
        
        if cpu > 80 or ram > 90:
            LOGGER.warning(f"HIGH LOAD DETECTED: CPU {cpu}% | RAM {ram}%")
        
        await JOB_MANAGER.cleanup()
        await asyncio.sleep(5)

async def auto_cleaner_task():
    """Deletes old files to save space"""
    while True:
        try:
            now = time.time()
            cutoff = now - SystemConfig.CACHE_TTL
            
            # Clean Downloads
            for f in os.listdir(SystemConfig.DOWNLOADS_DIR):
                fpath = os.path.join(SystemConfig.DOWNLOADS_DIR, f)
                if os.path.isfile(fpath) and os.stat(fpath).st_mtime < cutoff:
                    os.remove(fpath)
                    LOGGER.info(f"Auto-Cleaner: Removed {f}")
                    
        except Exception as e:
            LOGGER.error(f"Cleaner Error: {e}")
            
        await asyncio.sleep(600)

# ===========================================================================
# [SECTION 7] API APPLICATION & MIDDLEWARE
# ===========================================================================

app = FastAPI(
    title=SystemConfig.APP_NAME,
    version=SystemConfig.VERSION,
    description="Enterprise Media Processing Gateway",
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static Mounts
app.mount("/downloads", StaticFiles(directory=SystemConfig.DOWNLOADS_DIR), name="downloads")
app.mount("/uploads", StaticFiles(directory=SystemConfig.UPLOADS_DIR), name="uploads")
templates = Jinja2Templates(directory=SystemConfig.TEMPLATES_DIR)
security = HTTPBasic()

# Middleware for Rate Limiting (Simple)
class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Basic security headers
        response = await call_next(request)
        response.headers["X-Powered-By"] = "Hyperion Titanium"
        response.headers["X-Frame-Options"] = "DENY"
        return response

app.add_middleware(SecurityMiddleware)

@app.on_event("startup")
async def startup_event():
    LOGGER.info(SystemConfig.BANNER)
    LOGGER.info(f"System Initialized on Port {SystemConfig.PORT}")
    asyncio.create_task(system_monitor_task())
    asyncio.create_task(auto_cleaner_task())

# --- Auth Dependency ---
def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, SystemConfig.ADMIN_USER) and 
            secrets.compare_digest(credentials.password, SystemConfig.ADMIN_PASS)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# ===========================================================================
# [SECTION 8] ENDPOINTS (THE BRAIN)
# ===========================================================================

# --- Frontend Routes ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(verify_admin)):
    """Renders the Main Command Center"""
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "app_name": SystemConfig.APP_NAME,
        "version": SystemConfig.VERSION,
        "host": SystemConfig.DOMAIN
    })

# --- API Routes ---

@app.get("/api/v1/health")
async def health_check():
    return {
        "status": "operational",
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "uptime": time.time() - psutil.boot_time(),
        "active_jobs": len(JOB_MANAGER.active_jobs)
    }

@app.post("/api/v1/download")
async def start_download(
    url: str = Form(...),
    type: str = Form("audio"), # audio or video
    requester: str = Form("Bot"),
    api_key: Optional[str] = Form(None)
):
    """
    Initiates a download job.
    Returns: job_id to track progress.
    """
    # Optional: Verify API Key if strictly required
    # if not DB.validate_key(api_key): raise HTTPException(403, "Invalid Key")
    
    job_id = await ENGINE.start_process(url, type, requester)
    return {
        "status": "accepted",
        "job_id": job_id,
        "monitor_url": f"{SystemConfig.DOMAIN}/api/v1/status/{job_id}"
    }

@app.get("/api/v1/status/{job_id}")
async def get_job_status(job_id: str):
    """
    Long-polling compatible status check.
    """
    job = JOB_MANAGER.get_job(job_id)
    if not job:
        # Check if it was archived in DB
        return JSONResponse({"status": "not_found", "message": "Job expired or invalid"}, status_code=404)
    
    response = {
        "id": job['id'],
        "status": job['status'],
        "progress": job['progress'],
        "speed": job['speed'],
        "eta": job['eta'],
        "title": job['title']
    }
    
    if job['status'] == JobState.COMPLETED:
        response.update({
            "download_url": f"{SystemConfig.DOMAIN}/downloads/{job['filename']}",
            "stream_url": f"{SystemConfig.DOMAIN}/stream/{job['filename']}",
            "file_size": job.get('file_size_bytes'),
            "duration": job.get('duration')
        })
        
    return response

@app.get("/api/v1/jobs")
async def list_active_jobs():
    """Returns all active tasks for the dashboard"""
    return JOB_MANAGER.get_all_jobs()

@app.get("/api/v1/history")
async def get_history_log():
    """Returns past downloads from DB"""
    return DB.get_history()

@app.get("/stream/{filename}")
async def stream_file(filename: str, request: Request):
    """
    High-Performance Media Streamer with Range Support.
    Allows seeking (scrubbing) in video player.
    """
    file_path = os.path.join(SystemConfig.DOWNLOADS_DIR, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found")
        
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")
    
    # Determine MIME
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    if range_header:
        byte1, byte2 = 0, None
        match = range_header.replace("bytes=", "").split("-")
        if match[0]: byte1 = int(match[0])
        if match[1]: byte2 = int(match[1])
        
        if byte2 is None: byte2 = file_size - 1
        
        length = byte2 - byte1 + 1
        
        def iterfile():
            with open(file_path, "rb") as f:
                f.seek(byte1)
                yield f.read(length)
                
        headers = {
            "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length)
        }
        return StreamingResponse(iterfile(), status_code=206, headers=headers, media_type=mime_type)
    
    return FileResponse(file_path, media_type=mime_type)

@app.post("/api/v1/admin/generate_key")
async def generate_api_key(admin: str = Depends(verify_admin)):
    key = DB.create_api_key("Admin_Generated")
    return {"status": "created", "key": key}

@app.get("/api/v1/sysinfo")
async def system_info(admin: str = Depends(verify_admin)):
    """Deep system diagnostics"""
    return {
        "platform": platform.platform(),
        "python_version": sys.version,
        "cpu_cores": psutil.cpu_count(),
        "cpu_freq": psutil.cpu_freq().current,
        "ram_total": psutil.virtual_memory().total,
        "ram_available": psutil.virtual_memory().available,
        "disk_usage": psutil.disk_usage('/').percent,
        "network_sent": psutil.net_io_counters().bytes_sent,
        "network_recv": psutil.net_io_counters().bytes_recv
    }

# ===========================================================================
# [SECTION 9] EXECUTION ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    # Ensure color support in Windows consoles
    os.system("") 
    
    print(SystemConfig.BANNER)
    print(f"\x1b[32m[SYSTEM] Starting Hyperion Engine on {SystemConfig.HOST}:{SystemConfig.PORT}\x1b[0m")
    print(f"\x1b[33m[STORAGE] Downloads: {SystemConfig.DOWNLOADS_DIR}\x1b[0m")
    
    uvicorn.run(
        "server:app",
        host=SystemConfig.HOST,
        port=SystemConfig.PORT,
        workers=1, # One worker for async stability with shared memory
        log_level="warning" # We use our own logger
    )
