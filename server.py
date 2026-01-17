# server.py
# Hyper-Tube X - الخادم الكامل المرتبط بواجهة templates/index.html
# كل التعليقات بالعربية لتسهيل الفهم — الملف مستقل ويحتوي على مدير التنزيلات وإدارة WebSocket وبث الملفات مع دعم Range.
#
# ملاحظة: لتشغيل بأفضل أداء استعمل:
# uvicorn server:app --host 0.0.0.0 --port 8000 --loop uvloop
#
# متطلبات (في requirements.txt):
# fastapi, uvicorn[standard], yt-dlp, jinja2, aiofiles, websockets, uvloop (مستحسن), httpx (اختياري)
#
# تذكير أمني: إذا وضعت cookies.txt في الريبو تأكد من أنه Private أو أضفه إلى .gitignore.
# ------------------------------------------------------------------------------

import os
import sys
import time
import json
import shutil
import logging
import asyncio
import traceback
from typing import Dict, Any, Optional, List
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, status, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates

# محاولة استيراد مكتبات اختيارية
try:
    import yt_dlp as yt_dlp_module
except Exception:
    yt_dlp_module = None

try:
    import psutil
except Exception:
    psutil = None  # سنعطي قيم بديلة إن لم تتوفر

# -------------------------
# إعدادات ملفات ومجلدات
# -------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_FILE = BASE_DIR / "cookies.txt"  # المستخدم رفع ملف cookies.txt في الريبو حسب كلامك

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)  # لو واجهة مش موجودة سنعالج

# -------------------------
# إعدادات اللوجينج
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hyper-tube-x")

# -------------------------
# تطبيق FastAPI و CORS
# -------------------------
app = FastAPI(title="Hyper-Tube X")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # غيّرها في الإنتاج
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# استضافة الملفات الثابتة (إن وُجد مجلد static)
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# إعداد قوالب Jinja (نستخدم templates/index.html)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# -------------------------
# WebSocket Manager
# -------------------------
class WSManager:
    """
    يدير اتصالات WebSocket حسب client_id.
    يمكن ارسال رسائل JSON لعميل محدد أو البث لجميع العملاء.
    """
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
            del self._conns[client_id]
        if client_id in self._locks:
            del self._locks[client_id]
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
        # إرسال للجميع (حذر في الإنتاج)
        for client_id in list(self._conns.keys()):
            try:
                await self.send_json(client_id, payload)
            except Exception:
                pass

ws_manager = WSManager()


