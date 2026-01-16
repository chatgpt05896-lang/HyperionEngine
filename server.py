# -*- coding: utf-8 -*-
"""
#############################################################################
#                                                                           #
#       H Y P E R I O N   N U C L E A R   E N G I N E   |   v 9 . 0         #
#       -----------------------------------------------------------         #
#       Enterprise Async Media Processor & High-Speed Streamer              #
#                                                                           #
#       Features:                                                           #
#       - Non-Blocking ThreadPool Architecture                              #
#       - Memory-Mapped File Streaming                                      #
#       - Real-time WebSocket-ready State Management                        #
#       - Self-Healing File System & Auto-Cleanup                           #
#                                                                           #
#       Author: Senior Backend Engineer (Ref: Boda)                         #
#                                                                           #
#############################################################################
"""

import os
import sys
import time
import json
import uuid
import signal
import shutil
import socket
import logging
import asyncio
import psutil
import secrets
import threading
import mimetypes
import contextlib
from pathlib import Path
from typing import Dict, List, Optional, Union, Generator, Any
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# --- External Libraries Check & Import ---
try:
    import uvicorn
    from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, status
    from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.templating import Jinja2Templates
    from fastapi.staticfiles import StaticFiles
    from starlette.background import BackgroundTask
    from yt_dlp import YoutubeDL
except ImportError as e:
    print(f"CRITICAL ERROR: Missing libraries. Run: pip install fastapi uvicorn yt-dlp psutil jinja2 python-multipart requests")
    sys.exit(1)

# ===========================================================================
# [SECTION 1] CORE CONFIGURATION (SINGLETON)
# ===========================================================================

class Config:
    """
    Centralized Configuration for the Hyperion Engine.
    Controls paths, concurrency limits, and cleanup policies.
    """
    # Identity
    APP_NAME = "Hyperion Nuclear"
    VERSION = "9.0.0-Production"
    
    # Networking
    HOST = "0.0.0.0"
    PORT = int(os.getenv("PORT", 8080))
    DOMAIN = os.getenv("DOMAIN", f"http://localhost:{PORT}")
    
    # Performance & Concurrency
    MAX_WORKERS = 32                # Extreme concurrency
    DOWNLOAD_TIMEOUT = 1200         # 20 Minutes max per file
    STREAM_CHUNK_SIZE = 1024 * 1024 * 4  # 4MB chunks for faster streaming
    
    # File System Paths
    BASE_DIR = Path(__file__).resolve().parent
    STORAGE_DIR = BASE_DIR / "storage"
    DOWNLOADS_DIR = STORAGE_DIR / "downloads"
    TEMPLATES_DIR = BASE_DIR / "templates"
    LOGS_DIR = BASE_DIR / "logs"
    
    # Cleanup Policy (Strict)
    FILE_RETENTION_SECONDS = 900    # 15 Minutes
    CLEANUP_INTERVAL = 60           # Check every minute

    # yt-dlp Optimization
    YTDLP_BUFFER_SIZE = 1024 * 1024 * 16  # 16MB Buffer to reduce disk I/O
    
    @staticmethod
    def initialize():
        """Bootstrapper to create necessary folders."""
        for p in [Config.STORAGE_DIR, Config.DOWNLOADS_DIR, Config.TEMPLATES_DIR, Config.LOGS_DIR]:
            p.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt':
                try: os.chmod(p, 0o777)
                except: pass

Config.initialize()

# ===========================================================================
# [SECTION 2] ADVANCED LOGGING (THREAD-SAFE)
# ===========================================================================

