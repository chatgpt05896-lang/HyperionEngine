"""
server.py
Hyper-Tube X - Minimal viable high-performance server (self-contained)

وظيفة الملف:
- يقدّم API للبحث عن فيديوهات يوتيوب باستخدام yt-dlp
- ينشئ طابور تنزيل مع تسريع عبر aria2c (كمحمّل خارجي)
- يدعم WebSockets لبث تقدم التنزيل للعميل بصورة آنية
- يوفر endpoint لبث الملفات مع دعم Range (seek)
- مكتوب ومشرح بالكامل بالعربية، مع تحسينات أداء (uvloop-compatible, asyncio, ThreadPool)

تعليمات سريعة:
- تأكد من تثبيت aria2c وffmpeg على الخادم.
- شغّل عبر: uvicorn server:app --host 0.0.0.0 --port 8000 --loop uvloop --workers 1
  (أو استخدم الإعدادات التي تفضلها على Fly.io)

ملاحظة أمنية:
- هذا كود تعليمي/بدايات لإطلاق مشروع. قبل الإنتاج، قم بتأمين CORS، المصادقة، الحد على الحجم، الحماية من إساءة الاستخدام.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import json
import time
from typing import Dict, Optional, Any, List
from concurrent.futures import ThreadPoolExecutor
from functools import wraps, lru_cache
from uuid import uuid4
from pathlib import Path
import subprocess

# Third-party
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# محاولة تحميل yt_dlp (يجب تثبيته عبر pip)
try:
    import yt_dlp as yt_dlp_module
except Exception as e:
    yt_dlp_module = None  # سنرمي استثناءً عند الاستخدام إن لم يُركّب

# إعدادات بيئية وتهيئة
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"   # افتراضيًا قد تضع الواجهة هنا
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hyper-tube-x")

# التطبيق
app = FastAPI(title="Hyper-Tube X (Minimal Server)", version="0.1")

# Cors — في الإنتاج ضيّق المصادر بدقّة
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # غيّرها للإنتاج
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# استضافة صفحات ثابتة (المستخدم سيضع HTML خارجيًا حسب رغبتك)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------
# Utilities / helpers
# ---------------------------

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def human_size(n: int) -> str:
    # تحويل بايت إلى تمثيل مقروء
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"


# ---------------------------
# Lightweight LRU caching decorator for async functions
# ---------------------------

def async_lru_cache(maxsize=128):
    """
    ديكوراتور بسيط لتخزين نتائج دوال async (LRU).
    ملاحظة: يستخدم lru_cache داخليًا على دوال متزامنة صغيرة.
    """
    def decorator(func):
        # نغلف الدالة async داخل دالة sync صغيرة لاستخدام lru_cache
        @lru_cache(maxsize=maxsize)
        def _sync_cache_wrapper(*args, **kwargs):
            # نستدعي الدالة الحديثة بشكل متزامن عن طريق حدث جديد
            loop = asyncio.get_event_loop()
            coro = func(*args, **kwargs)
            # ملاحظة: run_until_complete لا يصلح داخل حلقة حدث حالية، لذا نستخدم loop.run_until_complete
            # لكن هنا علينا استدعاء بشكل blocking — لذا نتجنب، ونستخدم futures عبر asyncio.run_coroutine_threadsafe
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result()

        @wraps(func)
        async def wrapper(*args, **kwargs):
            # نعيد تنفيذ الدالة عبر التغليف
            return _sync_cache_wrapper(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------
# WebSocket connection manager (بث التحديثات)
# ---------------------------

class WSManager:
    """
    يدير اتصالات WebSocket لمختلف عملاء الواجهة. يمكن بث رسائل بناءً على client_id.
    """
    def __init__(self):
        self._connections: Dict[str, WebSocket] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def connect(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self._connections[client_id] = websocket
        self._locks[client_id] = asyncio.Lock()
        logger.info(f"WS connected: {client_id}")

    def disconnect(self, client_id: str):
        if client_id in self._connections:
            del self._connections[client_id]
        if client_id in self._locks:
            del self._locks[client_id]
        logger.info(f"WS disconnected: {client_id}")

    async def send_json(self, client_id: str, payload: dict):
        websocket = self._connections.get(client_id)
        if not websocket:
            logger.debug(f"No websocket for client {client_id}, can't send")
            return
        # استخدام قفل لتجنّب تعارضات الارسال
        lock = self._locks.get(client_id)
        if lock:
            async with lock:
                try:
                    await websocket.send_json(payload)
                except Exception as e:
                    logger.warning(f"Failed to send WS message to {client_id}: {e}")

    async def broadcast(self, payload: dict):
        # إرسال لجميع المتصلين (احذر في الإنتاج - يمكن أن يثقل الخادم)
        for client_id in list(self._connections.keys()):
            try:
                await self.send_json(client_id, payload)
            except Exception as e:
                logger.debug(f"Broadcast fail for {client_id}: {e}")

ws_manager = WSManager()


# ---------------------------
# DownloadManager - مدير التنزيل (مضمن هنا ليكون الملف مستقل)
# ---------------------------

class DownloadManager:
    """
    DownloadManager مسؤول عن:
    - البحث عبر yt-dlp (مخزّن LRU)
    - إدارة طابور تنزيل (async queue)
    - تشغيل yt-dlp لتنزيل مبدئي، ثم دعم aria2c كمحمّل خارجي لزيادة السرعة
    - إرسال تحديثات التقدّم للعميل عبر WebSockets
    - التعامل مع أخطاء 403 وتحويل الـ user-agent/كوكيز
    """
    def __init__(self,
                 downloads_dir: Path = DOWNLOADS_DIR,
                 max_workers: int = 4,
                 aria2c_args: Optional[List[str]] = None):
        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        # threadpool للعمليات الحاجه بلوك (yt-dlp يعمل blocking)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        # طابور المهام
        self.queue: asyncio.Queue = asyncio.Queue()
        # متابعة المهام
        self._running = False
        # خياريات aria2c الافتراضية
        self.aria2c_args = aria2c_args or [
            "--split=16",
            "--max-connection-per-server=8",
            "--min-split-size=1M",
            "--max-tries=5",
            "--retry-wait=3"
        ]
        # تخزين مؤقت للبحث
        # نستخدم ديكوراتور async_lru_cache لاحقاً على search_video
        # خريطة لتخزين حالة كل تنزيل
        self.status: Dict[str, Dict[str, Any]] = {}
        # بدء عامل معالجة الطابور
        loop = asyncio.get_event_loop()
        loop.create_task(self._worker_loop())

        logger.info("DownloadManager initialized (async worker started)")

    async def _worker_loop(self):
        """
        عامل مستمر يلتقط مهام من الطابور ويعالجها.
        نفصل المنطق حتى نخلي POST /api/download لا يحظر.
        """
        if self._running:
            return
        self._running = True
        logger.info("Download worker loop started")
        while True:
            try:
                job = await self.queue.get()
                if not job:
                    await asyncio.sleep(0.1)
                    continue
                # job عبارة عن dict: {'url':.., 'client_id':.., 'id':..}
                await self._process_job(job)
            except Exception as e:
                logger.exception(f"Error in worker loop: {e}")

    async def enqueue(self, url: str, client_id: str) -> str:
        """
        يضيف مهمة للطابور ويعيد معرفها.
        """
        job_id = str(uuid4())
        job = {"id": job_id, "url": url, "client_id": client_id, "created_at": time.time()}
        # تهيئة حالة المهمة
        self.status[job_id] = {"state": "queued", "progress": 0, "info": None}
        await self.queue.put(job)
        logger.info(f"Enqueued job {job_id} for {client_id}: {url}")
        # إرسال حدث بداية للمستخدم
        await ws_manager.send_json(client_id, {"event": "queued", "job_id": job_id, "url": url})
        return job_id

    async def _process_job(self, job: dict):
        """
        تنفيذ مهمة تنزيل: يمر بخطوتين
        1) تحليل metadata بواسطة yt-dlp (غير محظور للنت)
        2) تشغيل aria2c لتحميل الملف إذا أمكن، وإبلاغ التقدّم
        """
        job_id = job["id"]
        client_id = job["client_id"]
        url = job["url"]
        logger.info(f"Processing job {job_id} for {client_id}")

        # تحديث الحالة
        self.status[job_id]["state"] = "processing"
        await ws_manager.send_json(client_id, {"event": "started", "job_id": job_id})

        # 1) جلب معلومات الميتاداتا (يمكن أن يأخذ وقت) عبر yt-dlp في ThreadPool لتجنب الحظر
        try:
            info = await asyncio.get_event_loop().run_in_executor(self.executor, self._ydl_extract_info, url)
        except Exception as e:
            logger.exception(f"Failed to extract info for {url}: {e}")
            self.status[job_id]["state"] = "error"
            self.status[job_id]["info"] = {"error": str(e)}
            await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": str(e)})
            return

        # حفظ الميتاداتا
        self.status[job_id]["info"] = info
        title_safe = "".join(c for c in (info.get("title") or "video") if c.isalnum() or c in " _-").rstrip()
        out_basename = f"{title_safe or job_id}.%(ext)s"
        out_path_template = str(self.downloads_dir / out_basename)

        # إعدادات yt-dlp لاستخراج مسار التحميل (لكن سنستخدم aria2c كمحمّل خارجي لتعجيل)
        ytdl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestvideo+bestaudio/best",
            # نحاول استخدام aria2c كـ external_downloader
            "external_downloader": "aria2c",
            "external_downloader_args": self.aria2c_args,
            "outtmpl": out_path_template,
            "writesubtitles": False,
            "noplaylist": True,
            # hook للمراقبة - لكن لاحظ: مع external_downloader، yt-dlp لا يقوم بجزء كبير من التحميل بنفسه
            "progress_hooks": [self._make_progress_hook(job_id, client_id)],
            # مرونة التعامل مع بعض حالات الحظر
            "ratelimit": None,
            "retries": 3,
            # تكوين رؤوس لتقليل فرص الحظر (يمكن تدويرها لاحقًا)
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
            },
        }

        # 2) تشغيل yt-dlp لتنزيل عن طريق aria2c (ستقوم aria2c بتحسين السرعة)
        try:
            # ننفّذ في ThreadPool لأن yt-dlp عملية blocking
            result = await asyncio.get_event_loop().run_in_executor(
                self.executor,
                self._ydl_download,
                url,
                ytdl_opts
            )
            # عند النجاح، نحفظ حالة مكتملة ونبلغ المستخدم
            self.status[job_id]["state"] = "finished"
            self.status[job_id]["progress"] = 100
            await ws_manager.send_json(client_id, {"event": "finished", "job_id": job_id, "meta": info})
            logger.info(f"Job {job_id} finished, result: {result}")
        except Exception as e:
            logger.exception(f"Download failed for job {job_id}: {e}")
            self.status[job_id]["state"] = "error"
            await ws_manager.send_json(client_id, {"event": "error", "job_id": job_id, "error": str(e)})

    def _ydl_extract_info(self, url: str) -> dict:
        """
        تشغيل yt-dlp لاستخراج بيانات الميتاداتا فقط (blocking, يُشغّل في ThreadPool).
        """
        if yt_dlp_module is None:
            raise RuntimeError("yt_dlp not installed. 'pip install yt-dlp' required.")
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "format": "bestvideo+bestaudio/best",
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            },
        }
        with yt_dlp_module.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # نظهر فقط الحقول المهمة
            return {
                "id": info.get("id"),
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "duration": info.get("duration"),
                "formats": len(info.get("formats", [])),
                "webpage_url": info.get("webpage_url"),
                "thumbnail": info.get("thumbnail"),
                "filesize": info.get("filesize") or info.get("filesize_approx")
            }

    def _ydl_download(self, url: str, opts: dict) -> dict:
        """
        تشغيل yt-dlp لتحميل الملف (blocking). نحن نمرّر 'external_downloader' = aria2c
        حتى تستفيد من التحميل متعدد الاتصالات.
        """
        if yt_dlp_module is None:
            raise RuntimeError("yt_dlp not installed. 'pip install yt-dlp' required.")

        # ملاحظة مهمة: عند استخدام external_downloader=aria2c، يجب التأكد أن aria2c مثبت ومسار الأداة في PATH.
        # yt-dlp سيتولّى إنشاء ملف input للروابط وإعطاء aria2c الأوامر اللازمة.
        try:
            with yt_dlp_module.YoutubeDL(opts) as ydl:
                result = ydl.download([url])
                return {"status": "ok", "result": result}
        except yt_dlp_module.utils.DownloadError as e:
            # محاولة كشف حالات 403 أو حظر
            msg = str(e)
            if "403" in msg or "forbidden" in msg.lower():
                # نرمي نوع مختلف ليتم التعامل معه من قبل المستدعي
                raise RuntimeError("Received 403 Forbidden from remote host (possible throttling). " + msg)
            raise

    def _make_progress_hook(self, job_id: str, client_id: str):
        """
        يُنشئ progress hook متوافق مع yt-dlp. يُرسِل التحديثات إلى الويب سوكيت الخاص بالعميل.
        ملاحظة: قد لا تأتي تحديثات كثيرة عند استخدام aria2c كـ external_downloader,
        لذلك نعتمد على إشارات الحالة النهائية أيضاً.
        """
        def hook(d):
            try:
                status = d.get("status")
                if status == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    speed = d.get("speed") or 0
                    percent = int((downloaded / total) * 100) if total else 0
                    payload = {
                        "event": "progress",
                        "job_id": job_id,
                        "status": status,
                        "progress": percent,
                        "downloaded": downloaded,
                        "total": total,
                        "speed": speed
                    }
                    # حفظ بسيط للحالة
                    self.status[job_id].update({"progress": percent})
                    # نرسل رسالة إلى الويب سوكيت (بشكل غير متزامن)
                    # ملاحظة: hook يعمل في نفس ثريد yt-dlp => نستخدم asyncio.create_task للإرسال
                    try:
                        asyncio.get_event_loop().create_task(ws_manager.send_json(client_id, payload))
                    except Exception:
                        # قد لا يكون هناك حلقة حدث في هذا الثريد؛ فنجرب إرسال في الخلفية عبر run_in_executor
                        asyncio.run(asyncio.sleep(0))  # محاولة لطيفة لتحريك الـ loop
                elif status == "finished":
                    # إخبار العميل بأن yt-dlp انتهى من كتابته إلى الملف (قد يتبع aria2c مرحلة أخرى)
                    payload = {"event": "ydl_finished", "job_id": job_id}
                    try:
                        asyncio.get_event_loop().create_task(ws_manager.send_json(client_id, payload))
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"progress hook error: {e}")
        return hook

    # -------------------------
    # Search API (مع LRU cache)
    # -------------------------
    @async_lru_cache(maxsize=256)
    async def search_video(self, query: str) -> List[Dict[str, Any]]:
        """
        دالة بحث تستخدم yt-dlp لاستخراج قائمة الفيديوهات من نتيجة البحث.
        مخزّنة بـ LRU cache لتقليل الطلبات المتكررة وتسريع الاستجابة.
        ملاحظة: هذه الدالة تُغلف استدعاء yt-dlp في ThreadPool لأنها blocking.
        """
        logger.info(f"Searching for query: {query}")
        # تنفيذ استخلاص البحث في ThreadPool
        result = await asyncio.get_event_loop().run_in_executor(self.executor, self._search_blocking, query)
        return result

    def _search_blocking(self, query: str) -> List[Dict[str, Any]]:
        """
        تنفيذ فعلي للبحث بـ yt-dlp (blocking).
        """
        if yt_dlp_module is None:
            raise RuntimeError("yt_dlp not installed (required for search).")
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": "in_playlist",  # لتحسين السرعة، لا نحتاج كل الميتاداتا
            "default_search": "ytsearch5",  # نطلب أول 5 نتائج
            "noplaylist": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (compatible; HyperTubeX/1.0)"
            }
        }
        with yt_dlp_module.YoutubeDL(ydl_opts) as ydl:
            query_string = f"ytsearch5:{query}"
            info = ydl.extract_info(query_string, download=False)
            entries = info.get("entries", [])
            results = []
            for e in entries:
                results.append({
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "url": e.get("url") or e.get("webpage_url"),
                    "duration": e.get("duration"),
                    "uploader": e.get("uploader"),
                    "thumbnail": e.get("thumbnail")
                })
            return results


# تهيئة مدير التنزيل (قابل للتعديل في حال أردت ضبط workers/aria2 args)
download_manager = DownloadManager(max_workers=4)


# ---------------------------
# FastAPI Endpoints
# ---------------------------

@app.get("/api/search")
async def api_search(q: Optional[str] = None):
    """
    GET /api/search?q=...
    يعيد JSON من نتائج البحث.
    يستخدم LRU cache داخلي لتسريع الطلبات المتكررة.
    """
    if not q:
        raise HTTPException(status_code=400, detail="query 'q' is required")
    try:
        results = await download_manager.search_video(q)
        return JSONResponse(content={"ok": True, "query": q, "results": results})
    except RuntimeError as e:
        # خطأ ناتج عن عدم وجود yt-dlp أو مشكلة بيئة
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("Search failed")
        raise HTTPException(status_code=500, detail="Search failed: " + str(e))


@app.post("/api/download")
async def api_download(payload: dict):
    """
    POST /api/download
    body JSON { "url": "<youtube-url-or-id>", "client_id": "<ws-client-id>" }
    يعيد معرف المهمة فور إدراجها في الطابور.
    """
    url = payload.get("url")
    client_id = payload.get("client_id")
    if not url or not client_id:
        raise HTTPException(status_code=400, detail="url and client_id required")
    try:
        job_id = await download_manager.enqueue(url, client_id)
        return JSONResponse(content={"ok": True, "job_id": job_id})
    except Exception as e:
        logger.exception("Failed to enqueue")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/downloads/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """
    WebSocket endpoint:
    - العميل يتصل ويستمع للتحديثات: 'queued', 'started', 'progress', 'finished', 'error'
    - العميل يمكنه إرسال رسائل نصية بسيطة (مثل ping) لكن غير مطلوب
    """
    await ws_manager.connect(client_id, websocket)
    try:
        while True:
            # نستقبل رسائل حتى يتم قطع الاتصال. الهدف الأساسي: الحفاظ على الاتصال حي.
            try:
                msg = await websocket.receive_text()
                # يمكننا تنفيذ أوامر بسيطة إن أُرسلت من العميل
                # مثل طلب حالة مهمة: {"action":"status","job_id":"..."}
                try:
                    j = json.loads(msg)
                    action = j.get("action")
                    if action == "status":
                        job_id = j.get("job_id")
                        s = download_manager.status.get(job_id)
                        await ws_manager.send_json(client_id, {"event": "status_response", "job_id": job_id, "status": s})
                except json.JSONDecodeError:
                    # رسالة نص عادي - تجاهل أو إرسال ack
                    await ws_manager.send_json(client_id, {"event": "ack", "msg": msg})
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.debug(f"Error receiving WS message: {e}")
                await asyncio.sleep(0.1)
    finally:
        ws_manager.disconnect(client_id)


@app.get("/stream/{filename}")
async def stream_file(request: Request, filename: str):
    """
    Streaming endpoint مع دعم Range headers للـ seeking.
    تحمّل الملفات من مجلد downloads.
    """
    safe_name = os.path.basename(filename)  # منع اختراق المسارات
    file_path = DOWNLOADS_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="file not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        # نعيد الملف كاملاً
        return FileResponse(str(file_path), media_type="video/mp4")

    # تحليل Range: bytes=start-end
    units, _, range_spec = range_header.partition("=")
    if units != "bytes":
        raise HTTPException(status_code=416, detail="Invalid range unit")
    start_str, _, end_str = range_spec.partition("-")
    try:
        start = int(start_str) if start_str else 0
    except:
        start = 0
    try:
        end = int(end_str) if end_str else file_size - 1
    except:
        end = file_size - 1

    if start >= file_size:
        raise HTTPException(status_code=416, detail="Range not satisfiable")

    chunk_size = 1024 * 1024  # 1MB chunks

    async def iterfile(path, start_pos, end_pos):
        with open(path, "rb") as f:
            f.seek(start_pos)
            remaining = end_pos - start_pos + 1
            while remaining > 0:
                read_size = min(remaining, chunk_size)
                data = f.read(read_size)
                if not data:
                    break
                remaining -= len(data)
                yield data
                await asyncio.sleep(0)  # yield control for concurrency

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": "video/mp4",
    }
    return StreamingResponse(iterfile(str(file_path), start, end), status_code=206, headers=headers)


# Healthcheck صغير
@app.get("/health")
async def health():
    return {"ok": True, "status": "running", "downloads_in_queue": download_manager.queue.qsize()}


# ---------------------------
# Additional Helper Endpoints (Debug / Dev)
# ---------------------------

@app.get("/api/jobs")
async def list_jobs():
    """
    يعيد حالة المهام الحالية (debug)
    """
    return JSONResponse(content={"jobs": download_manager.status})


@app.post("/api/cancel")
async def cancel_job(payload: dict):
    """
    مسح مهمة غير مفعل بالكامل (محدود) - مخصص للتجارب.
    سيغيّر حالة المهمة ويبلغ العميل، لكن قد لا يوقف aria2c الجارية.
    """
    job_id = payload.get("job_id")
    reason = payload.get("reason", "cancelled by user")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id required")
    if job_id not in download_manager.status:
        raise HTTPException(status_code=404, detail="job not found")
    download_manager.status[job_id]["state"] = "cancelled"
    download_manager.status[job_id]["cancel_reason"] = reason
    # إعلام العميل لو عرفناه
    # نحاول العثور على client_id عبر فحص الطابور (غير مثالي)
    for j in list(download_manager.queue._queue):
        if j.get("id") == job_id:
            try:
                download_manager.queue._queue.remove(j)
            except Exception:
                pass
    # إبلاغ الجميع (أو يمكن إبلاغ مالك المهمة فقط إن عرفناه)
    await ws_manager.broadcast({"event": "cancelled", "job_id": job_id, "reason": reason})
    return {"ok": True, "job_id": job_id}


# ---------------------------
# تحسينات الأداء والتركيب
# ---------------------------

# ملاحظات أداء مرفقة في التعليقات:
# - نستخدم ThreadPoolExecutor لعمليات yt-dlp لأنها متزامنة و-blocking. بهذه الطريقة الحلقة الرئيسية asyncio
#   لا تحجب الطلبات الأخرى ولا يتأثر أداء السيرفر.
# - نفضل تشغيل uvicorn مع loop = uvloop للحصول على تنفيذ أسرع لحلقة الأحداث.
# - aria2c كمحمّل خارجي يسمح بتقسيم الملف إلى أجزاء متعددة وتحميل كل جزء عبر اتصال مستقل -> زيادة الاستفادة من
#   عرض النطاق الترددي (bandwidth) وسرعة التحميل الإجمالية.
# - نستخدم StreamingResponse مع إنتاج بيانات بشكل تدريجي (chunks) و await asyncio.sleep(0) داخل المولد
#   لترك الفرصة لباقي المهام (yield control) — هذا يحسن التوازي.
# - WebSocket يجنّب الحاجة لعمليات poll من العميل (long-polling) وبالتالي يقلل الحمل على الخادم.
# - LRU cache لنتائج البحث تقلل عدد الطلبات المتكررة لخدمة البحث وتقلل خطر حظر IP أو throttling على yt.
#
# خطوات إضافية مقترحة لرفع الأداء:
# - تشغيل الخدمة ضمن container خفيف الحجم وتفعيل مكتبات شبكة محسنة (مثل tcp_tw_reuse أو tuning ل-nagle).
# - استخدام CDN لملفات الفيديو عند الحاجة، أو توجيه الطلبات لبروكسي مخصص للملفات الكبيرة.
# - تقييد السرعة الديناميكي لمستخدمين مختلفين (rate-limit) ولمنع إساءة الاستخدام.
# - نشر workers متعددة (بـ uvicorn workers او k8s pods) مع واجهة تحميل متوازنة (load balancer).
#
# أخيراً: تعامل دائم مع أخطاء 403:
# - نغير User-Agent، نستخدم كوكيز إن توفرت، ونوزّع طلبات البحث عبر cache لتقليل الاستعلامات.
# - إذا استمر 403 يجب تسجيل السبب وطلب كوكيز/بروكسي مُحترم (هذا يتجاوز قدرات السكربت لوحده).

# ---------------------------
# بدء السيرفر (لو شغّلته كملف مباشرة)
# ---------------------------
if __name__ == "__main__":
    # تشغيل كمثال للمطور: uvicorn يفضّل استخدامه من CLI (وليس عبر ال import)
    print("Run with: uvicorn server:app --host 0.0.0.0 --port 8000 --loop uvloop")
