# -*- coding: utf-8 -*-
"""
#############################################################################
#                                                                           #
#         H Y P E R I O N   E N G I N E   |   P R O D U C T I O N           #
#         -------------------------------------------------------           #
#   High-Performance Media Streaming & Async Processing Architecture        #
#                                                                           #
#   ➻ sᴏᴜʀᴄᴇ : بُودَا | ʙᴏᴅᴀ                                                #
#   Status: Stable / Optimized / Non-Blocking                               #
#                                                                           #
#############################################################################
"""

import os
import sys
import time
import json
import uuid
import logging
import asyncio
import shutil
import signal
import socket
import secrets
import mimetypes
import platform
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List, Union, Generator, Tuple
from concurrent.futures import ThreadPoolExecutor

# --- Third Party Libraries ---
# pip install fastapi uvicorn yt-dlp psutil aiofiles jinja2 python-multipart
import uvicorn
import psutil
from pydantic import BaseModel, Field
from fastapi import (
    FastAPI, Request, HTTPException, BackgroundTasks, 
    Form, Depends, Header, status
)
from fastapi.responses import (
    JSONResponse, StreamingResponse, HTMLResponse, 
    FileResponse, RedirectResponse
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from yt_dlp import YoutubeDL

# ===========================================================================
# [SECTION 1] CONFIGURATION & ENVIRONMENT
# ===========================================================================

class AppConfig:
    """
    Centralized Configuration Management.
    Controls all aspects of the engine's behavior.
    """
    # Identity
    APP_NAME: str = "Hyperion Media Engine"
    VERSION: str = "6.2.0-Stable"
    SOURCE_CREDIT: str = "➻ sᴏᴜʀᴄᴇ : بُودَا | ʙᴏᴅᴀ"
    
    # Server Binding
    HOST: str = "0.0.0.0"
    PORT: int = int(os.getenv("PORT", 8080))
    DOMAIN: str = os.getenv("DOMAIN", f"http://localhost:{PORT}")
    
    # Concurrency & Performance
    MAX_WORKERS: int = 16  # Max simultaneous downloads
    EXECUTION_TIMEOUT: int = 600  # 10 Minutes max per job
    STREAM_CHUNK_SIZE: int = 1024 * 1024 * 2  # 2MB chunks for streaming
    
    # Paths (Auto-generated)
    BASE_DIR: Path = Path(__file__).resolve().parent
    STORAGE_DIR: Path = BASE_DIR / "storage"
    DOWNLOADS_DIR: Path = STORAGE_DIR / "downloads"
    LOGS_DIR: Path = BASE_DIR / "logs"
    TEMPLATES_DIR: Path = BASE_DIR / "templates"
    
    # Cleanup Policy
    FILE_RETENTION_SECONDS: int = 900  # 15 Minutes (Strict)
    CLEANUP_INTERVAL: int = 60     # Run cleaner every 60s
    
    # Network Optimization (DNS)
    DNS_SERVERS: List[str] = ["1.1.1.1", "8.8.8.8"]

    @classmethod
    def init_storage(cls):
        """Ensure all required directories exist with correct permissions."""
        for path in [cls.STORAGE_DIR, cls.DOWNLOADS_DIR, cls.LOGS_DIR, cls.TEMPLATES_DIR]:
            path.mkdir(parents=True, exist_ok=True)
            # On Linux/Unix, ensure write permissions
            if os.name != 'nt':
                try:
                    os.chmod(path, 0o755)
                except Exception:
                    pass

AppConfig.init_storage()

# ===========================================================================
# [SECTION 2] ADVANCED LOGGING (THREAD-SAFE)
# ===========================================================================

class ProductionLogger:
    """
    Thread-safe, colorful logger for production debugging.
    """
    RESET = "\033[0m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"

    def __init__(self):
        self.logger = logging.getLogger("HyperionCore")
        self.logger.setLevel(logging.INFO)
        
        # File Handler
        fh = logging.FileHandler(AppConfig.LOGS_DIR / "engine.log", encoding='utf-8')
        fh.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)s | [%(threadName)s] | %(message)s'
        ))
        self.logger.addHandler(fh)
        
        # Console Handler
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(ch)

    def info(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.info(f"{self.GREEN}[INFO]    {timestamp} | {msg}{self.RESET}")

    def warn(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.warning(f"{self.YELLOW}[WARNING] {timestamp} | {msg}{self.RESET}")

    def error(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.error(f"{self.RED}[ERROR]   {timestamp} | {msg}{self.RESET}")

    def debug(self, msg: str):
        # Only enable if needed
        # timestamp = datetime.now().strftime("%H:%M:%S")
        # self.logger.debug(f"{self.CYAN}[DEBUG]   {timestamp} | {msg}{self.RESET}")
        pass

sys_logger = ProductionLogger()

# ===========================================================================
# [SECTION 3] DATA MODELS (SCHEMAS)
# ===========================================================================

class DownloadRequest(BaseModel):
    url: str = Field(..., description="The URL of the media to download")
    type: str = Field("audio", description="audio or video")
    requester: str = Field("API", description="Source of request")

class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: float
    speed: str
    eta: str
    filename: Optional[str] = None
    download_url: Optional[str] = None
    error: Optional[str] = None

# ===========================================================================
# [SECTION 4] CORE ENGINE: JOB MANAGEMENT
# ===========================================================================

class JobState:
    """Enum-like class for Job Statuses"""
    QUEUED = "queued"
    PROCESSING = "processing"
    CONVERTING = "converting"
    COMPLETED = "completed"
    FAILED = "failed"

class JobManager:
    """
    In-Memory State Manager.
    Handles thread safety for concurrent reads/writes of job status.
    """
    def __init__(self):
        self._jobs: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def create_job(self, url: str, type: str, requester: str) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "url": url,
                "type": type,
                "requester": requester,
                "status": JobState.QUEUED,
                "created_at": time.time(),
                "progress": 0,
                "speed": "N/A",
                "eta": "Calculating...",
                "filename": None,
                "filepath": None,
                "filesize": 0,
                "error": None
            }
        return job_id

    def update_job(self, job_id: str, **kwargs):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kwargs)
                if "status" in kwargs:
                    # Update heartbeat or similar if needed
                    pass

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            return self._jobs.get(job_id, None).copy() if job_id in self._jobs else None

    def get_all_jobs(self) -> List[Dict]:
        with self._lock:
            return list(self._jobs.values())

    def cleanup_stale_jobs(self):
        """Removes job metadata from memory (not files)"""
        with self._lock:
            now = time.time()
            keys_to_remove = []
            for jid, job in self._jobs.items():
                # Keep completed jobs for a bit so client can query status
                if job['status'] in [JobState.COMPLETED, JobState.FAILED]:
                    if now - job.get('completed_at', now) > 600: # 10 mins memory retention
                        keys_to_remove.append(jid)
            
            for k in keys_to_remove:
                del self._jobs[k]

# Global Instance
job_manager = JobManager()

# ===========================================================================
# [SECTION 5] DOWNLOAD ENGINE (YT-DLP WRAPPER)
# ===========================================================================

class DownloadEngine:
    """
    The Heavy Lifter. Executes downloads in separate threads.
    """
    def __init__(self):
        self.executor = ThreadPoolExecutor(
            max_workers=AppConfig.MAX_WORKERS, 
            thread_name_prefix="HyperionWorker"
        )

    def _progress_hook(self, d, job_id):
        """Callback executed by yt-dlp during download"""
        if d['status'] == 'downloading':
            try:
                # Calculate percentage
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                percent = (downloaded / total * 100) if total > 0 else 0
                
                # Update State
                job_manager.update_job(
                    job_id,
                    status=JobState.PROCESSING,
                    progress=round(percent, 2),
                    speed=d.get('_speed_str', 'N/A'),
                    eta=d.get('_eta_str', 'Wait...')
                )
            except Exception:
                pass # Don't break download on progress error

        elif d['status'] == 'finished':
            job_manager.update_job(job_id, status=JobState.CONVERTING, progress=99.0)

    def _get_optimized_opts(self, job_id: str, mode: str, output_template: str) -> dict:
        """Returns the 'Production Grade' yt-dlp configuration"""
        
        # Base Options for Reliability
        opts = {
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'geo_bypass': True,
            'socket_timeout': 15,
            'retries': 3,
            'progress_hooks': [lambda d: self._progress_hook(d, job_id)],
            
            # Speed Optimizations
            'buffersize': 1024 * 1024,  # 1MB buffer
            'http_chunk_size': 10485760, # 10MB chunks
            'concurrent_fragment_downloads': 5, # Parallel fragments
        }

        # DNS Optimization (If possible via external args, usually OS dependent, 
        # but here we ensure requests leverage fast resolution if library supports it)
        # yt-dlp doesn't support setting DNS directly in python opts easily without 
        # patching socket, so we rely on OS.

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
            # Smart format selection: Avoid re-encoding video if possible
            opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'merge_output_format': 'mp4'
            })
            
        return opts

    def _execute_job(self, job_id: str, url: str, mode: str):
        """The function running inside the Worker Thread"""
        sys_logger.info(f"Worker started for Job: {job_id} [{mode}]")
        
        try:
            # 1. Prepare Path
            # We use job_id as filename to prevent collisions
            filename_base = f"{job_id}"
            out_tmpl = str(AppConfig.DOWNLOADS_DIR / f"{filename_base}.%(ext)s")
            
            # 2. Configure yt-dlp
            opts = self._get_optimized_opts(job_id, mode, out_tmpl)
            
            # 3. Execute
            with YoutubeDL(opts) as ydl:
                # Extract info first (fast)
                info = ydl.extract_info(url, download=False)
                
                # Sanitize Title (optional, but we serve by ID mostly)
                clean_title = info.get('title', 'media')
                
                # Download
                ydl.download([url])
                
                # 4. Resolve Final File
                # yt-dlp might change extension (webm -> mp3, etc)
                final_filename = None
                final_path = None
                
                # Scan directory for the file starting with job_id
                # This is more robust than guessing extension
                for f in os.listdir(AppConfig.DOWNLOADS_DIR):
                    if f.startswith(job_id):
                        final_filename = f
                        final_path = AppConfig.DOWNLOADS_DIR / f
                        break
                
                if not final_path or not final_path.exists():
                    raise FileNotFoundError("Output file not found after download")

                # 5. Validation (0KB Check)
                if final_path.stat().st_size < 1024:
                    raise Exception("Downloaded file is empty (Corruption detected)")

                # 6. Success
                job_manager.update_job(
                    job_id,
                    status=JobState.COMPLETED,
                    progress=100.0,
                    filename=final_filename,
                    filepath=str(final_path),
                    filesize=final_path.stat().st_size,
                    eta="00:00",
                    completed_at=time.time()
                )
                sys_logger.info(f"Job {job_id} Completed Successfully.")

        except Exception as e:
            sys_logger.error(f"Job {job_id} Failed: {str(e)}")
            job_manager.update_job(
                job_id,
                status=JobState.FAILED,
                error=str(e),
                completed_at=time.time()
            )

    def submit(self, job_id: str, url: str, mode: str):
        """Submit task to thread pool (Non-Blocking)"""
        self.executor.submit(self._execute_job, job_id, url, mode)

