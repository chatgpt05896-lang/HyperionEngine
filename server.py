# server.py
# HyperionEngine - FastAPI server (single-file)
# Serves templates/interface.html, provides /api/v1/* endpoints, mounts /downloads static files
# Includes ffmpeg check, yt-dlp download engine, background cleanup, and BOT_PLAY_CALLABLE integration.

import os
import sys
import time
import uuid
import shutil
import logging
import asyncio
import threading
import contextlib
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

# --- third-party imports (must be in requirements.txt) ---
try:
    import uvicorn
    from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, status
    from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.templating import Jinja2Templates
    from fastapi.staticfiles import StaticFiles
    import aiohttp
    import aiofiles
    import yt_dlp
    import psutil
except Exception as e:
    print("Missing dependencies. Run: pip install -r requirements.txt")
    raise

# -----------------------
# Configuration
# -----------------------
class Config:
    APP_NAME = "Hyperion Nuclear"
    VERSION = "9.0.1-Integrated"
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8080"))
    DOMAIN = os.getenv("DOMAIN", f"http://localhost:{PORT}")
    BASE_DIR = Path(__file__).resolve().parent
    STORAGE_DIR = BASE_DIR / "storage"
    DOWNLOADS_DIR = STORAGE_DIR / "downloads"
    TEMPLATES_DIR = BASE_DIR / "templates"
    LOGS_DIR = BASE_DIR / "logs"
    TMP_DIR = STORAGE_DIR / "tmp"
    MAX_WORKERS = int(os.getenv("HYPERION_MAX_WORKERS", "8"))
    DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1200"))
    STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", 1024 * 1024 * 2))
    FILE_RETENTION_SECONDS = int(os.getenv("FILE_RETENTION_SECONDS", "900"))
    CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "60"))
    YTDLP_BUFFER_SIZE = int(os.getenv("YTDLP_BUFFER_SIZE", 1024 * 1024 * 8))
    BOT_PLAY_CALLABLE = os.getenv("BOT_PLAY_CALLABLE", "AnnieXMedia.core.call:join_call")
    YTDLP_TIMEOUT = int(os.getenv("YTDLP_TIMEOUT", "300"))
    YOUTUBE_META_TTL = int(os.getenv("YOUTUBE_META_TTL", "300"))

    @staticmethod
    def ensure_dirs():
        for p in [Config.STORAGE_DIR, Config.DOWNLOADS_DIR, Config.TEMPLATES_DIR, Config.LOGS_DIR, Config.TMP_DIR]:
            p.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                with contextlib.suppress(Exception):
                    os.chmod(p, 0o777)

Config.ensure_dirs()

# -----------------------
# Logger
# -----------------------
logger = logging.getLogger("hyperion")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(Config.LOGS_DIR / "server.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

def log_info(msg: str):
    logger.info(msg)

def log_warn(msg: str):
    logger.warning(msg)

def log_error(msg: str):
    logger.error(msg)

# -----------------------
# Job manager
# -----------------------
class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    CONVERTING = "converting"
    COMPLETED = "completed"
    FAILED = "failed"

class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Dict] = {}
        self._history: List[Dict] = []
        self._lock = threading.RLock()

    def create_job(self, url: str, type_: str, requester: str) -> str:
        job_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "url": url,
                "type": type_,
                "requester": requester,
                "status": JobStatus.QUEUED,
                "created_at": time.time(),
                "progress": 0.0,
                "speed": "Waiting...",
                "eta": "--:--",
                "filename": None,
                "filepath": None,
                "title": "Resolving...",
                "size": None,
                "error": None,
                "completed_at": None,
            }
        return job_id

    def update_job(self, job_id: str, **kwargs):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kwargs)
                if kwargs.get("status") == JobStatus.COMPLETED:
                    self._jobs[job_id]["completed_at"] = time.time()
                    job_copy = self._jobs[job_id].copy()
                    self._history.insert(0, job_copy)
                    if len(self._history) > 100:
                        self._history.pop()

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_active_jobs(self) -> List[Dict]:
        with self._lock:
            return [j for j in self._jobs.values() if j['status'] in (JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.CONVERTING)]

    def get_history(self) -> List[Dict]:
        with self._lock:
            return list(self._history)

    def cleanup_memory(self):
        with self._lock:
            now = time.time()
            to_remove = []
            for jid, job in list(self._jobs.items()):
                if job['status'] in (JobStatus.COMPLETED, JobStatus.FAILED):
                    completed_at = job.get("completed_at", now)
                    if now - completed_at > 300:
                        to_remove.append(jid)
            for jid in to_remove:
                self._jobs.pop(jid, None)

