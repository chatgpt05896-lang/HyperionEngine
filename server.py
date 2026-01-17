""" server.py Hyper-Tube X - خادم محسّن ومرتبط تمامًا بملف الواجهة templates/index.html ملاحظات سريعة:

يستخدم ملف cookies.txt تلقائيًا إذا وُجد (PATH: ./cookies.txt)

يستخدم aria2c كـ external_downloader لزيادات السرعة

يستعمل ThreadPoolExecutor لتشغيل yt-dlp (بلا حظر لحلقة الأحداث)

يُرسل تحديثات التقدّم عبر WebSocket إلى /ws/downloads/{client_id}

يدعم بث الملفات مع Range headers في /stream/{filename}

يقرأ PORT من المتغير البيئي (مناسب لـ Fly.io حيث PORT يُمرَّر أوتوماتيكيًا)


تشغيل محلي: export PORT=8080 uvicorn server:app --host 0.0.0.0 --port $PORT --loop uvloop

تأكد من تثبيت المتطلبات في requirements.txt وأن يكون aria2c وffmpeg مثبتين داخل الحاوية. """

import os import sys import time import json import logging import traceback import asyncio from typing import Dict, Any, Optional, List from pathlib import Path from concurrent.futures import ThreadPoolExecutor from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, status from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, JSONResponse from fastapi.staticfiles import StaticFiles from fastapi.middleware.cors import CORSMiddleware from fastapi.templating import Jinja2Templates

Optional imports

try: import yt_dlp as yt_dlp_module except Exception: yt_dlp_module = None

try: import psutil except Exception: psutil = None

-------------------------

Configuration

-------------------------

BASE_DIR = Path(file).resolve().parent TEMPLATES_DIR = BASE_DIR / "templates" DOWNLOADS_DIR = BASE_DIR / "downloads" COOKIES_FILE = BASE_DIR / "cookies.txt"  # المستخدم قال أنه رفعه

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

Logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s") logger = logging.getLogger("hyper-tube-x")

App