engine = DownloadEngine()

# ===========================================================================
# [SECTION 6] BACKGROUND SERVICES (CLEANER)
# ===========================================================================

class SystemServices:
    
    @staticmethod
    def start_scheduler():
        """Starts the background cleanup thread"""
        t = threading.Thread(target=SystemServices._cleanup_loop, daemon=True)
        t.start()
        sys_logger.info("Auto-Cleanup Scheduler Started.")

    @staticmethod
    def _cleanup_loop():
        while True:
            try:
                now = time.time()
                retention = AppConfig.FILE_RETENTION_SECONDS
                
                # Iterate over files in downloads dir
                for file_path in AppConfig.DOWNLOADS_DIR.glob("*"):
                    if file_path.is_file():
                        # check modification time
                        if now - file_path.stat().st_mtime > retention:
                            try:
                                file_path.unlink() # Delete
                                sys_logger.info(f"Cleaned up expired file: {file_path.name}")
                            except Exception as e:
                                sys_logger.warn(f"Failed to delete {file_path.name}: {e}")
                
                # Cleanup Memory
                job_manager.cleanup_stale_jobs()
                
            except Exception as e:
                sys_logger.error(f"Scheduler Error: {e}")
            
            time.sleep(AppConfig.CLEANUP_INTERVAL)

