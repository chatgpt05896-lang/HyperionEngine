#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
################################################################################
#                                                                              #
#    HYPERION TITANIUM SERVER - v11.0 (SECURE CORE)                            #
#    ARCHITECTURE: MONOLITHIC ASYNC SERVER                                     #
#    AUTH: ENABLED (BASIC HTTP)                                                #
#    DATABASE: SQLITE3 EMBEDDED                                                #
#                                                                              #
#    [WARNING]: THIS IS A PRODUCTION-GRADE SCRIPT.                             #
#    IT HANDLES THREADING, I/O BOUND TASKS, AND MEMORY MANAGEMENT.             #
#                                                                              #
################################################################################
"""

import os
import sys
import time
import json
import uuid
import shutil
import psutil
import socket
import logging
import sqlite3
import secrets
import asyncio
import hashlib
import threading
import requests
import uvicorn
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from abc import ABC, abstractmethod

# --- DEPENDENCY INJECTION CHECK ---
try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, Depends, status
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from starlette.exceptions import HTTPException as StarletteHTTPException
    import yt_dlp
except ImportError as e:
    print(f"CRITICAL ERROR: Missing Dependency -> {e}")
    print("INSTALL: pip install fastapi uvicorn yt-dlp requests psutil aiofiles")
    sys.exit(1)

# ==============================================================================
# [MODULE 1] SYSTEM CONFIGURATION & CONSTANTS
# ==============================================================================

class SystemConfig:
    """Central Configuration Hub"""
    APP_NAME = "Hyperion Titanium"
    VERSION = "11.0.0-Secure"
    
    # Paths
    BASE_DIR = Path(__file__).resolve().parent
    DOWNLOAD_DIR = BASE_DIR / "downloads"
    CACHE_DIR = BASE_DIR / "cache"
    LOGS_DIR = BASE_DIR / "logs"
    TEMPLATES_DIR = BASE_DIR / "templates"
    STATIC_DIR = BASE_DIR / "static"
    DB_FILE = BASE_DIR / "hyperion_core.db"
    COOKIES_FILE = BASE_DIR / "cookies.txt"
    
    # External Config
    COOKIES_URL = "https://batbin.me/raw/reingress"
    TEMPLATE_INDEX = "index.html"
    
    # Authentication (As Requested)
    AUTH_USER = "admin"
    AUTH_PASS = "asdfghjkl05896"
    
    # Performance Limits
    MAX_WORKERS = 8           # Thread Pool Size
    MAX_QUEUE = 100           # Max concurrent tasks
    MAX_DISK_USAGE = 85.0     # Percentage
    CLEANUP_AGE_SEC = 3600    # Delete files older than 1 hour

    @classmethod
    def initialize_filesystem(cls):
        """Creates necessary directory structures"""
        dirs = [cls.DOWNLOAD_DIR, cls.CACHE_DIR, cls.LOGS_DIR, cls.TEMPLATES_DIR, cls.STATIC_DIR]
        for d in dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"FS ERROR: Could not create {d} -> {e}")
                sys.exit(1)

SystemConfig.initialize_filesystem()

# ==============================================================================
# [MODULE 2] LOGGING & DIAGNOSTICS
# ==============================================================================

class Logger:
    """Advanced Logging Wrapper"""
    def __init__(self):
        self.logger = logging.getLogger("HyperionCore")
        self.logger.setLevel(logging.DEBUG)
        
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # File Handler
        fh = logging.FileHandler(SystemConfig.LOGS_DIR / "server.log", encoding='utf-8')
        fh.setFormatter(formatter)
        
        # Stream Handler
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)
        
        self.logger.addHandler(fh)
        self.logger.addHandler(sh)

    def info(self, msg): self.logger.info(msg)
    def warning(self, msg): self.logger.warning(msg)
    def error(self, msg): self.logger.error(msg)
    def debug(self, msg): self.logger.debug(msg)
    def critical(self, msg): self.logger.critical(msg)

log = Logger()

# ==============================================================================
# [MODULE 3] PERSISTENT STORAGE (DATABASE)
# ==============================================================================

class DatabaseManager:
    """Thread-safe SQLite Wrapper"""
    
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self._bootstrap()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _bootstrap(self):
        """Create Tables if not exist"""
        schema = """
        CREATE TABLE IF NOT EXISTS downloads (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            status TEXT DEFAULT 'pending',
            progress REAL DEFAULT 0.0,
            filepath TEXT,
            speed TEXT,
            error_log TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            details TEXT,
            ip_address TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        try:
            with self._get_conn() as conn:
                conn.executescript(schema)
            log.info("Database Schema Validated.")
        except Exception as e:
            log.critical(f"Database Bootstrap Failed: {e}")

    def create_task(self, task_id: str, client_id: str, url: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO downloads (id, client_id, url) VALUES (?, ?, ?)",
                (task_id, client_id, url)
            )

    def update_task(self, task_id: str, **kwargs):
        """Dynamic Update Query Builder"""
        if not kwargs: return
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [task_id]
        with self._get_conn() as conn:
            conn.execute(f"UPDATE downloads SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id = ?", values)

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM downloads WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def log_event(self, event_type: str, details: str, ip: str = "SYSTEM"):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (event_type, details, ip_address) VALUES (?, ?, ?)",
                (event_type, details, ip)
            )