job_manager = JobManager()

# -----------------------
# Download engine (yt-dlp) - runs in threadpool
# -----------------------
from concurrent.futures import ThreadPoolExecutor
executor = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS, thread_name_prefix="hyperion-worker")

def _ytdlp_progress_hook(job_id: str, d: dict):
    try:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0) or 0
            pct = round((downloaded / total * 100), 1) if total > 0 else 0.0
            job_manager.update_job(job_id, status=JobStatus.PROCESSING, progress=pct, speed=d.get("_speed_str", "N/A"), eta=d.get("_eta_str", "..."))
        elif d.get("status") == "finished":
            job_manager.update_job(job_id, status=JobStatus.CONVERTING, progress=99.0, speed="Processing...")
    except Exception:
        pass

def _download_worker(job_id: str, url: str, mode: str):
    log_info(f"[worker] start job={job_id} url={url} mode={mode}")
    outtmpl = str(Config.DOWNLOADS_DIR / f"{job_id}.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "ignoreerrors": True,
        "geo_bypass": True,
        "progress_hooks": [lambda d: _ytdlp_progress_hook(job_id, d)],
        "buffersize": Config.YTDLP_BUFFER_SIZE,
        "concurrent_fragment_downloads": 4,
        "retries": 3,
    }
    if mode == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192"
            }]
        })
    else:
        opts.update({
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4"
        })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown")
            job_manager.update_job(job_id, title=title)
            ydl.download([url])

        # find file
        final_file = None
        for f in Config.DOWNLOADS_DIR.iterdir():
            if f.name.startswith(job_id):
                final_file = f
                break
        if not final_file or not final_file.exists() or final_file.stat().st_size == 0:
            raise FileNotFoundError("yt-dlp did not produce a valid output file")

        size_mb = round(final_file.stat().st_size / (1024 * 1024), 2)
        job_manager.update_job(job_id,
                               status=JobStatus.COMPLETED,
                               progress=100.0,
                               filename=final_file.name,
                               filepath=str(final_file.resolve()),
                               size=f"{size_mb} MB",
                               completed_at=time.time(),
                               eta="Done")
        log_info(f"[worker] job completed {job_id} -> {final_file.name}")
    except Exception as e:
        log_error(f"[worker] job failed {job_id}: {e}")
        job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e), completed_at=time.time())

def submit_download_job(url: str, mode: str, requester: str) -> str:
    job_id = job_manager.create_job(url, mode, requester)
    executor.submit(_download_worker, job_id, url, mode)
    return job_id

# -----------------------
# Cleaner thread (files + memory)
# -----------------------
def cleaner_loop():
    log_info("Cleaner started")
    while True:
        try:
            now = time.time()
            removed = 0
            for f in list(Config.DOWNLOADS_DIR.glob("*")):
                try:
                    if f.is_file() and (now - f.stat().st_mtime > Config.FILE_RETENTION_SECONDS):
                        f.unlink()
                        removed += 1
                except Exception:
                    pass
            if removed:
                log_info(f"Cleaner removed {removed} files")
            job_manager.cleanup_memory()
        except Exception as e:
            log_error(f"Cleaner error: {e}")
        time.sleep(Config.CLEANUP_INTERVAL)

threading.Thread(target=cleaner_loop, daemon=True).start()

# -----------------------
# ffmpeg availability
# -----------------------
FFMPEG_BIN = os.getenv("FFMPEG_BIN", shutil.which("ffmpeg") or "ffmpeg")
ffmpeg_available = False

def ensure_ffmpeg() -> bool:
    global ffmpeg_available
    if shutil.which(FFMPEG_BIN):
        ffmpeg_available = True
        log_info(f"ffmpeg found: {shutil.which(FFMPEG_BIN)}")
        return True
    log_warn("ffmpeg not found in container. Please include ffmpeg in Docker image.")
    ffmpeg_available = False
    return False

ensure_ffmpeg()