app = FastAPI(title="Hyper-Tube X") app.add_middleware( CORSMiddleware, allow_origins=[""],  # ضع هنا قائمة المجالات في الإنتاج allow_methods=[""], allow_headers=["*"], allow_credentials=True, )

Static & templates

STATIC_DIR = BASE_DIR / "static" if STATIC_DIR.exists(): app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

-------------------------

WebSocket manager

-------------------------

class WSManager: def init(self): self._conns: Dict[str, WebSocket] = {} self._locks: Dict[str, asyncio.Lock] = {}

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
    for cid in list(self._conns.keys()):
        try:
            await self.send_json(cid, payload)
        except Exception:
            pass

ws_manager = WSManager()

-------------------------

Download Manager

-------------------------

class DownloadManager: def init(self, downloads_dir: Path = DOWNLOADS_DIR, max_workers: int = 4, aria2c_args: Optional[List[str]] = None): self.downloads_dir = Path(downloads_dir) self.downloads_dir.mkdir(parents=True, exist_ok=True) self.executor = ThreadPoolExecutor(max_workers=max_workers) self.queue: asyncio.Queue = asyncio.Queue() self.status: Dict[str, Dict[str, Any]] = {} self.aria2c_args = aria2c_args or [ "--split=16", "--max-connection-per-server=8", "--min-split-size=1M", "--max-tries=5", "--retry-wait=3" ] # تخزين لوب الرئيسي لإرسال رسائل من ثريد try: self.loop = asyncio.get_event_loop() except RuntimeError: # سينشأ لاحقًا داخل uvicorn — نترك self.loop None وسيتم تعيينها عند التشغيل self.loop = None

# ابدأ عامل المعالجة بعد وجود لوب
    asyncio.get_event_loop().create_task(self._worker_loop())
    logger.info("DownloadManager initialized")

async def ensure_loop(self):
    if self.loop is None:
        self.loop = asyncio.get_event_loop()

async def enqueue(self, url: str, client_id: str) -> str:
    job_id = str(uuid4())
    job = {"id": job_id, "url": url, "client_id": client_id, "created_at": time.time()}
    self.status[job_id] = {"state": "queued", "progress": 0, "url": url, "created_at": time.time()}
    await self.queue.put(job)
    await ws_manager.send_json(client_id, {"event": "queued", "job_id": job_id, "url": url})
    logger.info(f"Enqueued {job_id} url={url} client={client_id}")
    return job_id

async def _worker_loop(self):
    logger.info("Worker loop started")
    await self.ensure_loop()
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

async def _process_job(self, job: dict):
    job_id = job["id"]
    url = job["url"]
    client_id = job["client_id"]
    logger.info(f"Processing job {job_id} url={url}")

    self.status[job_id].update({"state": "processing", "progress": 0})
    await ws_manager.send_json(client_id, {"event": "started", "job_id": job_id, "url": url})

    # 1) استخراج المعلومات
    try:
        info = await asyncio.get_event_loop().run_in_executor(self.executor, self._ydl_extract_info_blocking, url)
        if not info:
            raise RuntimeError("yt-dlp returned no info (possible blocking or cookies missing)")
        self.status[job_id]["info"] = info
    except Exception as e:
        logger.exception("Failed extract info for %s: %s", url, e)
        err = str(e)
        if "Sign in to confirm" in err or "cookie" in err.lower():
            err = "YouTube asks to sign in or cookies required. Ensure cookies.txt is up-to-date."
        self.status[job_id].update({"state": "error", "error": err})
        await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": err})
        return

    # 2) التحميل باستخدام yt-dlp + aria2c
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
    }
    if COOKIES_FILE.exists():
        ytdl_opts["cookiefile"] = str(COOKIES_FILE)
        logger.info(f"Using cookies file: {COOKIES_FILE}")

    # رأس User-Agent
    ytdl_opts["http_headers"] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    }

    try:
        await self.ensure_loop()
        result = await asyncio.get_event_loop().run_in_executor(self.executor, self._ydl_download_blocking, url, ytdl_opts)
    except Exception as e:
        msg = str(e)
        logger.exception("Download failed for %s: %s", url, msg)
        status_msg = msg[:240] if len(msg) > 240 else msg
        self.status[job_id].update({"state": "error", "error": status_msg})
        await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": status_msg})
        return

    # 3) تحديد الملف الناتج
    try:
        vid_id = info.get("id") or job_id
        found_file = None
        for p in self.downloads_dir.iterdir():
            if p.name.startswith(str(vid_id) + "."):
                found_file = p
                break
        if not found_file:
            candidates = sorted(self.downloads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
            found_file = candidates[0] if candidates else None
    except Exception:
        found_file = None

    filename = found_file.name if found_file else None
    self.status[job_id].update({"state": "finished", "progress": 100, "filename": filename, "finished_at": time.time()})
    await ws_manager.send_json(client_id, {"event": "finished", "job_id": job_id, "meta": info, "filename": filename})
    logger.info(f"Job {job_id} finished, file={filename}")

def _ydl_extract_info_blocking(self, url: str) -> dict:
    if yt_dlp_module is None:
        raise RuntimeError("yt_dlp is not installed. pip install yt-dlp")
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "format": "bestvideo+bestaudio/best"}
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp_module.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
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
    if yt_dlp_module is None:
        raise RuntimeError("yt_dlp is not installed. pip install yt-dlp")
    try:
        with yt_dlp_module.YoutubeDL(opts) as ydl:
            return ydl.download([url])
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("yt-dlp download exception: %s\n%s", e, tb)
        raise

def _make_progress_hook(self, job_id: str, client_id: str):
    def hook(d):
        try:
            status_val = d.get("status")
            if status_val == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                speed = d.get("speed") or 0
                percent = int((downloaded / total) * 100) if total else 0
                self.status[job_id].update({"progress": percent, "downloaded": downloaded, "total": total, "speed": speed})
                payload = {"event": "progress", "job_id": job_id, "status": status_val, "progress": percent, "downloaded": downloaded, "total": total, "speed": speed}
                # إرسال من ثريد إلى لوب الرئيسي بأمان
                try:
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(ws_manager.send_json(client_id, payload), self.loop)
                except Exception:
                    # لا نفشل العملية، فقط نكتب لوج
                    logger.debug("Failed to schedule ws send in progress hook")
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

def list_jobs(self) -> Dict[str, Any]:
    return self.status

