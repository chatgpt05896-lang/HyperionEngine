# -*- coding: utf-8 -*-
"""
#############################################################################
#                                                                           #
#         H Y P E R I O N   E N G I N E   |   P R O D U C T I O N           #
#         -------------------------------------------------------           #
#   High-Performance Media Streaming & Async Processing Architecture        #
#                                                                           #
#   ➻ sᴏᴜʀᴄᴇ : بُودَا | ʙᴏᴅᴀ                                                #
#   Status: Stable / Optimized / Linked to templates/interface.html         #
#                                                                           #
#############################################################################
"""

import os
import sys
import time
import uuid
import logging
import threading
import mimetypes
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List
from concurrent.futures import ThreadPoolExecutor

# --- Third Party Libraries ---
import uvicorn
from pydantic import BaseModel, Field
from fastapi import (
    FastAPI, Request, HTTPException, Form, status
)
from fastapi.responses import (
    JSONResponse, StreamingResponse, HTMLResponse, 
    FileResponse
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from yt_dlp import YoutubeDL

# ===========================================================================
# [SECTION 1] CONFIGURATION & ENVIRONMENT
# ===========================================================================

class AppConfig:
    APP_NAME: str = "Hyperion Media Engine"
    VERSION: str = "6.5.0-TemplateMode"
    SOURCE_CREDIT: str = "➻ sᴏᴜʀᴄᴇ : بُودَا | ʙᴏᴅᴀ"
    
    # Server Binding
    HOST: str = "0.0.0.0"
    PORT: int = int(os.getenv("PORT", 8080))
    DOMAIN: str = os.getenv("DOMAIN", f"http://localhost:{PORT}")
    
    # Performance
    MAX_WORKERS: int = 16
    STREAM_CHUNK_SIZE: int = 1024 * 1024 * 2  # 2MB
    
    # Paths
    BASE_DIR: Path = Path(__file__).resolve().parent
    STORAGE_DIR: Path = BASE_DIR / "storage"
    DOWNLOADS_DIR: Path = STORAGE_DIR / "downloads"
    LOGS_DIR: Path = BASE_DIR / "logs"
    TEMPLATES_DIR: Path = BASE_DIR / "templates"
    
    # Cleanup
    FILE_RETENTION_SECONDS: int = 900  # 15 Minutes
    CLEANUP_INTERVAL: int = 60

    @classmethod
    def init_storage(cls):
        """Ensure all required directories exist"""
        for path in [cls.STORAGE_DIR, cls.DOWNLOADS_DIR, cls.LOGS_DIR, cls.TEMPLATES_DIR]:
            path.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt':
                try: os.chmod(path, 0o755)
                except: pass

AppConfig.init_storage()

# ===========================================================================
# [SECTION 2] LOGGING
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Hyperion")

# ===========================================================================
# [SECTION 3] JOB MANAGEMENT
# ===========================================================================

class JobState:
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class JobManager:
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
                "speed": "Pending...",
                "eta": "...",
                "filename": None,
                "error": None
            }
        return job_id

    def update_job(self, job_id: str, **kwargs):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kwargs)

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            return self._jobs.get(job_id, None).copy() if job_id in self._jobs else None

    def get_all_jobs(self) -> List[Dict]:
        with self._lock:
            return list(reversed(list(self._jobs.values())))

    def cleanup_stale_jobs(self):
        with self._lock:
            now = time.time()
            keys = [k for k, v in self._jobs.items() 
                   if v['status'] in [JobState.COMPLETED, JobState.FAILED] 
                   and now - v.get('completed_at', now) > 600]
            for k in keys: del self._jobs[k]

job_manager = JobManager()

# ===========================================================================
# [SECTION 4] DOWNLOAD ENGINE
# ===========================================================================

class DownloadEngine:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=AppConfig.MAX_WORKERS)

    def _progress_hook(self, d, job_id):
        if d['status'] == 'downloading':
            try:
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                percent = (downloaded / total * 100) if total > 0 else 0
                job_manager.update_job(
                    job_id, 
                    status=JobState.PROCESSING, 
                    progress=round(percent, 2), 
                    speed=d.get('_speed_str', 'N/A'),
                    eta=d.get('_eta_str', '...')
                )
            except: pass
        elif d['status'] == 'finished':
            job_manager.update_job(job_id, progress=99.0, status="finalizing")

    def _execute_job(self, job_id: str, url: str, mode: str):
        logger.info(f"⚡ Job Started: {job_id}")
        try:
            filename_base = f"{job_id}"
            out_tmpl = str(AppConfig.DOWNLOADS_DIR / f"{filename_base}.%(ext)s")
            
            opts = {
                'outtmpl': out_tmpl,
                'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
                'ignoreerrors': True, 'geo_bypass': True,
                'socket_timeout': 15,
                'progress_hooks': [lambda d: self._progress_hook(d, job_id)],
                'buffersize': 1024 * 1024,
                'concurrent_fragment_downloads': 5,
            }

            if mode == "audio":
                opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]})
            else:
                opts.update({'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'})
            
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
                
            final_filename = None
            for f in os.listdir(AppConfig.DOWNLOADS_DIR):
                if f.startswith(job_id):
                    final_filename = f
                    break
            
            if not final_filename: raise Exception("File not created")

            job_manager.update_job(job_id, status=JobState.COMPLETED, progress=100.0, filename=final_filename, completed_at=time.time())
            logger.info(f"✅ Job {job_id} Done")

        except Exception as e:
            logger.error(f"❌ Job {job_id} Failed: {e}")
            job_manager.update_job(job_id, status=JobState.FAILED, error=str(e), completed_at=time.time())

    def submit(self, job_id: str, url: str, mode: str):
        self.executor.submit(self._execute_job, job_id, url, mode)