# ===========================================================================
# [SECTION 7] API APPLICATION & MIDDLEWARE
# ===========================================================================

app = FastAPI(
    title=AppConfig.APP_NAME,
    version=AppConfig.VERSION,
    description="Enterprise Grade Asynchronous Media Downloader",
    docs_url="/docs",
    redoc_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Template Engine
templates = Jinja2Templates(directory=str(AppConfig.TEMPLATES_DIR))

# --- Utils for Streaming ---

def is_safe_path(filename: str) -> bool:
    """Path traversal protection"""
    safe_path = AppConfig.DOWNLOADS_DIR / filename
    try:
        return safe_path.resolve().parent == AppConfig.DOWNLOADS_DIR.resolve()
    except:
        return False

def range_stream_response(file_path: Path, range_header: str):
    """
    Advanced Generator for Range Requests.
    Enables seeking in video players (Telegram/Browser).
    """
    file_size = file_path.stat().st_size
    start, end = 0, file_size - 1

    # Parse Range Header (bytes=0-1024)
    if range_header:
        try:
            range_val = range_header.replace("bytes=", "").split("-")
            start = int(range_val[0]) if range_val[0] else 0
            end = int(range_val[1]) if range_val[1] else file_size - 1
        except ValueError:
            pass # Fallback to full file

    # Clamp logic
    if start >= file_size: start = file_size - 1
    if end >= file_size: end = file_size - 1
    
    chunk_length = (end - start) + 1
    
    def file_iterator(fpath, offset, length):
        with open(fpath, "rb") as f:
            f.seek(offset)
            remaining = length
            while remaining > 0:
                chunk_size = min(AppConfig.STREAM_CHUNK_SIZE, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    # Detect MIME
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type: mime_type = "application/octet-stream"

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_length),
        "Cache-Control": "no-cache",
        "X-Source-Credit": AppConfig.SOURCE_CREDIT
    }

    return StreamingResponse(
        file_iterator(file_path, start, chunk_length),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        headers=headers,
        media_type=mime_type
    )