def list_history(self) -> List[Dict[str, Any]]:
    files = []
    for p in sorted(self.downloads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        files.append({"file": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    return files

instantiate

download_manager = DownloadManager(max_workers=3)

-------------------------

Routes

-------------------------

@app.get("/", response_class=HTMLResponse) async def index(request: Request): tpl_path = TEMPLATES_DIR / "index.html" if not tpl_path.exists(): return HTMLResponse("<h3>صفحة الواجهة غير موجودة. ضع ملف templates/index.html ثم أعد المحاولة.</h3>") return templates.TemplateResponse("index.html", {"request": request, "user": "Admin"})

@app.get("/health") async def health(): try: if psutil: cpu = int(psutil.cpu_percent(interval=0.1)) ram = int(psutil.virtual_memory().percent) else: cpu = 0; ram = 0 except Exception: cpu = 0; ram = 0 return {"ok": True, "cpu": cpu, "ram": ram, "downloads_in_queue": download_manager.queue.qsize()}

@app.get("/api/jobs") async def api_jobs(): return JSONResponse(content={"jobs": download_manager.list_jobs()})

@app.get("/api/history") async def api_history(): return JSONResponse(content=download_manager.list_history())

@app.post("/api/download") async def api_download(payload: dict): url = payload.get("url") client_id = payload.get("client_id") if not url or not client_id: raise HTTPException(status_code=400, detail="url and client_id are required") try: job_id = await download_manager.enqueue(url, client_id) return JSONResponse(content={"ok": True, "job_id": job_id}) except Exception as e: logger.exception("enqueue failed") raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws/downloads/{client_id}") async def websocket_endpoint(ws: WebSocket, client_id: str): await ws_manager.connect(client_id, ws) try: while True: try: msg = await ws.receive_text() try: j = json.loads(msg) if j.get("action") == "status" and j.get("job_id"): job_state = download_manager.status.get(j.get("job_id")) await ws_manager.send_json(client_id, {"event": "status_response", "job_id": j.get("job_id"), "status": job_state}) except json.JSONDecodeError: await ws_manager.send_json(client_id, {"event": "ack", "msg": msg}) except WebSocketDisconnect: break except Exception as e: logger.debug("WS recv error: %s", e) await asyncio.sleep(0.1) finally: ws_manager.disconnect(client_id)

@app.get("/stream/{filename}") async def stream_file(request: Request, filename: str): safe = os.path.basename(filename) file_path = DOWNLOADS_DIR / safe if not file_path.exists(): raise HTTPException(status_code=404, detail="File not found")

file_size = file_path.stat().st_size
range_header = request.headers.get("range")
if not range_header:
    return FileResponse(str(file_path), media_type="video/mp4")

try:
    range_val = range_header.strip().lower()
    assert range_val.startswith("bytes=")
    range_val = range_val.split("=", 1)[1]
    start_str, sep, end_str = range_val.partition("-")
    start = int(start_str) if start_str else 0
    end = int(end_str) if end_str else file_size - 1
except Exception:
    raise HTTPException(status_code=400, detail="Invalid Range header")

if start >= file_size:
    raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="Range not satisfiable")

length = end - start + 1
async def iterfile(path, start_pos, total_len):
    with open(path, "rb") as f:
        f.seek(start_pos)
        remaining = total_len
        chunk = 1024 * 1024
        while remaining > 0:
            read_size = min(chunk, remaining)
            data = f.read(read_size)
            if not data:
                break
            remaining -= len(data)
            yield data
            await asyncio.sleep(0)

headers = {
    "Content-Range": f"bytes {start}-{end}/{file_size}",
    "Accept-Ranges": "bytes",
    "Content-Length": str(length),
    "Content-Type": "video/mp4",
}
return StreamingResponse(iterfile(str(file_path), start, length), status_code=206, headers=headers)

@app.post("/api/cancel") async def api_cancel(payload: dict): job_id = payload.get("job_id") reason = payload.get("reason", "cancelled") if not job_id: raise HTTPException(status_code=400, detail="job_id required") if job_id not in download_manager.status: raise HTTPException(status_code=404, detail="job not found") download_manager.status[job_id]["state"] = "cancelled" download_manager.status[job_id]["cancel_reason"] = reason await ws_manager.broadcast({"event": "cancelled", "job_id": job_id, "reason": reason}) return {"ok": True, "job_id": job_id}

if name == "main": # اقرأ PORT من المتغير البيئي (Fly.io يمرره أحيانًا) port = int(os.environ.get("PORT", "8080")) print(f"تشغيل محلي: uvicorn server:app --host 0.0.0.0 --port {port} --loop uvloop") try: import uvicorn uvicorn.run("server:app", host="0.0.0.0", port=port) except Exception: print("لا يمكن تشغيل uvicorn من هنا — تأكد من تثبيت uvicorn وافتحه من CLI.")