engine = DownloadEngine()

# ===========================================================================
# [SECTION 5] CLEANER
# ===========================================================================

def cleaner_loop():
    while True:
        try:
            now = time.time()
            for f in AppConfig.DOWNLOADS_DIR.glob("*"):
                if f.is_file() and now - f.stat().st_mtime > AppConfig.FILE_RETENTION_SECONDS:
                    f.unlink()
            job_manager.cleanup_stale_jobs()
        except: pass
        time.sleep(AppConfig.CLEANUP_INTERVAL)

threading.Thread(target=cleaner_loop, daemon=True).start()

def range_stream_response(file_path: Path, range_header: str):
    file_size = file_path.stat().st_size
    start, end = 0, file_size - 1
    if range_header:
        try:
            p = range_header.replace("bytes=", "").split("-")
            start = int(p[0]) if p[0] else 0
            end = int(p[1]) if p[1] else file_size - 1
        except: pass
    
    chunk_length = (end - start) + 1
    def iter_file():
        with open(file_path, "rb") as f:
            f.seek(start)
            rem = chunk_length
            while rem > 0:
                chunk = f.read(min(AppConfig.STREAM_CHUNK_SIZE, rem))
                if not chunk: break
                yield chunk
                rem -= len(chunk)

    headers = {"Content-Range": f"bytes {start}-{end}/{file_size}", "Accept-Ranges": "bytes", "Content-Length": str(chunk_length)}
    return StreamingResponse(iter_file(), status_code=206, headers=headers, media_type="application/octet-stream")

# ===========================================================================
# [SECTION 6] API & TEMPLATE LINKING
# ===========================================================================

app = FastAPI(title=AppConfig.APP_NAME, version=AppConfig.VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Initialize Template Engine pointing to 'templates' folder
templates = Jinja2Templates(directory=str(AppConfig.TEMPLATES_DIR))

# Mount Static Files (Optional, if you have CSS/JS files in a 'static' folder)
# app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """
    Serves the external 'interface.html' file from templates directory.
    """
    return templates.TemplateResponse(
        "interface.html", 
        {
            "request": request, 
            "app_name": AppConfig.APP_NAME,
            "version": AppConfig.VERSION,
            "source": AppConfig.SOURCE_CREDIT
        }
    )

@app.post("/api/v1/download", status_code=202)
async def enqueue(
    url: str = Form(...), 
    type: str = Form("audio"),
    requester: str = Form("WebUI")
):
    job_id = job_manager.create_job(url, type, requester)
    engine.submit(job_id, url, type)
    return {"status": "queued", "job_id": job_id}

@app.get("/api/v1/status/{job_id}")
async def status_endpoint(job_id: str):
    job = job_manager.get_job(job_id)
    if not job: raise HTTPException(404, "Job not found")
    
    res = {
        "id": job['id'],
        "status": job['status'], 
        "progress": job['progress'], 
        "speed": job['speed'],
        "eta": job['eta']
    }
    
    if job['status'] == JobState.COMPLETED:
        res['download_url'] = f"{AppConfig.DOMAIN}/api/v1/file/{job['filename']}"
    if job['status'] == JobState.FAILED: 
        res['error'] = job['error']
        
    return res

@app.get("/api/v1/file/{filename}")
async def serve_file(filename: str, request: Request):
    if not filename.startswith(tuple("0123456789abcdef")): 
        raise HTTPException(403, "Invalid Filename")
        
    file_path = AppConfig.DOWNLOADS_DIR / filename
    if not file_path.exists(): raise HTTPException(404, "File Not Found")
    
    range_header = request.headers.get("range")
    if range_header:
        return range_stream_response(file_path, range_header)
    
    return FileResponse(file_path, headers={"X-Source": AppConfig.SOURCE_CREDIT})

@app.get("/api/v1/debug/jobs")
async def debug_jobs():
    return job_manager.get_all_jobs()

# ===========================================================================
# [SECTION 7] EXECUTION
# ===========================================================================

if __name__ == "__main__":
    if sys.platform == "win32": os.system("chcp 65001")
    
    print(f"\n{ProductionLogger.GREEN}" + "="*60)
    print(f"   HYPERION ENGINE | {AppConfig.VERSION}")
    print(f"   {AppConfig.SOURCE_CREDIT}")
    print(f"   Mode: Template Linked (templates/interface.html)")
    print("="*60 + f"{ProductionLogger.RESET}\n")
    
    uvicorn.run("server:app", host=AppConfig.HOST, port=AppConfig.PORT, workers=1, log_level="warning")