db = DatabaseManager(SystemConfig.DB_FILE)

# ==============================================================================
# [MODULE 4] SECURITY & AUTHENTICATION
# ==============================================================================

class SecurityModule:
    """Handles Authentication and Protection"""
    security = HTTPBasic()

    @staticmethod
    def verify_auth(credentials: HTTPBasicCredentials = Depends(security)):
        """Dependency for route protection"""
        correct_user = secrets.compare_digest(credentials.username, SystemConfig.AUTH_USER)
        correct_pass = secrets.compare_digest(credentials.password, SystemConfig.AUTH_PASS)
        
        if not (correct_user and correct_pass):
            db.log_event("AUTH_FAIL", f"Failed login attempt for user: {credentials.username}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

# ==============================================================================
# [MODULE 5] SYSTEM HEALTH & MAINTENANCE
# ==============================================================================

class StorageGuardian:
    """Monitors Disk Usage and Cleans up"""
    
    @staticmethod
    async def run_cleanup_loop():
        """Background Daemon"""
        while True:
            try:
                StorageGuardian._check_disk_space()
                StorageGuardian._purge_old_files()
            except Exception as e:
                log.error(f"Cleanup Error: {e}")
            await asyncio.sleep(600) # Check every 10 mins

    @staticmethod
    def _check_disk_space():
        usage = psutil.disk_usage(SystemConfig.BASE_DIR)
        if usage.percent > SystemConfig.MAX_DISK_USAGE:
            log.warning(f"Disk Critical ({usage.percent}%)! Triggering emergency purge.")
            StorageGuardian._purge_old_files(force=True)

    @staticmethod
    def _purge_old_files(force=False):
        now = time.time()
        age_limit = 600 if force else SystemConfig.CLEANUP_AGE_SEC
        
        for f in SystemConfig.DOWNLOAD_DIR.glob("*"):
            if f.stat().st_mtime < now - age_limit:
                try:
                    if f.is_file(): f.unlink()
                    elif f.is_dir(): shutil.rmtree(f)
                    log.info(f"Purged file: {f.name}")
                except Exception as e:
                    log.error(f"Failed to delete {f.name}: {e}")

class CookieManager:
    """Ensures YouTube Cookies are Valid"""
    
    @staticmethod
    def refresh_cookies():
        log.info(f"Fetching cookies from {SystemConfig.COOKIES_URL}...")
        try:
            r = requests.get(SystemConfig.COOKIES_URL, timeout=15)
            if r.status_code == 200:
                with open(SystemConfig.COOKIES_FILE, "w", encoding="utf-8") as f:
                    f.write(r.text)
                log.info("Cookies updated successfully.")
                return True
        except Exception as e:
            log.error(f"Cookie fetch failed: {e}")
        return False

# ==============================================================================
# [MODULE 6] WEBSOCKET REAL-TIME BUS
# ==============================================================================

class SocketManager:
    """Manages WebSocket Connections"""
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        log.debug(f"Client Connected: {client_id}")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_json(self, client_id: str, data: dict):
        if client_id in self.active_connections:
            try:
                await self.active_connections[client_id].send_json(data)
            except RuntimeError:
                self.disconnect(client_id)

ws_manager = SocketManager()

# ==============================================================================
# [MODULE 7] DOWNLOAD ENGINE (CORE)
# ==============================================================================

class EngineWorker:
    """The Heavy Lifter - Wraps yt-dlp"""
    
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=SystemConfig.MAX_WORKERS)

    def _progress_hook(self, d, task_id, client_id):
        if d['status'] == 'downloading':
            try:
                # Safe conversion of values
                p = d.get('_percent_str', '0%').replace('%', '')
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                
                payload = {
                    "type": "progress",
                    "task_id": task_id,
                    "percent": float(p),
                    "speed": speed,
                    "eta": eta,
                    "filename": d.get('filename', 'Unknown')
                }
                
                # Send to Socket
                asyncio.run_coroutine_threadsafe(
                    ws_manager.send_json(client_id, payload),
                    loop
                )
                
                # Low-freq DB Update (Optimization)
                if int(float(p)) % 10 == 0:
                    db.update_task(task_id, progress=float(p), speed=speed, status='downloading')

            except Exception as e:
                # Suppress errors inside hook to not crash download
                pass

    def run_task(self, task_id: str, url: str, client_id: str):
        """This runs inside a thread pool"""
        log.info(f"Starting Task {task_id} for Client {client_id}")
        db.update_task(task_id, status="processing")
        
        cookie_path = str(SystemConfig.COOKIES_FILE)
        if not os.path.exists(cookie_path):
             # Try emergency fetch
             CookieManager.refresh_cookies()

        opts = {
            'cookiefile': cookie_path,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': str(SystemConfig.DOWNLOAD_DIR / f'%(title)s_{task_id}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [lambda d: self._progress_hook(d, task_id, client_id)],
            # Resilience Options
            'retries': 10,
            'fragment_retries': 10,
            'skip_unavailable_fragments': True,
            'keepvideo': False,
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                
                # Success
                final_name = os.path.basename(filename)
                db.update_task(task_id, status="completed", progress=100.0, filepath=filename)
                db.log_event("DOWNLOAD_SUCCESS", f"Downloaded {final_name}")
                
                asyncio.run_coroutine_threadsafe(
                    ws_manager.send_json(client_id, {
                        "type": "completed",
                        "path": f"/stream/{final_name}",
                        "filename": final_name
                    }),
                    loop
                )

        except Exception as e:
            # Failure
            error_msg = str(e)
            log.error(f"Task {task_id} Failed: {error_msg}")
            db.update_task(task_id, status="failed", error_log=error_msg)
            db.log_event("DOWNLOAD_ERROR", f"Failed URL {url}: {error_msg}")
            
            asyncio.run_coroutine_threadsafe(
                ws_manager.send_json(client_id, {
                    "type": "error",
                    "msg": "فشل التحميل. تأكد من الرابط أو حاول لاحقاً."
                }),
                loop
            )

engine = EngineWorker()

# ==============================================================================
# [MODULE 8] FASTAPI APP FACTORY
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and Shutdown Logic"""
    global loop
    loop = asyncio.get_running_loop()
    
    log.info("--- HYPERION SYSTEM BOOT ---")
    log.info(f"Authenticated Mode: ACTIVE (User: {SystemConfig.AUTH_USER})")
    
    # 1. Fetch Cookies
    CookieManager.refresh_cookies()
    
    # 2. Start Cleanup Daemon
    asyncio.create_task(StorageGuardian.run_cleanup_loop())
    
    yield
    
    log.info("--- HYPERION SHUTDOWN ---")

app = FastAPI(
    title=SystemConfig.APP_NAME,
    version=SystemConfig.VERSION,
    lifespan=lifespan,
    docs_url=None, # Security: Disable Swagger
    redoc_url=None
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static Files
app.mount("/static", StaticFiles(directory=SystemConfig.STATIC_DIR), name="static")
templates = Jinja2Templates(directory=SystemConfig.TEMPLATES_DIR)

# Global Exception Handler (Catch All)
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_id = uuid.uuid4()
    log.error(f"Global Error {error_id}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "error_id": str(error_id)}
    )

# ==============================================================================
# [MODULE 9] ROUTING & ENDPOINTS
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, username: str = Depends(SecurityModule.verify_auth)):
    """Protected Dashboard Route"""
    template_path = SystemConfig.TEMPLATES_DIR / SystemConfig.TEMPLATE_INDEX
    
    if not template_path.exists():
        return HTMLResponse(
            f"""
            <html>
            <body style="background:#000; color:red; font-family:monospace; text-align:center; padding-top:100px;">
                <h1>CRITICAL MISSING FILE</h1>
                <p>Could not find: {template_path}</p>
                <p>Please upload the design file.</p>
            </body>
            </html>
            """, 
            status_code=404
        )
    
    return templates.TemplateResponse(
        SystemConfig.TEMPLATE_INDEX, 
        {"request": request, "user": username}
    )

@app.get("/api/search")
async def search_youtube(q: str, username: str = Depends(SecurityModule.verify_auth)):
    """Search Proxy with Threading"""
    if not q: raise HTTPException(400, "Query empty")
    
    opts = {
        'cookiefile': str(SystemConfig.COOKIES_FILE),
        'quiet': True,
        'extract_flat': True,
        'skip_download': True
    }
    
    def _search():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch10:{q}", download=False).get('entries', [])
    
    try:
        results = await loop.run_in_executor(engine.executor, _search)
        return JSONResponse(results)
    except Exception as e:
        log.error(f"Search Failed: {e}")
        raise HTTPException(500, "Search engine failure")

@app.post("/api/queue")
async def queue_download(
    request: Request, 
    background_tasks: BackgroundTasks, 
    username: str = Depends(SecurityModule.verify_auth)
):
    """Secure Download Endpoint"""
    try:
        data = await request.json()
    except:
        raise HTTPException(400, "Invalid JSON")
        
    url = data.get("url")
    client_id = data.get("client_id")
    
    if not url or not client_id:
        raise HTTPException(400, "Missing url or client_id")
        
    task_id = str(uuid.uuid4())
    
    # 1. Log to DB
    db.create_task(task_id, client_id, url)
    db.log_event("TASK_QUEUED", f"User {username} queued {url}")
    
    # 2. Dispatch to Background (Separate Thread)
    background_tasks.add_task(engine.run_task, task_id, url, client_id)
    
    return {"status": "ok", "task_id": task_id, "message": "Download Queued"}

@app.get("/stream/{filename}")
async def stream_file(filename: str, username: str = Depends(SecurityModule.verify_auth)):
    """Secure File Serving"""
    file_path = SystemConfig.DOWNLOAD_DIR / filename
    
    # Security: Prevent Directory Traversal
    if ".." in filename or not file_path.resolve().is_relative_to(SystemConfig.DOWNLOAD_DIR):
        db.log_event("SECURITY_ALERT", f"Path traversal attempt by {username}: {filename}")
        raise HTTPException(403, "Access Denied")
        
    if not file_path.exists():
        raise HTTPException(404, "File not found (Expired or Deleted)")
        
    return FileResponse(
        file_path, 
        filename=filename,
        media_type="application/octet-stream"
    )

@app.get("/api/stats")
async def system_stats(username: str = Depends(SecurityModule.verify_auth)):
    """Admin Telemetry"""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(SystemConfig.BASE_DIR)
    
    return {
        "cpu_usage": psutil.cpu_percent(),
        "ram_usage": mem.percent,
        "disk_usage": disk.percent,
        "workers_active": threading.active_count(),
        "security_level": "High (Authenticated)"
    }

# WebSocket is Open (No Auth Header support in JS standard WS API usually, handled by token in msg if needed)
# For simplicity, we keep it open but validate messages if needed.
@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await ws_manager.connect(websocket, client_id)
    try:
        while True:
            # Keep-alive loop
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(client_id)
    except Exception as e:
        log.error(f"WebSocket Error: {e}")
        ws_manager.disconnect(client_id)

# ==============================================================================
# [MODULE 10] SERVER ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    # Ensure Single Instance Logic could go here (PID check)
    
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting Hyperion Titanium on Port {port}...")
    log.info("Make sure 'requirements.txt' contains: fastapi, uvicorn, yt-dlp, psutil, requests")
    
    try:
        uvicorn.run(
            "server:app",
            host="0.0.0.0",
            port=port,
            log_level="warning", # Suppress default logs, use ours
            loop="uvloop" if sys.platform != "win32" else "asyncio",
            reload=False,
            workers=1 # Using 1 worker for singleton DB access, Threads handle concurrency
        )
    except KeyboardInterrupt:
        log.info("Stopping Server...")
    except Exception as e:
        log.critical(f"Server Crash: {e}")