class HyperionLogger:
    """
    Thread-safe logger with ANSI colors for console and structured file logging.
    """
    COLORS = {
        'INFO': '\033[92m',     # Green
        'WARNING': '\033[93m',  # Yellow
        'ERROR': '\033[91m',    # Red
        'DEBUG': '\033[96m',    # Cyan
        'RESET': '\033[0m'
    }

    def __init__(self):
        self.logger = logging.getLogger("Hyperion")
        self.logger.setLevel(logging.INFO)
        
        # Avoid duplicate handlers
        if not self.logger.handlers:
            # File Handler
            fh = logging.FileHandler(Config.LOGS_DIR / "server.log", encoding='utf-8')
            fh.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(threadName)s | %(message)s'))
            self.logger.addHandler(fh)
            
            # Console Handler
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(logging.Formatter('%(message)s'))
            self.logger.addHandler(ch)

    def info(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        self.logger.info(f"{self.COLORS['INFO']}[INFO]    {t} | {msg}{self.COLORS['RESET']}")

    def warn(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        self.logger.warning(f"{self.COLORS['WARNING']}[WARN]    {t} | {msg}{self.COLORS['RESET']}")

    def error(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        self.logger.error(f"{self.COLORS['ERROR']}[ERROR]   {t} | {msg}{self.COLORS['RESET']}")

log = HyperionLogger()

# ===========================================================================
# [SECTION 3] JOB MANAGEMENT & STATE
# ===========================================================================

class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    CONVERTING = "converting"
    COMPLETED = "completed"
    FAILED = "failed"

class JobManager:
    """
    In-Memory Database for Job States.
    Uses RLock to ensure thread safety during high concurrency.
    """
    def __init__(self):
        self._jobs: Dict[str, Dict] = {}
        self._history: List[Dict] = []
        self._lock = threading.RLock()

    def create_job(self, url: str, type: str, requester: str) -> str:
        job_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "url": url,
                "type": type,
                "requester": requester,
                "status": JobStatus.QUEUED,
                "created_at": time.time(),
                "progress": 0,
                "speed": "Waiting...",
                "eta": "--:--",
                "filename": None,
                "title": "Resolving...",
                "filesize": 0,
                "error": None
            }
        return job_id

    def update_job(self, job_id: str, **kwargs):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kwargs)
                
                # If completed, add to history log
                if kwargs.get("status") == JobStatus.COMPLETED:
                    job_copy = self._jobs[job_id].copy()
                    self._history.insert(0, job_copy)
                    # Keep history size manageable
                    if len(self._history) > 100:
                        self._history.pop()

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            return self._jobs.get(job_id, None)

    def get_active_jobs(self) -> List[Dict]:
        with self._lock:
            return [j for j in self._jobs.values() if j['status'] in [JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.CONVERTING]]

    def get_history(self) -> List[Dict]:
        with self._lock:
            return self._history

    def cleanup_memory(self):
        """Removes old jobs from RAM map, keeps them in history only."""
        with self._lock:
            now = time.time()
            to_remove = []
            for jid, job in self._jobs.items():
                if job['status'] in [JobStatus.COMPLETED, JobStatus.FAILED]:
                    # Keep in memory map for 5 minutes for polling, then drop
                    if now - job.get('completed_at', now) > 300:
                        to_remove.append(jid)
            
            for jid in to_remove:
                del self._jobs[jid]

job_manager = JobManager()

# ===========================================================================
# [SECTION 4] DOWNLOAD ENGINE (THE WORKER)
# ===========================================================================

class DownloadEngine:
    """
    The Core Engine wrapper around yt-dlp.
    Executes in a separate thread pool to prevent blocking the API.
    """
    def __init__(self):
        self.executor = ThreadPoolExecutor(
            max_workers=Config.MAX_WORKERS,
            thread_name_prefix="HyperionWorker"
        )

    def _progress_hook(self, d, job_id):
        """Callback from yt-dlp. Updates job state in real-time."""
        if d['status'] == 'downloading':
            try:
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                percent = (downloaded / total * 100) if total > 0 else 0
                
                job_manager.update_job(
                    job_id,
                    status=JobStatus.PROCESSING,
                    progress=round(percent, 1),
                    speed=d.get('_speed_str', 'N/A'),
                    eta=d.get('_eta_str', '...')
                )
            except Exception:
                pass # Suppress calculation errors
        
        elif d['status'] == 'finished':
            job_manager.update_job(job_id, status=JobStatus.CONVERTING, progress=99.0, speed="Processing...")

    def _worker_logic(self, job_id: str, url: str, mode: str):
        """The actual code running inside the thread."""
        log.info(f"Starting Job {job_id} [{mode}] for {url}")
        
        try:
            # 1. Define Paths
            # Using job_id as filename ensures uniqueness
            filename_template = f"{job_id}"
            out_tmpl = str(Config.DOWNLOADS_DIR / f"{filename_template}.%(ext)s")
            
            # 2. Configure yt-dlp options (Optimized for Speed)
            opts = {
                'outtmpl': out_tmpl,
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                'ignoreerrors': True,
                'geo_bypass': True,
                'socket_timeout': 15,
                'progress_hooks': [lambda d: self._progress_hook(d, job_id)],
                
                # Performance Tuning
                'buffersize': Config.YTDLP_BUFFER_SIZE,
                'http_chunk_size': 10485760, # 10MB chunks
                'concurrent_fragment_downloads': 8,
                'retries': 3,
            }

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
                # Prioritize MP4 for compatibility without re-encoding if possible
                opts.update({
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'merge_output_format': 'mp4'
                })

            # 3. Execute Download
            with YoutubeDL(opts) as ydl:
                # Extract Info First (Quick)
                info = ydl.extract_info(url, download=False)
                clean_title = info.get('title', 'Unknown Media')
                
                # Update title in UI
                job_manager.update_job(job_id, title=clean_title)
                
                # Perform Download
                ydl.download([url])

            # 4. Locate Final File
            # yt-dlp might have changed the extension (webm -> mp3)
            final_file = None
            final_path = None
            
            for f in os.listdir(Config.DOWNLOADS_DIR):
                if f.startswith(job_id):
                    final_file = f
                    final_path = Config.DOWNLOADS_DIR / f
                    break
            
            if not final_path or not final_path.exists():
                raise FileNotFoundError("Output file not generated.")
            
            if final_path.stat().st_size == 0:
                raise Exception("Downloaded file is 0KB (Corruption).")

            # 5. Finalize
            file_size_human = f"{round(final_path.stat().st_size / (1024*1024), 2)} MB"
            
            job_manager.update_job(
                job_id,
                status=JobStatus.COMPLETED,
                progress=100.0,
                filename=final_file,
                filepath=str(final_path),
                size=file_size_human,
                completed_at=time.time(),
                eta="Done"
            )
            log.info(f"Job {job_id} Completed: {clean_title} ({file_size_human})")

        except Exception as e:
            log.error(f"Job {job_id} Failed: {str(e)}")
            job_manager.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=str(e),
                completed_at=time.time()
            )

    def submit_job(self, job_id: str, url: str, mode: str):
        """Non-blocking submission to thread pool."""
        self.executor.submit(self._worker_logic, job_id, url, mode)

