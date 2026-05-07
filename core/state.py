import asyncio
import time
from typing import Dict, Any, List, Optional
from .models import Job, DownloadTask

jobs: Dict[str, Job] = {}
progress_queues: Dict[str, asyncio.Queue] = {}

DOWNLOAD_QUEUE: Optional[asyncio.Queue] = None
DOWNLOAD_QUEUE_LOCK: Optional[asyncio.Lock] = None
DOWNLOAD_SEMAPHORE: Optional[asyncio.Semaphore] = None
PARALLEL_DOWNLOADS: bool = True
_WORKER_STARTED = False

class MetaCacheEntry:
    def __init__(self, info: Dict[str, Any], url: str):
        self.info = info
        self.url = url
        self.ts = time.time()

META_CACHE: Dict[str, MetaCacheEntry] = {}
CACHE_TTL = 600.0  # sekundy
CACHE_MAX = 50

def meta_cache_get_by_url(url: str) -> Optional[Dict[str, Any]]:
    e = META_CACHE.get(url)
    if not e:
        return None
    if time.time() - e.ts > CACHE_TTL:
        META_CACHE.pop(url, None)
        return None
    return e.info

def meta_cache_get_by_id(vid: str) -> Optional[Dict[str, Any]]:
    e = META_CACHE.get(vid)
    if not e:
        return None
    if time.time() - e.ts > CACHE_TTL:
        META_CACHE.pop(vid, None)
        return None
    return e.info

def meta_cache_set(url: str, info: Dict[str, Any]):
    META_CACHE[url] = MetaCacheEntry(info=info, url=url)
    vid = info.get("id")
    if vid:
        META_CACHE[vid] = MetaCacheEntry(info=info, url=url)
    if len(META_CACHE) > CACHE_MAX:
        items = sorted(META_CACHE.items(), key=lambda kv: kv[1].ts)
        to_remove = max(0, len(META_CACHE) - CACHE_MAX)
        for k, _ in items[:to_remove]:
            META_CACHE.pop(k, None)

async def queue_worker(run_download_func):
    global DOWNLOAD_QUEUE
    while True:
        task: DownloadTask = await DOWNLOAD_QUEUE.get()
        job = jobs.get(task.job_id)
        if not job:
            DOWNLOAD_QUEUE.task_done()
            continue
        if job.cancel and not job.done:
            q = progress_queues.setdefault(task.job_id, asyncio.Queue())
            await q.put({"type": "cancelled", "job_id": task.job_id, "final": True})
            job.done = True
            DOWNLOAD_QUEUE.task_done()
            continue
        try:
            if PARALLEL_DOWNLOADS:
                asyncio.create_task(_run_with_cleanup(run_download_func, task, True))
            else:
                await _run_with_cleanup(run_download_func, task, True)
        except Exception:
            pass

async def _run_with_cleanup(func, task, call_task_done):
    try:
        if not PARALLEL_DOWNLOADS and DOWNLOAD_SEMAPHORE:
            async with DOWNLOAD_SEMAPHORE:
                await func(task.job_id, task.url, task.fmt_video, task.out_dir)
        else:
            await func(task.job_id, task.url, task.fmt_video, task.out_dir)
    except Exception as e:
        q = progress_queues.setdefault(task.job_id, asyncio.Queue())
        await q.put({"type": "error", "job_id": task.job_id, "final": True, "message": str(e)})
    finally:
        if call_task_done:
            DOWNLOAD_QUEUE.task_done()

async def _remove_task_from_queue(job_id: str) -> bool:
    global DOWNLOAD_QUEUE, DOWNLOAD_QUEUE_LOCK
    if DOWNLOAD_QUEUE is None:
        return False
    lock = DOWNLOAD_QUEUE_LOCK or asyncio.Lock()
    async with lock:
        removed = False
        buffer: List[DownloadTask] = []
        while True:
            try:
                item = DOWNLOAD_QUEUE.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                if isinstance(item, DownloadTask) and item.job_id == job_id:
                    removed = True
                    DOWNLOAD_QUEUE.task_done()
                else:
                    buffer.append(item)
        for it in buffer:
            await DOWNLOAD_QUEUE.put(it)
        return removed

async def startup_worker(run_download_func):
    global DOWNLOAD_QUEUE, DOWNLOAD_QUEUE_LOCK, DOWNLOAD_SEMAPHORE, _WORKER_STARTED
    if not _WORKER_STARTED:
        DOWNLOAD_QUEUE = asyncio.Queue()
        DOWNLOAD_QUEUE_LOCK = asyncio.Lock()
        DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)
        asyncio.create_task(queue_worker(run_download_func))
        _WORKER_STARTED = True