# -----------------------
# Dynamic bot callable
# -----------------------
def import_callable(path: str):
    if ":" in path:
        module_path, func_name = path.split(":", 1)
    elif "." in path:
        module_path, func_name = path.rsplit(".", 1)
    else:
        raise ValueError("BOT_PLAY_CALLABLE must be module:callable or module.callable")
    module = __import__(module_path, fromlist=[func_name])
    func = getattr(module, func_name)
    return func

async def call_bot_play(chat_id: int, file_path: str, options: dict):
    try:
        func = import_callable(Config.BOT_PLAY_CALLABLE)
    except Exception as e:
        raise RuntimeError(f"Failed to import BOT_PLAY_CALLABLE: {e}")
    if asyncio.iscoroutinefunction(func):
        return await func(chat_id, file_path, **(options or {}))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(chat_id, file_path, **(options or {})))

# -----------------------
# Range streaming helper
# -----------------------
from fastapi import Header

def range_stream_response(file_path: Path, range_header: str, mime_type: str):
    file_size = file_path.stat().st_size
    start, end = 0, file_size - 1
    if range_header:
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
        except Exception:
            start, end = 0, file_size - 1
    if start >= file_size:
        start = file_size - 1
    if end >= file_size:
        end = file_size - 1
    chunk_len = (end - start) + 1

    def iter_file():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = chunk_len
            while remaining > 0:
                read_size = min(Config.STREAM_CHUNK_SIZE, remaining)
                data = f.read(read_size)
                if not data:
                    break
                yield data
                remaining -= len(data)

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_len),
        "Cache-Control": "no-cache"
    }
    return StreamingResponse(iter_file(), status_code=status.HTTP_206_PARTIAL_CONTENT, headers=headers, media_type=mime_type)

# -----------------------
# FastAPI app & routes
# -----------------------
app = FastAPI(title=Config.APP_NAME, version=Config.VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

templates = Jinja2Templates(directory=str(Config.TEMPLATES_DIR))
app.mount("/downloads", StaticFiles(directory=str(Config.DOWNLOADS_DIR)), name="downloads")

@app.on_event("startup")
async def on_startup():
    # ensure ffmpeg available - best-effort
    ensure_ffmpeg()
    log_info("Hyperion server startup complete")

# Frontend
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("interface.html", {"request": request, "user": "Admin", "app_name": Config.APP_NAME})

# Health & status
@app.get("/api/v1/health")
async def health():
    return {
        "status": "operational",
        "cpu": psutil.cpu_percent(interval=None),
        "ram": psutil.virtual_memory().percent,
        "active_jobs": len(job_manager.get_active_jobs()),
        "ffmpeg": ffmpeg_available,
    }

@app.get("/api/v1/jobs")
async def list_jobs():
    return job_manager.get_active_jobs()

# history: return frontend-friendly fields (title, size, file_path)
@app.get("/api/v1/history")
async def list_history():
    raw = job_manager.get_history()
    out = []
    for j in raw:
        filename = j.get("filename") or (Path(j.get("filepath")).name if j.get("filepath") else "")
        out.append({
            "id": j.get("id"),
            "title": (j.get("title") or "")[:200],
            "size": j.get("size") or "",
            "filename": filename,
            # frontend expects file_path to be the filename used with /downloads/<filename>
            "file_path": filename,
            "completed_at": j.get("completed_at", 0)
        })
    return out

# enqueue download
@app.post("/api/v1/download", status_code=202)
async def api_download(url: str = Form(...), type: str = Form("audio"), requester: str = Form("Dashboard")):
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    job_id = submit_download_job(url, type, requester)
    return {"status": "queued", "job_id": job_id, "message": "Download started in background"}

@app.get("/api/v1/status/{job_id}")
async def api_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    response = {
        "id": job["id"],
        "status": job["status"],
        "progress": job["progress"],
        "speed": job["speed"],
        "eta": job["eta"],
        "title": job["title"],
    }
    if job["status"] == JobStatus.COMPLETED:
        filename = job.get("filename") or (Path(job.get("filepath")).name if job.get("filepath") else "")
        response["download_url"] = f"{Config.DOMAIN}/downloads/{filename}"
        response["file_path"] = filename
        response["file_size"] = job.get("size")
    if job["status"] == JobStatus.FAILED:
        response["error"] = job.get("error")
    return response

# Serve file with Range support via /api/v1/file/<filename>
@app.get("/api/v1/file/{filename}")
async def api_file(filename: str, request: Request):
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=403, detail="Invalid filename")
    file_path = Config.DOWNLOADS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File expired or deleted")
    mime_type = (None)
    import mimetypes as _m
    mime_type, _ = _m.guess_type(str(file_path))
    if not mime_type:
        mime_type = "application/octet-stream"
    range_header = request.headers.get("range")
    if range_header:
        return range_stream_response(file_path, range_header, mime_type)
    return FileResponse(file_path, media_type=mime_type, filename=filename)