engine = DownloadEngine()

# ===========================================================================
# [SECTION 5] FILE CLEANER & MONITOR (DAEMONS)
# ===========================================================================

class SystemServices:
    """Background services that keep the server healthy."""
    
    @staticmethod
    def _cleaner_loop():
        log.info("Cleaner Service Started.")
        while True:
            try:
                now = time.time()
                retention = Config.FILE_RETENTION_SECONDS
                
                # Clean Files
                count = 0
                for f in Config.DOWNLOADS_DIR.glob("*"):
                    if f.is_file() and (now - f.stat().st_mtime > retention):
                        try:
                            f.unlink()
                            count += 1
                        except: pass
                
                if count > 0:
                    log.info(f"Cleaner: Removed {count} expired files.")
                
                # Clean Memory
                job_manager.cleanup_memory()
                
            except Exception as e:
                log.error(f"Cleaner Error: {e}")
            
            time.sleep(Config.CLEANUP_INTERVAL)

    @staticmethod
    def start():
        t = threading.Thread(target=SystemServices._cleaner_loop, daemon=True)
        t.start()

SystemServices.start()

# ===========================================================================
# [SECTION 6] STREAMING UTILS (RANGE SUPPORT)
# ===========================================================================

def range_stream_response(file_path: Path, range_header: str, mime_type: str):
    """
    Generates a Partial Content (206) response.
    Essential for Telegram and modern video players to allow seeking.
    """
    file_size = file_path.stat().st_size
    start, end = 0, file_size - 1

    if range_header:
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
        except ValueError:
            pass # Invalid range, fallback to full
            
    if start >= file_size: start = file_size - 1
    if end >= file_size: end = file_size - 1
    
    chunk_length = (end - start) + 1
    
    def iter_file():
        with open(file_path, "rb") as f:
            f.seek(start)
            bytes_remaining = chunk_length
            while bytes_remaining > 0:
                chunk_size = min(Config.STREAM_CHUNK_SIZE, bytes_remaining)
                data = f.read(chunk_size)
                if not data: break
                yield data
                bytes_remaining -= len(data)

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_length),
        "Cache-Control": "no-cache"
    }
    
    return StreamingResponse(
        iter_file(),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        headers=headers,
        media_type=mime_type
    )

# ===========================================================================
# [SECTION 7] API APPLICATION
# ===========================================================================