# ===========================================================================
# [SECTION 8] ENDPOINTS (CONTROLLERS)
# ===========================================================================

@app.on_event("startup")
async def startup_event():
    sys_logger.info(f"--- {AppConfig.APP_NAME} v{AppConfig.VERSION} Starting ---")
    sys_logger.info(AppConfig.SOURCE_CREDIT)
    SystemServices.start_scheduler()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Admin Dashboard Interface"""
    # Simple embedded dashboard for diagnostics
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <title>{AppConfig.APP_NAME}</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }}
            .card {{ background: #1e293b; padding: 20px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #334155; }}
            h1 {{ color: #38bdf8; }}
            .status-badge {{ padding: 4px 8px; border-radius: 4px; font-size: 0.8em; font-weight: bold; }}
            .status-queued {{ background: #f59e0b; color: #000; }}
            .status-processing {{ background: #3b82f6; color: #fff; }}
            .status-completed {{ background: #22c55e; color: #fff; }}
            .status-failed {{ background: #ef4444; color: #fff; }}
            code {{ background: #000; padding: 2px 5px; border-radius: 3px; font-family: monospace; color: #22c55e; }}
        </style>
        <script>
            async function fetchJobs() {{
                const res = await fetch('/api/v1/debug/jobs');
                const data = await res.json();
                const container = document.getElementById('jobs-container');
                container.innerHTML = '';
                data.forEach(job => {{
                    const div = document.createElement('div');
                    div.className = 'card';
                    div.innerHTML = `
                        <h3>Job: ${{job.id}} <span class="status-badge status-${{job.status}}">${{job.status}}</span></h3>
                        <p>Type: ${{job.type}} | Progress: ${{job.progress}}% | Speed: ${{job.speed}}</p>
                        <p><small>${{job.url}}</small></p>
                        ${{job.error ? `<p style="color:red">Error: ${{job.error}}</p>` : ''}}
                    `;
                    container.appendChild(div);
                }});
            }}
            setInterval(fetchJobs, 2000);
        </script>
    </head>
    <body>
        <div class="card">
            <h1>{AppConfig.APP_NAME}</h1>
            <p>{AppConfig.SOURCE_CREDIT}</p>
            <p>System Status: 🟢 Operational</p>
            <p>Active Threads: <code>{threading.active_count()}</code></p>
        </div>
        <h2>Active Jobs</h2>
        <div id="jobs-container"></div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/api/v1/download", status_code=202)
async def enqueue_download(
    url: str = Form(...), 
    type: str = Form("audio"),
    requester: str = Form("TelegramBot")
):
    """
    ENDPOINT 1: Initialize Download (Non-Blocking).
    Returns Job ID immediately.
    """
    # 1. Validation
    if not url.startswith("http"):
        raise HTTPException(400, "Invalid URL")
    
    if type not in ["audio", "video"]:
        type = "audio"

    # 2. Create Job
    job_id = job_manager.create_job(url, type, requester)
    
    # 3. Spawn Thread (Fire & Forget)
    engine.submit(job_id, url, type)
    
    sys_logger.info(f"API Request: Enqueued {url} as {job_id}")

    return {
        "status": "queued",
        "job_id": job_id,
        "message": "Download started in background",
        "check_status": f"{AppConfig.DOMAIN}/api/v1/status/{job_id}"
    }

@app.get("/api/v1/status/{job_id}")
async def check_status(job_id: str):
    """
    ENDPOINT 2: Poll Status.
    Returns JSON with progress or final download link.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job ID not found or expired")
    
    response = {
        "id": job['id'],
        "status": job['status'],
        "progress": job['progress'],
        "speed": job['speed'],
        "eta": job['eta']
    }
    
    if job['status'] == JobState.COMPLETED:
        response['download_url'] = f"{AppConfig.DOMAIN}/api/v1/file/{job['filename']}"
        response['file_size'] = job.get('filesize')
        response['file_size_mb'] = round(job.get('filesize', 0) / (1024*1024), 2)
        
    if job['status'] == JobState.FAILED:
        response['error'] = job.get('error')
        
    return response