# Admin actions
@app.post("/api/v1/server/action")
async def server_action(action: str = Form(...)):
    if action == "clean":
        job_manager.cleanup_memory()
        removed = 0
        for f in list(Config.DOWNLOADS_DIR.glob("*")):
            try:
                f.unlink(); removed += 1
            except Exception:
                pass
        return {"message": "Cache Cleared", "removed": removed}
    if action == "stop":
        # graceful stop (container orchestration will restart if configured)
        os.kill(os.getpid(), 15)
        return {"message": "Stopping Server..."}
    return {"message": "Unknown Action"}

# Play endpoint to instruct bot to play (calls BOT_PLAY_CALLABLE)
@app.post("/api/v1/play")
async def api_play(chat_id: int = Form(...), source: str = Form(...), background: BackgroundTasks = None):
    if not source or not chat_id:
        raise HTTPException(status_code=400, detail="chat_id and source required")
    if not ffmpeg_available:
        # we still allow since Hyperion may supply direct stream URLs; but warn
        log_warn("ffmpeg not available on server; some operations may fail")
    # Prepare: if source is http/https use directly, if yt: or youtube use ytdlp to prepare a local file
    try:
        # If it's a URL (http/https) -> pass to bot directly as source
        if source.startswith("http://") or source.startswith("https://"):
            local_path = source
        elif source.startswith("yt:") or "youtube.com" in source or "youtu.be" in source:
            # Download to tmp then provide path
            tmp_name = Config.TMP_DIR / (uuid.uuid4().hex + ".mp3")
            # run yt-dlp in subprocess to extract audio
            cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(tmp_name), source[3:] if source.startswith("yt:") else source]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log_error(f"yt-dlp failed for play: {stderr.decode(errors='ignore')}")
                raise HTTPException(status_code=500, detail="yt-dlp failed during play preparation")
            # locate actual file (yt-dlp might append ext)
            found = None
            for f in Config.TMP_DIR.iterdir():
                if f.name.startswith(tmp_name.name.split(".")[0]):
                    found = f
                    break
            if not found:
                raise HTTPException(status_code=500, detail="Failed to prepare play file")
            local_path = str(found)
        else:
            # assume local path
            local_path = source
    except HTTPException:
        raise
    except Exception as e:
        log_error(f"prepare for play failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    async def _bg_play(chat_id: int, path: str):
        try:
            await call_bot_play(chat_id, path, {})
        except Exception as e:
            log_error(f"call_bot_play error: {e}")

    if background:
        background.add_task(_bg_play, chat_id, local_path)
    else:
        asyncio.create_task(_bg_play(chat_id, local_path))

    return {"status": "started", "local_file": local_path}

# -----------------------
# Periodic cleanup coroutine (also run at startup)
# -----------------------
async def periodic_cleanup():
    while True:
        try:
            now = time.time()
            removed = 0
            for f in list(Config.DOWNLOADS_DIR.glob("*")):
                try:
                    if f.is_file() and (now - f.stat().st_mtime > Config.FILE_RETENTION_SECONDS):
                        f.unlink(); removed += 1
                except Exception:
                    pass
            if removed:
                log_info(f"Periodic cleanup removed {removed} files")
            job_manager.cleanup_memory()
        except Exception as e:
            log_error(f"periodic_cleanup error: {e}")
        await asyncio.sleep(Config.CLEANUP_INTERVAL)

@app.on_event("startup")
async def start_periodic_cleanup():
    asyncio.create_task(periodic_cleanup())

# -----------------------
# Run server
# -----------------------
if __name__ == "__main__":
    if sys.platform == "win32":
        os.system("chcp 65001")
    log_info(f"Starting {Config.APP_NAME} v{Config.VERSION} on {Config.HOST}:{Config.PORT}")
    uvicorn.run("server:app", host=Config.HOST, port=Config.PORT, log_level="info", workers=int(os.getenv("UVICORN_WORKERS", "1")))