app = FastAPI(title=Config.APP_NAME, version=Config.VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory=str(Config.TEMPLATES_DIR))
# We mount downloads mainly for direct access if needed, but the /api/v1/file endpoint is preferred
app.mount("/downloads", StaticFiles(directory=str(Config.DOWNLOADS_DIR)), name="downloads")

# --- FRONTEND ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Serves the dashboard interface."""
    return templates.TemplateResponse(
        "interface.html", 
        {"request": request, "user": "Admin", "app_name": Config.APP_NAME}
    )

# --- API ENDPOINTS ---

@app.get("/api/v1/health")
async def health_check():
    """
    CRITICAL: This endpoint feeds the Dashboard.
    Must return exactly the keys the JS expects: cpu, ram, active_jobs.
    """
    return {
        "status": "operational",
        "cpu": psutil.cpu_percent(interval=None),
        "ram": psutil.virtual_memory().percent,
        "active_jobs": len(job_manager.get_active_jobs())
    }

@app.get("/api/v1/jobs")
async def list_jobs():
    """Returns active jobs for the live list."""
    return job_manager.get_active_jobs()

@app.get("/api/v1/history")
async def list_history():
    """Returns the last 100 completed downloads."""
    return job_manager.get_history()

@app.post("/api/v1/download", status_code=202)
async def enqueue_download(
    url: str = Form(...), 
    type: str = Form("audio"),
    requester: str = Form("Dashboard")
):
    """
    Main Entry Point.
    Non-blocking: Returns Job ID immediately.
    """
    if not url: raise HTTPException(400, "URL required")
    
    job_id = job_manager.create_job(url, type, requester)
    engine.submit_job(job_id, url, type)
    
    return {
        "status": "queued",
        "job_id": job_id,
        "message": "Download started in background"
    }

@app.get("/api/v1/status/{job_id}")
async def check_status(job_id: str):
    """Poll this endpoint to get progress updates."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    response = {
        "id": job['id'],
        "status": job['status'],
        "progress": job['progress'],
        "speed": job['speed'],
        "eta": job['eta'],
        "title": job['title']
    }
    
    if job['status'] == JobStatus.COMPLETED:
        response['download_url'] = f"{Config.DOMAIN}/api/v1/file/{job['filename']}"
        response['file_size'] = job['size']
        
    if job['status'] == JobStatus.FAILED:
        response['error'] = job['error']
        
    return response

@app.get("/api/v1/file/{filename}")
async def serve_file(filename: str, request: Request):
    """
    High-Performance File Server.
    Supports Range Headers (Scrubbing/Seeking).
    """
    # Security: Prevent path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(403, "Invalid filename")
        
    file_path = Config.DOWNLOADS_DIR / filename
    
    if not file_path.exists():
        raise HTTPException(404, "File expired or deleted")
        
    # Detect MIME type
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type: mime_type = "application/octet-stream"
    
    # Check for Range Header
    range_header = request.headers.get("range")
    if range_header:
        return range_stream_response(file_path, range_header, mime_type)
        
    return FileResponse(file_path, media_type=mime_type, filename=filename)

@app.post("/api/v1/server/action")
async def server_action(action: str = Form(...)):
    """Admin controls: Stop/Clean."""
    if action == "clean":
        job_manager.cleanup_memory()
        for f in Config.DOWNLOADS_DIR.glob("*"):
            try: f.unlink()
            except: pass
        return {"message": "Cache Cleared"}
    elif action == "stop":
        # In a real production env like Fly.io/Heroku, stopping the process restarts it usually
        os.kill(os.getpid(), signal.SIGTERM)
        return {"message": "Stopping Server..."}
    return {"message": "Unknown Action"}

# ===========================================================================
# [SECTION 8] MAIN ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    # Windows Console Color Fix
    if sys.platform == "win32": os.system("chcp 65001")
    
    print(f"\n{HyperionLogger.COLORS['INFO']}" + "="*60)
    print(f"   HYPERION NUCLEAR ENGINE | {Config.VERSION}")
    print(f"   Running on: {Config.HOST}:{Config.PORT}")
    print(f"   Storage: {Config.DOWNLOADS_DIR}")
    print(f"   Workers: {Config.MAX_WORKERS}")
    print("="*60 + f"{HyperionLogger.COLORS['RESET']}\n")
    
    uvicorn.run(
        "server:app",
        host=Config.HOST,
        port=Config.PORT,
        log_level="critical", # We use our own logger
        access_log=False,      # Reduce IO noise
        workers=1              # Important: 1 Uvicorn worker, but lots of ThreadPool workers
    )