@app.get("/api/v1/file/{filename}")
async def serve_file(filename: str, request: Request):
    """
    ENDPOINT 3: High-Performance File Delivery.
    Supports Range Headers for Scrubbing.
    """
    # Security Check
    if not is_safe_path(filename):
        raise HTTPException(403, "Access Denied")
    
    file_path = AppConfig.DOWNLOADS_DIR / filename
    
    if not file_path.exists():
        raise HTTPException(404, "File not found or deleted by cleanup")
    
    # Check Range Header
    range_header = request.headers.get("range")
    
    if range_header:
        # Return Streaming Response (206)
        return range_stream_response(file_path, range_header)
    else:
        # Standard File Response (200) - For direct downloads
        return FileResponse(
            file_path, 
            headers={"X-Source-Credit": AppConfig.SOURCE_CREDIT}
        )

@app.get("/api/v1/debug/jobs")
async def debug_jobs():
    """Internal Endpoint for Dashboard"""
    return job_manager.get_all_jobs()

# ===========================================================================
# [SECTION 9] EXECUTION
# ===========================================================================

if __name__ == "__main__":
    # Ensure UTF-8 in Windows Terminals
    if sys.platform == "win32":
        os.system("chcp 65001")
    
    print(f"\n{ProductionLogger.GREEN}" + "="*60)
    print(f"   HYPERION MEDIA ENGINE | {AppConfig.VERSION}")
    print(f"   {AppConfig.SOURCE_CREDIT}")
    print(f"   Storage: {AppConfig.DOWNLOADS_DIR}")
    print("="*60 + f"{ProductionLogger.RESET}\n")
    
    # Run Uvicorn with optimized settings
    uvicorn.run(
        "app:app",
        host=AppConfig.HOST,
        port=AppConfig.PORT,
        reload=False,         # False for production
        workers=1,            # 1 Worker + ThreadPool is best for this specific logic
        log_level="warning",  # We use our own logger
        access_log=False
    )