# -------------------------
# DownloadManager داخلي (لكي يكون الملف مستقل)
# -------------------------
class DownloadManager:
    """
    مدير التنزيل:
    - يشغّل yt-dlp في ThreadPool لتجنب حجب حلقة الأحداث.
    - يمرّر aria2c كـ external_downloader لتسريع التحميل.
    - يرسل تحديثات التقدم عبر WebSocket باستخدام ws_manager.
    - يحفظ الحالة في self.status ويعطي endpoint للاطّلاع.
    """
    def __init__(self,
                 downloads_dir: Path = DOWNLOADS_DIR,
                 max_workers: int = 4,
                 aria2c_args: Optional[List[str]] = None):
        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.status: Dict[str, Dict[str, Any]] = {}
        self.aria2c_args = aria2c_args or [
            "--split=16",
            "--max-connection-per-server=8",
            "--min-split-size=1M",
            "--max-tries=5",
            "--retry-wait=3"
        ]
        # تشغيل حلقة العامل
        loop = asyncio.get_event_loop()
        loop.create_task(self._worker_loop())
        logger.info("DownloadManager initialized")

    async def enqueue(self, url: str, client_id: str) -> str:
        """
        يضيف مهمة إلى الطابور ويُرجع job_id
        """
        job_id = str(uuid4())
        job = {"id": job_id, "url": url, "client_id": client_id, "created_at": time.time()}
        self.status[job_id] = {"state": "queued", "progress": 0, "url": url, "created_at": time.time()}
        await self.queue.put(job)
        # إرسال إشعار للعميل
        await ws_manager.send_json(client_id, {"event": "queued", "job_id": job_id, "url": url})
        logger.info(f"Enqueued {job_id} url={url} client={client_id}")
        return job_id

    async def _worker_loop(self):
        """
        حلقة تقطّ مهام من الطابور وتنفّذها.
        """
        logger.info("Worker loop started")
        while True:
            job = await self.queue.get()
            try:
                await self._process_job(job)
            except Exception as e:
                logger.exception("Error processing job: %s", e)
            finally:
                self.queue.task_done()

    async def _process_job(self, job: dict):
        job_id = job["id"]
        url = job["url"]
        client_id = job["client_id"]
        logger.info(f"Processing job {job_id} url={url}")

        self.status[job_id].update({"state": "processing", "progress": 0})
        await ws_manager.send_json(client_id, {"event": "started", "job_id": job_id, "url": url})

        # 1) استخرج الميتاداتا باستخدام yt-dlp (blocking -> run_in_executor)
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._ydl_extract_info_blocking, url
            )
            self.status[job_id]["info"] = info
        except Exception as e:
            logger.exception("Failed extract info")
            self.status[job_id].update({"state": "error", "error": str(e)})
            await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": str(e)})
            return

        # 2) قم بتنزيل الملف (yt-dlp + external_downloader aria2c)
        # نحدد قالب اسم الملف لتسهيل التشغيل لاحقاً: نستخدم id.%(ext)s
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
            # تَضمين ملف الكوكيز إن وُجد
        }
        if COOKIES_FILE.exists():
            ytdl_opts["cookiefile"] = str(COOKIES_FILE)
            logger.info(f"Using cookies file: {COOKIES_FILE}")

        # إضافة رؤوس http لتقليل فرص الحظر
        ytdl_opts["http_headers"] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._ydl_download_blocking, url, ytdl_opts
            )
        except Exception as e:
            # حاول اكتشاف 403 لإعطاء رسالة واضحة
            msg = str(e)
            logger.exception("Download failed: %s", msg)
            status_msg = "download_error: " + (msg[:240] if len(msg) > 240 else msg)
            self.status[job_id].update({"state": "error", "error": status_msg})
            await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": status_msg})
            return

        # البحث عن الملف الذي تم تنزيله (نستند إلى id من info)
        try:
            vid_id = info.get("id") or job_id
            # البنية المتوقعة: <id>.<ext>  — يمكن أن تكون mp4/webm ...
            # نبحث عن أي ملف يبدأ بالـ id.
            found_file = None
            for p in self.downloads_dir.iterdir():
                if p.name.startswith(str(vid_id) + "."):
                    found_file = p
                    break
            if not found_file:
                # إذا لم نعثر بالاسم حاول البحث عن أحدث ملف تم تغييره
                candidates = sorted(self.downloads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
                found_file = candidates[0] if candidates else None
        except Exception:
            found_file = None

        # حدّث الحالة وأخبر العميل باسم الملف الفعلي لتشغيله لاحقًا
        if found_file:
            filename = found_file.name
        else:
            filename = None

        self.status[job_id].update({"state": "finished", "progress": 100, "filename": filename, "finished_at": time.time()})
        # أرسل حدث finished مع ميتاداتا واسم الملف الفعلي (إن وُجد)
        await ws_manager.send_json(client_id, {"event": "finished", "job_id": job_id, "meta": info, "filename": filename})
        logger.info(f"Job {job_id} finished, file={filename}")

    def _ydl_extract_info_blocking(self, url: str) -> dict:
        """
        دالة blocking تستخدم yt-dlp لاستخراج الميتاداتا فقط.
        تُشغّل في ThreadPoolExecutor.
        """
        if yt_dlp_module is None:
            raise RuntimeError("yt_dlp غير مُثبت. ثبت المكتبة عبر pip install yt-dlp")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "format": "bestvideo+bestaudio/best",
        }
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
        """
        دالة blocking تشغّل yt-dlp مع الخيارات المذكورة (external_downloader=aria2c).
        """
        if yt_dlp_module is None:
            raise RuntimeError("yt_dlp غير مُثبت. ثبت المكتبة عبر pip install yt-dlp")

        try:
            with yt_dlp_module.YoutubeDL(opts) as ydl:
                # download() يرجع 0 عند النجاح عادة، لكنه يقوم بالعمل عبر external downloader
                return ydl.download([url])
        except Exception as e:
            # إن كان الخطأ متعلق ب403 حاول إعطاء وصف أو إعادة الرمي
            tb = traceback.format_exc()
            logger.error("yt-dlp download exception: %s\n%s", e, tb)
            raise

    def _make_progress_hook(self, job_id: str, client_id: str):
        """
        يُعيد hook لyt-dlp يرسل تحديثات progress إلى العميل عن طريق WebSocket.
        يُستخدم داخل Thread في yt-dlp؛ لذلك نستخدم run_coroutine_threadsafe لإرسال رسائل بشكل آمن.
        """
        def hook(d):
            try:
                status = d.get("status")
                if status == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or d.get("downloaded_bytes") or 0
                    speed = d.get("speed") or 0
                    percent = int((downloaded / total) * 100) if total else 0
                    self.status[job_id].update({"progress": percent, "downloaded": downloaded, "total": total, "speed": speed})
                    payload = {
                        "event": "progress",
                        "job_id": job_id,
                        "status": status,
                        "progress": percent,
                        "downloaded": downloaded,
                        "total": total,
                        "speed": speed
                    }
                    # جدولة الارسال في حلقة الايفنت الرئيسية
                    try:
                        loop = asyncio.get_event_loop()
                        # قد لا تكون نفس اللوب — نستخدم run_coroutine_threadsafe للثريد
                        asyncio.run_coroutine_threadsafe(ws_manager.send_json(client_id, payload), asyncio.get_event_loop())
                    except Exception:
                        # كحل احتياطي نحاول استخدام asyncio.new_event_loop()
                        try:
                            asyncio.run(ws_manager.send_json(client_id, payload))
                        except Exception:
                            pass
                elif status == "finished":
                    # إشعار داخلي: yt-dlp انتهى من تحميل الجزء الخاص به
                    payload = {"event": "ydl_finished", "job_id": job_id}
                    try:
                        asyncio.run_coroutine_threadsafe(ws_manager.send_json(client_id, payload), asyncio.get_event_loop())
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("progress hook error: %s", e)
        return hook

    # واجهة قراءة الحالة
    def list_jobs(self) -> Dict[str, Any]:
        return self.status

    # واجهة تحقق المواقع (history عن طريق قراءة مجلد downloads)
    def list_history(self) -> List[Dict[str, Any]]:
        files = []
        for p in sorted(self.downloads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file():
                continue
            files.append({
                "file": p.name,
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            })
        return files


# تهيئة مدير التنزيل (قابل تعديل max_workers)
download_manager = DownloadManager(max_workers=3)


# -------------------------
# مسارات الـ API و WebSocket و Streaming
# -------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """
    يعيد صفحة index.html من templates (واجهة المستخدم).
    """
    tpl_path = TEMPLATES_DIR / "index.html"
    if not tpl_path.exists():
        # إن كانت الواجهة غير موجودة نُعيد رسالة بسيطة
        return HTMLResponse("<h3>صفحة الواجهة غير موجودة. ضع ملف templates/index.html ثم أعد المحاولة.</h3>")
    # نمرّر user افتراضي لو كنت تودّ
    return templates.TemplateResponse("index.html", {"request": request, "user": "Admin"})


@app.get("/health")
async def health():
    """
    نقطة فحص بسيطة — تعيد CPU و RAM إن أمكن وعدد المهام في الطابور.
    """
    try:
        if psutil:
            cpu = int(psutil.cpu_percent(interval=0.1))
            ram = int(psutil.virtual_memory().percent)
        else:
            cpu = 0
            ram = 0
    except Exception:
        cpu = 0
        ram = 0
    return {"ok": True, "cpu": cpu, "ram": ram, "downloads_in_queue": download_manager.queue.qsize()}


@app.get("/api/jobs")
async def api_jobs():
    """
    يعيد حالة المهام الحالية (خريطة job_id -> حالة)
    """
    return JSONResponse(content={"jobs": download_manager.list_jobs()})


@app.get("/api/history")
async def api_history():
    """
    يعيد لائحة الملفات في مجلد التنزيلات
    """
    return JSONResponse(content=download_manager.list_history())


@app.post("/api/download")
async def api_download(payload: dict):
    """
    يبدأ مهمة تنزيل:
    يتوقع JSON body: { "url": "<youtube url or search>", "client_id": "<client-id>" }
    """
    url = payload.get("url")
    client_id = payload.get("client_id")
    if not url or not client_id:
        raise HTTPException(status_code=400, detail="url and client_id are required")
    try:
        job_id = await download_manager.enqueue(url, client_id)
        return JSONResponse(content={"ok": True, "job_id": job_id})
    except Exception as e:
        logger.exception("enqueue failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/downloads/{client_id}")
async def websocket_endpoint(ws: WebSocket, client_id: str):
    """
    WebSocket endpoint يتصل به الواجهة للحصول على تحديثات المهام لحظيًا.
    """
    await ws_manager.connect(client_id, ws)
    try:
        while True:
            try:
                msg = await ws.receive_text()
                # إمكانية تنفيذ أوامر بسيطة من العميل (مثل طلب حالة job)
                try:
                    j = json.loads(msg)
                    if j.get("action") == "status" and j.get("job_id"):
                        job_state = download_manager.status.get(j.get("job_id"))
                        await ws_manager.send_json(client_id, {"event": "status_response", "job_id": j.get("job_id"), "status": job_state})
                except json.JSONDecodeError:
                    # رسالة نصية عادية — نرسل ACK
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
    بث الملفات من مجلد downloads مع دعم رؤوس Range للـ seeking.
    هذا يمكّن تشغيل الفيديو عبر <video> HTML5 ويدعم التقديم.
    """
    safe = os.path.basename(filename)  # تأمين المسار
    file_path = DOWNLOADS_DIR / safe
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        # إعادة الملف كاملاً
        return FileResponse(str(file_path), media_type="video/mp4")

    # تحليل رأس Range: bytes=start-end
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
        # غير قابل للتلبية
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="Range not satisfiable")

    length = end - start + 1
    # مولّد يقرأ قطع من الملف ويعيدها بشكل غير متزامن
    async def iterfile(path, start_pos, total_len):
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
                await asyncio.sleep(0)  # ترك المجال للحدثات الأخرى

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "video/mp4",
    }
    return StreamingResponse(iterfile(str(file_path), start, length), status_code=206, headers=headers)


# -------------------------
# أدوات مساعدة / إدارية
# -------------------------

@app.post("/api/cancel")
async def api_cancel(payload: dict):
    """
    محاولة إلغاء مهمة (مبدئيًا فقط - لا يضمن إيقاف aria2c الجارية).
    """
    job_id = payload.get("job_id")
    reason = payload.get("reason", "cancelled")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id required")
    if job_id not in download_manager.status:
        raise HTTPException(status_code=404, detail="job not found")
    download_manager.status[job_id]["state"] = "cancelled"
    download_manager.status[job_id]["cancel_reason"] = reason
    # إعلام جميع العملاء
    await ws_manager.broadcast({"event": "cancelled", "job_id": job_id, "reason": reason})
    return {"ok": True, "job_id": job_id}


# -------------------------
# بدء التطبيق لو شُغّل الملف مباشرة
# -------------------------
if __name__ == "__main__":
    print("تشغيل محلي: uvicorn server:app --host 0.0.0.0 --port 8000 --loop uvloop")
    try:
        import uvicorn
        uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
    except Exception:
        print("لا يمكن تشغيل uvicorn من هنا — تأكد من تثبيت uvicorn وافتحه من CLI.")
