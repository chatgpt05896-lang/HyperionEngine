# -*- coding: utf-8 -*-
"""
server.py — Hyper-Tube X
خادم متكامل: بحث، تنزيل (yt-dlp + aria2c)، WebSocket للتقدّم، بث (Range) وملفات ثابتة.
ملاحظات:
 - يستخدم cookies.txt تلقائياً إن وُجد في جذر المشروع.
 - كل استدعاءات yt-dlp تعمل في ThreadPoolExecutor (لتجنّب حجب asyncio loop).
 - قراءة PORT من المتغير البيئي (مناسب لـ Fly.io).
 - واجهة templates/index.html تُخدم عبر Jinja2.
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
import traceback
import asyncio
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from functools import wraps

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates

# Optional libs
try:
    import yt_dlp as yt_dlp_module
except Exception:
    yt_dlp_module = None

try:
    import psutil
except Exception:
    psutil = None

# ----------------------------
# تهيئة ومسارات
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_FILE = BASE_DIR / "cookies.txt"  # الملف الذي رفعتَه

# إن لم توجد المجلدات، ننشئها
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
if not TEMPLATES_DIR.exists():
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hyper-tube-x")

# FastAPI app
app = FastAPI(title="Hyper-Tube X")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ضيّقها لاحقًا إن أردت
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Static files (إن وُجد مجلد static)
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ----------------------------
# أدوات مساعدة
# ----------------------------
def safe_filename(s: str, max_len: int = 180) -> str:
    """تحويل عنوان إلى اسم ملف آمن (يُستخدم لتخمين أسماء الملفات إن لزم)."""
    if not s:
        return ""
    # نسمح بحروف عربية وإنجليزية وأرقام ومسافات و_-.
    import re
    cleaned = re.sub(r"[^0-9A-Za-z\u0600-\u06FF \-_.]", "", s)
    cleaned = cleaned.strip().replace(" ", "_")
    return cleaned[:max_len]

def now_ts() -> float:
    return time.time()

# ----------------------------
# WebSocket manager
# ----------------------------
class WSManager:
    """يدير اتصالات WebSocket لكل client_id"""
    def __init__(self):
        self._conns: Dict[str, WebSocket] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def connect(self, client_id: str, ws: WebSocket):
        await ws.accept()
        self._conns[client_id] = ws
        self._locks[client_id] = asyncio.Lock()
        logger.info(f"WS connected: {client_id}")

    def disconnect(self, client_id: str):
        if client_id in self._conns:
            try:
                del self._conns[client_id]
            except KeyError:
                pass
        if client_id in self._locks:
            try:
                del self._locks[client_id]
            except KeyError:
                pass
        logger.info(f"WS disconnected: {client_id}")

    async def send_json(self, client_id: str, payload: dict):
        """إرسال رسالة JSON لعميل محدد؛ إذا لم يكن متصلًا نُهمل."""
        ws = self._conns.get(client_id)
        if not ws:
            logger.debug(f"No websocket for client {client_id}")
            return
        lock = self._locks.get(client_id)
        if lock:
            async with lock:
                try:
                    await ws.send_json(payload)
                except Exception as e:
                    logger.warning(f"Failed to send to {client_id}: {e}")

    async def broadcast(self, payload: dict):
        """إرسال رسالة لكل العملاء (يجب الحذر في الإنتاج)."""
        for cid in list(self._conns.keys()):
            try:
                await self.send_json(cid, payload)
            except Exception:
                pass

ws_manager = WSManager()

# ----------------------------
# DownloadManager
# ----------------------------
class DownloadManager:
    """
    مدير التنزيلات — ينفذ yt-dlp ضمن ThreadPool ويستخدم aria2c كـ external_downloader.
    يحتفظ بحالة المهام في self.status.
    """
    def __init__(self,
                 downloads_dir: Path = DOWNLOADS_DIR,
                 max_workers: int = 3,
                 aria2c_args: Optional[List[str]] = None,
                 search_cache_ttl: int = 300):
        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.status: Dict[str, Dict[str, Any]] = {}  # job_id -> metadata/state
        self.aria2c_args = aria2c_args or [
            "--split=16", "--max-connection-per-server=8", "--min-split-size=1M", "--max-tries=5", "--retry-wait=3"
        ]
        self.loop: Optional[asyncio.AbstractEventLoop] = None  # سيُحدَّد عند startup
        # Cache للبحث: query -> (result, ts)
        self._search_cache: Dict[str, Tuple[Any, float]] = {}
        self._search_cache_ttl = search_cache_ttl

    async def start_worker(self):
        """تشغيل حلقة العامل — يجب استدعاؤها أثناء startup لتعيين اللوب."""
        if self.loop is None:
            self.loop = asyncio.get_event_loop()
        logger.info("Starting download worker loop")
        # لا نريد spawn غير محدود عند استدعاء start أكثر من مرة
        asyncio.create_task(self._worker_loop())

    async def _worker_loop(self):
        logger.info("Worker loop running")
        while True:
            job = await self.queue.get()
            try:
                await self._process_job(job)
            except Exception as e:
                logger.exception("Error processing job: %s", e)
            finally:
                try:
                    self.queue.task_done()
                except Exception:
                    pass

    async def enqueue(self, url: str, client_id: str) -> str:
        """إضافة مهمة للطابور وإرجاع job_id"""
        job_id = str(uuid4())
        job = {"id": job_id, "url": url, "client_id": client_id, "created_at": now_ts()}
        self.status[job_id] = {"state": "queued", "progress": 0, "url": url, "created_at": now_ts()}
        await self.queue.put(job)
        # إحاطة المستخدم
        await ws_manager.send_json(client_id, {"event": "queued", "job_id": job_id, "url": url})
        logger.info(f"Enqueued job={job_id} url={url} client={client_id}")
        return job_id

    async def _process_job(self, job: dict):
        job_id = job["id"]
        url = job["url"]
        client_id = job["client_id"]
        logger.info(f"Processing job {job_id}: {url}")
        self.status[job_id].update({"state": "processing", "progress": 0, "started_at": now_ts()})
        await ws_manager.send_json(client_id, {"event": "started", "job_id": job_id, "url": url})

        # خطوة 1: استخراج الميتاداتا
        try:
            info = await asyncio.get_event_loop().run_in_executor(self.executor, self._ydl_extract_info_blocking, url)
            if not info:
                raise RuntimeError("yt-dlp returned no info (possible blocking or missing cookies)")
            self.status[job_id]["info"] = info
        except Exception as e:
            logger.exception("Failed to extract info for %s: %s", url, e)
            err = str(e)
            if "Sign in to confirm" in err or "cookie" in err.lower():
                err = "يوتيوب يطلب تسجيل دخول أو ملفات الكوكيز. حدّث cookies.txt."
            self.status[job_id].update({"state": "error", "error": err})
            await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": err})
            return

        # خطوة 2: إعداد خيارات yt-dlp والتحميل (external_downloader = aria2c)
        out_template = "%(id)s.%(ext)s"
        ytdl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": str(self.downloads_dir / out_template),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "external_downloader": "aria2c",
            "external_downloader_args": self.aria2c_args,
            "progress_hooks": [self._make_progress_hook(job_id, client_id)],
            "retries": 3,
            "fragment_retries": 3,
            # تحسين: تخطي SSL verification إن لزم (تعطيل مؤقت فقط إن كنت تعلم ما تفعل)
            # "nocheckcertificate": True,
        }
        # تضمين cookies إن وُجدت
        if COOKIES_FILE.exists():
            ytdl_opts["cookiefile"] = str(COOKIES_FILE)
            logger.info(f"Using cookies: {COOKIES_FILE}")

        # رأس User-Agent ثابت لتحسين التوافق
        ytdl_opts["http_headers"] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }

        # تشغيل التحميل في ThreadPool
        try:
            result = await asyncio.get_event_loop().run_in_executor(self.executor, self._ydl_download_blocking, url, ytdl_opts)
            logger.debug("yt-dlp returned: %s", result)
        except Exception as e:
            msg = str(e)
            logger.exception("Download failed for %s: %s", url, msg)
            status_msg = msg[:400] if len(msg) > 400 else msg
            self.status[job_id].update({"state": "error", "error": status_msg})
            await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": status_msg})
            return

        # خطوة 3: تعيين اسم الملف الفعلي
        try:
            vid_id = (info.get("id") or job_id) if isinstance(info, dict) else job_id
            found_file = None
            for p in self.downloads_dir.iterdir():
                if p.name.startswith(f"{vid_id}."):
                    found_file = p
                    break
            if not found_file:
                candidates = sorted(self.downloads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
                found_file = candidates[0] if candidates else None
        except Exception:
            found_file = None

        filename = found_file.name if found_file else None
        self.status[job_id].update({"state": "finished", "progress": 100, "filename": filename, "finished_at": now_ts()})
        await ws_manager.send_json(client_id, {"event": "finished", "job_id": job_id, "meta": info, "filename": filename})
        logger.info(f"Job {job_id} finished -> file={filename}")

    # -------- blocking helpers (run in ThreadPool) --------
    def _ydl_extract_info_blocking(self, url: str) -> dict:
        """استخراج ميتاداتا بواسطة yt-dlp (blocking)"""
        if yt_dlp_module is None:
            raise RuntimeError("yt-dlp غير مثبت. نفّذ: pip install yt-dlp")
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "format": "bestvideo+bestaudio/best"
        }
        if COOKIES_FILE.exists():
            ydl_opts["cookiefile"] = str(COOKIES_FILE)
        with yt_dlp_module.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # نضمن أن info قاموسي صالح
            if not isinstance(info, dict):
                return {}
            return {
                "id": info.get("id"),
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "webpage_url": info.get("webpage_url"),
                "filesize": info.get("filesize") or info.get("filesize_approx")
            }

    def _ydl_download_blocking(self, url: str, opts: dict) -> Any:
        """تنزيل باستخدام yt-dlp (blocking) مع external_downloader=aria2c"""
        if yt_dlp_module is None:
            raise RuntimeError("yt-dlp غير مثبت. نفّذ: pip install yt-dlp")
        try:
            with yt_dlp_module.YoutubeDL(opts) as ydl:
                return ydl.download([url])
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("yt-dlp download exception: %s\n%s", e, tb)
            raise

    def _make_progress_hook(self, job_id: str, client_id: str):
        """إنشاء progress-hook لyt-dlp يرسل progress عبر WebSocket بطريقة thread-safe"""
        def hook(d):
            try:
                status_val = d.get("status")
                if status_val == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    speed = d.get("speed") or 0
                    percent = int((downloaded / total) * 100) if total else 0
                    # تحديث الحالة المحلية
                    self.status[job_id].update({"progress": percent, "downloaded": downloaded, "total": total, "speed": speed})
                    payload = {
                        "event": "progress",
                        "job_id": job_id,
                        "status": status_val,
                        "progress": percent,
                        "downloaded": downloaded,
                        "total": total,
                        "speed": speed
                    }
                    # إرسال بشكل آمن إلى لوب الرئيسي
                    try:
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(ws_manager.send_json(client_id, payload), self.loop)
                    except Exception:
                        logger.debug("Failed to schedule WS send from hook")
                elif status_val == "finished":
                    payload = {"event": "ydl_finished", "job_id": job_id}
                    try:
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(ws_manager.send_json(client_id, payload), self.loop)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("progress hook error: %s", e)
        return hook

    # -------- Search (with simple TTL cache) --------
    async def search(self, query: str, max_results: int = 6) -> List[Dict[str, Any]]:
        """بحث بواسطة yt-dlp مع caching بسيط لتقليل طلبات متكررة."""
        q = (query or "").strip()
        if not q:
            return []
        # تحقق من الكاش
        cached = self._search_cache.get(q)
        if cached:
            result, ts = cached
            if now_ts() - ts < self._search_cache_ttl:
                return result
        # Blocking call via thread
        try:
            res = await asyncio.get_event_loop().run_in_executor(self.executor, self._search_blocking, q, max_results)
            # خزّن في الكاش
            self._search_cache[q] = (res, now_ts())
            return res
        except Exception as e:
            logger.exception("Search failed: %s", e)
            return []

    def _search_blocking(self, query: str, max_results: int = 6) -> List[Dict[str, Any]]:
        """تنفيذ البحث باستخدام yt-dlp (blocking)."""
        if yt_dlp_module is None:
            raise RuntimeError("yt-dlp not installed.")
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "default_search": f"ytsearch{max_results}",
            "noplaylist": True,
            "no_warnings": True,
            "format": "best"
        }
        if COOKIES_FILE.exists():
            ydl_opts["cookiefile"] = str(COOKIES_FILE)
        with yt_dlp_module.YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch{max_results}:{query}"
            info = ydl.extract_info(search_query, download=False)
            entries = info.get("entries", []) if isinstance(info, dict) else []
            results = []
            for e in entries:
                results.append({
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "url": e.get("url") or e.get("webpage_url"),
                    "duration": e.get("duration"),
                    "uploader": e.get("uploader"),
                    "thumbnail": e.get("thumbnail"),
                })
            return results

    # -------- Utilities: jobs/history listing --------
    def list_jobs(self) -> Dict[str, Any]:
        return self.status

    def list_history(self) -> List[Dict[str, Any]]:
        files = []
        for p in sorted(self.downloads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file():
                continue
            files.append({"file": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
        return files

# create manager instance
download_manager = DownloadManager(max_workers=3)

# ----------------------------
# FastAPI Events (startup)
# ----------------------------
@app.on_event("startup")
async def _startup():
    # تعيين لوب وبدء عامل التحميل
    try:
        download_manager.loop = asyncio.get_event_loop()
        await download_manager.start_worker()
    except Exception as e:
        logger.exception("Startup error: %s", e)

# ----------------------------
# Routes (API + WebSocket + Streaming)
# ----------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    tpl = TEMPLATES_DIR / "index.html"
    if not tpl.exists():
        return HTMLResponse("<h3>صفحة الواجهة غير موجودة. ضع ملف templates/index.html ثم أعد المحاولة.</h3>")
    return templates.TemplateResponse("index.html", {"request": request, "user": "Admin"})

@app.get("/health")
async def health():
    """حالة الخادم: CPU, RAM, queue size"""
    try:
        cpu = int(psutil.cpu_percent(interval=0.1)) if psutil else 0
        ram = int(psutil.virtual_memory().percent) if psutil else 0
    except Exception:
        cpu = 0; ram = 0
    return {"ok": True, "cpu": cpu, "ram": ram, "downloads_in_queue": download_manager.queue.qsize()}

@app.get("/api/jobs")
async def api_jobs():
    return JSONResponse(content={"jobs": download_manager.list_jobs()})

@app.get("/api/history")
async def api_history():
    return JSONResponse(content=download_manager.list_history())

@app.post("/api/download")
async def api_download(payload: dict):
    """
    بدء تنزيل: يتوقع JSON body { url, client_id }
    """
    url = (payload.get("url") or "").strip()
    client_id = (payload.get("client_id") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")
    # فحص سريع: هل هذا رابط يوتيوب أو id أو كلمة بحث
    try:
        job_id = await download_manager.enqueue(url, client_id)
        return JSONResponse(content={"ok": True, "job_id": job_id})
    except Exception as e:
        logger.exception("Failed to enqueue download: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/search")
async def api_search(payload: dict):
    """
    بحث عام: { query: "..." }
    يعيد نتائج بحث حتى 6 عناصر.
    """
    q = (payload.get("query") or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query is required")
    results = await download_manager.search(q, max_results=6)
    return JSONResponse(content={"ok": True, "query": q, "results": results})

@app.websocket("/ws/downloads/{client_id}")
async def websocket_endpoint(ws: WebSocket, client_id: str):
    """WebSocket: العميل يتصل بالـ client_id لاستقبال التحديثات."""
    await ws_manager.connect(client_id, ws)
    try:
        while True:
            try:
                msg = await ws.receive_text()
                # دعم أوامر بسيطة من العميل (مثال: {"action":"status","job_id":"..."})
                try:
                    j = json.loads(msg)
                    if isinstance(j, dict) and j.get("action") == "status" and j.get("job_id"):
                        job_state = download_manager.status.get(j.get("job_id"))
                        await ws_manager.send_json(client_id, {"event": "status_response", "job_id": j.get("job_id"), "status": job_state})
                except json.JSONDecodeError:
                    await ws_manager.send_json(client_id, {"event": "ack", "msg": msg})
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.debug("WS recv error: %s", e)
                await asyncio.sleep(0.1)
    finally:
        ws_manager.disconnect(client_id)

@app.get("/stream/{filename}")
async def stream_file(request: Request, filename: str):
    """
    بث ملف من مجلد downloads مع دعم Range header (لـ seeking).
    """
    safe = os.path.basename(filename)
    file_path = DOWNLOADS_DIR / safe
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(str(file_path), media_type="video/mp4")

    # تحليل range
    try:
        range_val = range_header.strip().lower()
        if not range_val.startswith("bytes="):
            raise ValueError("Invalid range unit")
        range_val = range_val.split("=", 1)[1]
        start_str, _, end_str = range_val.partition("-")
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Range header")

    if start >= file_size:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="Range not satisfiable")

    length = end - start + 1

    async def iterfile(path: str, start_pos: int, total_len: int):
        with open(path, "rb") as f:
            f.seek(start_pos)
            remaining = total_len
            chunk = 1024 * 1024  # 1MB
            while remaining > 0:
                read_size = min(chunk, remaining)
                data = f.read(read_size)
                if not data:
                    break
                remaining -= len(data)
                yield data
                await asyncio.sleep(0)  # yield control

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "video/mp4",
    }
    return StreamingResponse(iterfile(str(file_path), start, length), status_code=206, headers=headers)

@app.post("/api/cancel")
async def api_cancel(payload: dict):
    job_id = payload.get("job_id")
    reason = payload.get("reason", "cancelled")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id required")
    if job_id not in download_manager.status:
        raise HTTPException(status_code=404, detail="job not found")
    download_manager.status[job_id]["state"] = "cancelled"
    download_manager.status[job_id]["cancel_reason"] = reason
    await ws_manager.broadcast({"event": "cancelled", "job_id": job_id, "reason": reason})
    return {"ok": True, "job_id": job_id}

# ----------------------------
# تشغيل محلي (main)
# ----------------------------
if __name__ == "__main__":
    # اقرأ PORT من البيئة (Fly.io يمرر PORT أحيانًا)
    port = int(os.environ.get("PORT", "8080"))
    print(f"تشغيل محلي: uvicorn server:app --host 0.0.0.0 --port {port} --loop uvloop")
    try:
        import uvicorn
        # نمرّر loop=uvloop لو متاح (CLI يفعل ذلك عادة)
        uvicorn.run("server:app", host="0.0.0.0", port=port, loop="uvloop")
    except Exception:
        # fallback آمن
        try:
            uvicorn.run("server:app", host="0.0.0.0", port=port)
        except Exception as e:
            print("لا يمكن تشغيل uvicorn من هنا — تأكد من تثبيت uvicorn. خطأ:", e)
