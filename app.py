import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ===== Utils =====
ROOT = Path(__file__).parent.resolve()
DEFAULT_DOWNLOAD_DIR = ROOT / "downloads"
DEFAULT_DOWNLOAD_DIR.mkdir(exist_ok=True)

# Regexy do parsowania linii postępu yt-dlp --progress-template
PROGRESS_RE = re.compile(r"(?P<pct>\d+(?:\.\d+)?)%\s+of\s+(?P<size>[^\s]+)\s+at\s+(?P<speed>[^\s]+)\s+ETA\s+(?P<eta>[^\s]+)")
TIME_RE = re.compile(r"(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2})")


def human_size(num_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0


def parse_hms_to_seconds(text: str) -> int:
    m = TIME_RE.fullmatch(text.strip())
    if not m:
        return 0
    h = int(m.group("h") or 0)
    m_ = int(m.group("m") or 0)
    s = int(m.group("s") or 0)
    return h * 3600 + m_ * 60 + s


@dataclass
class Job:
    id: str
    proc: Optional[asyncio.subprocess.Process] = None
    created_at: float = field(default_factory=time.time)
    cancel: bool = False
    done: bool = False
    error: Optional[str] = None


jobs: Dict[str, Job] = {}

# Zadanie pobierania (do kolejki)
@dataclass
class DownloadTask:
    job_id: str
    url: str
    fmt_video: str
    out_dir: Path

# Globalna kolejka i worker (pojedynczy konsument -> sekwencyjne pobieranie)
DOWNLOAD_QUEUE: Optional[asyncio.Queue] = None
DOWNLOAD_QUEUE_LOCK: Optional[asyncio.Lock] = None
_WORKER_STARTED = False

async def queue_worker():
    assert DOWNLOAD_QUEUE is not None
    while True:
        task: DownloadTask = await DOWNLOAD_QUEUE.get()
        job = jobs.get(task.job_id)
        if not job:
            # brak zadania -> pomiń
            DOWNLOAD_QUEUE.task_done()
            continue
        # Jeśli anulowano przed startem – wyślij 'cancelled' i omiń
        if job.cancel and not job.done:
            q = progress_queues.setdefault(task.job_id, asyncio.Queue())
            await q.put({"type": "cancelled", "job_id": task.job_id, "final": True})
            job.done = True
            DOWNLOAD_QUEUE.task_done()
            continue
        try:
            await run_download(task.job_id, task.url, task.fmt_video, task.out_dir)
        except Exception as e:
            q = progress_queues.setdefault(task.job_id, asyncio.Queue())
            await q.put({"type": "error", "job_id": task.job_id, "final": True, "message": str(e)})
        finally:
            DOWNLOAD_QUEUE.task_done()

async def _remove_task_from_queue(job_id: str) -> bool:
    """Usuń zadanie o danym job_id z kolejki, jeśli jeszcze czeka. Zwraca True, jeśli usunięto."""
    if DOWNLOAD_QUEUE is None:
        return False
    # Best-effort: zablokuj i przefiltruj elementy w kolejce
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
                    # zbalansuj licznik unfinished
                    DOWNLOAD_QUEUE.task_done()
                else:
                    buffer.append(item)
        # odtwórz kolejkę w tej samej kolejności
        for it in buffer:
            await DOWNLOAD_QUEUE.put(it)
        return removed

async def _startup_worker():
    global DOWNLOAD_QUEUE, DOWNLOAD_QUEUE_LOCK, _WORKER_STARTED
    if not _WORKER_STARTED:
        DOWNLOAD_QUEUE = asyncio.Queue()
        # Lock do bezpiecznych modyfikacji kolejki (np. purge)
        DOWNLOAD_QUEUE_LOCK = asyncio.Lock()
        asyncio.create_task(queue_worker())
        _WORKER_STARTED = True

# Prosty cache metadanych w pamięci (by nie odpalać yt-dlp -J dwa razy)
@dataclass
class MetaCacheEntry:
    info: Dict[str, Any]
    url: str
    ts: float = field(default_factory=time.time)

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
    # prosta polityka wyparcia najstarszych wpisów
    if len(META_CACHE) > CACHE_MAX:
        items = sorted(META_CACHE.items(), key=lambda kv: kv[1].ts)
        to_remove = max(0, len(META_CACHE) - CACHE_MAX)
        for k, _ in items[:to_remove]:
            META_CACHE.pop(k, None)


# ===== FastAPI + Jinja =====
app = FastAPI()

# Rejestracja startowego workera kolejki
@app.on_event("startup")
async def __on_startup_register_worker():
    await _startup_worker()

# mount static
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# jinja env
jinja_env = Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)


# ===== Schemas =====
class FormatsRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str
    download_dir: Optional[str] = None


# ===== Templating =====
@app.get("/", response_class=HTMLResponse)
async def index():
    template = jinja_env.get_template("index.html")
    return template.render()


# ===== YT: list formats =====
@app.post("/api/yt/formats")
async def yt_formats(data: FormatsRequest):
    url = data.url.strip()
    if not url:
        raise HTTPException(400, "URL jest wymagany")

    print(f"[formats] Szukam formatów dla: {url}")

    # Polecenie yt-dlp -F w JSON (użyjemy -J, bo -F nie daje JSON)
    # Filtrujemy po stronie backendu tylko video-only, bez mhtml itd.
    cmd = [
        shutil.which("yt-dlp") or "yt-dlp",
        "-J",
        url,
        "--no-warnings",
        "--ignore-errors",
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(500, "Nie znaleziono yt-dlp w PATH")

    if result.returncode != 0:
        print("[formats] błąd:", (result.stderr or "")[:500])
        raise HTTPException(500, "Nie udało się pobrać metadanych")

    info = json.loads(result.stdout or "{}")
    # Zapisz metadane do cache (pod URL oraz ID)
    try:
        meta_cache_set(url, info)
    except Exception:
        pass

    # Thumbnail, title, channel, duration
    title = info.get("title")
    channel = info.get("uploader") or info.get("channel")
    duration = info.get("duration") or 0
    thumbnail = None
    thumbs = info.get("thumbnails") or []
    if thumbs:
        # weź największą
        thumbnail = sorted(thumbs, key=lambda t: t.get("width", 0) * t.get("height", 0))[-1].get("url")

    # Filtruj formaty video-only
    formats = info.get("formats") or []
    # Zbierz wpisy wraz z danymi do sortowania (height, tbr), a potem posortuj
    video_only_entries = []
    for f in formats:
        if f.get("vcodec") and f.get("acodec") in (None, "none"):
            ext = f.get("ext")
            if ext and ext.lower() not in ("mhtml",):
                # Resolution label
                height = f.get("height") or 0
                fps = f.get("fps")
                res = None
                if height:
                    res = f"{height}p{int(fps)}" if fps else f"{height}p"
                # Bitrate in kbps (approx)
                tbr = f.get("tbr") or 0  # average bitrate Kbits/s
                # Preferuj rzeczywisty rozmiar z API, a nie estymację
                size_bytes = f.get("filesize") or f.get("filesize_approx")
                size_str = human_size(size_bytes) if size_bytes else "-"
                entry = (
                    int(height) if isinstance(height, (int, float)) else 0,
                    float(tbr) if isinstance(tbr, (int, float)) else 0.0,
                    {
                        "id": f.get("format_id"),
                        "ext": ext,
                        "res": res or f.get("format_note") or "",
                        "bitrate": f"{tbr:.0f} kbps" if tbr else "-",
                        # Zachowaj klucz size_est dla zgodności w UI, ale wartość pochodzi z API
                        "size_est": size_str,
                    },
                )
                video_only_entries.append(entry)

    # Sortowanie: najpierw po rozdzielczości (height) malejąco, potem po bitrate (tbr) malejąco
    video_only_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    video_only = [e[2] for e in video_only_entries]

    data = {
        "title": title,
        "channel": channel,
        "duration": duration,
        "thumbnail": thumbnail,
        "formats": video_only,
    }
    print(f"[formats] znaleziono {len(video_only)} formatów video-only")
    return data


# ===== Progress SSE =====
async def sse_event(gen: asyncio.Queue):
    try:
        while True:
            item = await gen.get()
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("final"):
                break
    except asyncio.CancelledError:
        pass


progress_queues: Dict[str, asyncio.Queue] = {}


@app.get("/api/progress/{job_id}")
async def get_progress(job_id: str):
    q = progress_queues.get(job_id)
    if q is None:
        q = asyncio.Queue()
        progress_queues[job_id] = q
    return StreamingResponse(sse_event(q), media_type="text/event-stream")


@app.delete("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Nie znaleziono zadania")
    job.cancel = True
    # Jeśli proces już działa – przerwij
    if job.proc and job.proc.returncode is None:
        try:
            job.proc.terminate()
        except ProcessLookupError:
            pass
    else:
        # Spróbuj usunąć z kolejki, jeśli jeszcze nie wystartował
        removed = await _remove_task_from_queue(job_id)
        if removed:
            job.done = True
            q = progress_queues.setdefault(job_id, asyncio.Queue())
            await q.put({"type": "cancelled", "job_id": job_id, "final": True})
            print(f"[cancel] Usunięto z kolejki {job_id}")
            return {"status": "cancelled"}
    print(f"[cancel] Anulowano {job_id}")
    return {"status": "cancelling"}


# ===== Download =====
async def pick_best_audio(info_json: Dict[str, Any]) -> Optional[str]:
    """Wybierz najlepszą ścieżkę audio z priorytetem:
    1) oryginalna (audio_track.original/"Original"),
    2) domyślna (audioIsDefault),
    3) pozostałe,
    kryteria jakości: najpierw najwyższy ABR, przy remisie wyższy ASR; pomijaj DRC i ścieżki opisowe (audio description).
    """
    # (is_original, is_default, abr, asr, format_id)
    candidates: List[tuple] = []
    for f in info_json.get("formats", []):
        # audio-only: ma acodec i brak vcodec
        if f.get("acodec") and (f.get("vcodec") in (None, "none")):
            fmt_id = str(f.get("format_id") or "")
            note = str(f.get("format_note") or "")
            # Pomiń DRC
            if "-drc" in fmt_id.lower() or "drc" in note.lower():
                continue

            at = f.get("audio_track") or {}
            at_name = str((at.get("name") if isinstance(at, dict) else "") or note).lower()
            # Oryginalna ścieżka — heurystyki na podstawie dostępnych pól
            is_original = False
            if isinstance(at, dict):
                is_original = bool(
                    at.get("original") is True
                    or at.get("audioIsOriginal") is True
                    or at.get("audio_is_original") is True
                    or str(at.get("id") or "").lower() in ("original", "main")
                    or "original" in str(at.get("name") or "").lower()
                )
            if not is_original and "original" in note.lower():
                is_original = True

            # Domyślna ścieżka
            is_default = False
            if isinstance(at, dict):
                is_default = bool(
                    at.get("default") is True
                    or at.get("audioIsDefault") is True
                    or at.get("audio_is_default") is True
                )

            # Pomiń ścieżki opisowe (np. Audio Description)
            if any(x in at_name for x in ["description", "audio description", "descriptive", "ad "]):
                continue

            abr_val = float(f.get("abr") or 0)
            # ASR (Hz) – tie-breaker dla takich samych ABR
            asr_val = float(f.get("asr") or 0)
            if fmt_id:
                candidates.append((is_original, is_default, abr_val, asr_val, fmt_id))

    if not candidates:
        return None

    # Sortuj wg oryginalności, domyślności, potem ABR i ASR malejąco
    candidates.sort(key=lambda t: (t[0], t[1], t[2], t[3]), reverse=True)
    return candidates[0][4]


async def run_download(job_id: str, url: str, fmt_video: str, out_dir: Path):
    # Spróbuj użyć metadanych z cache; jeśli brak/po TTL, odśwież
    info = meta_cache_get_by_url(url)
    if not info:
        cmd_info = [
            shutil.which("yt-dlp") or "yt-dlp",
            "-J",
            url,
            "--no-warnings",
            "--ignore-errors",
        ]
        print("[download] Pobieram metadane do wyboru audio...")
        result_info = await asyncio.to_thread(
            subprocess.run,
            cmd_info,
            capture_output=True,
            text=True,
            check=False,
        )
        if result_info.returncode != 0:
            raise RuntimeError("Nie udało się pobrać metadanych do wyboru audio")
        info = json.loads(result_info.stdout or "{}")
        try:
            meta_cache_set(url, info)
        except Exception:
            pass
    audio_id = await pick_best_audio(info)
    print(f"[download] video={fmt_video}, audio={audio_id}")

    out_dir.mkdir(parents=True, exist_ok=True)
    # Tymczasowy katalog na częściowe pliki tego zadania.
    temp_dir = out_dir / f".tmp_{job_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Progress template pozwala kontrolować standardowe logi w 1 linii
    # Używamy JSON do łatwiejszego parsowania (custom template)
    # Uwaga: wartości z yt-dlp (np. _percent_str, _eta_str) to stringi – muszą być w cudzysłowach,
    # w przeciwnym razie wyjście nie będzie poprawnym JSON-em.
    progress_tpl = (
        "{"
        "\"status\":\"downloading\"," 
        "\"pct\":\"%(progress._percent_str)s\"," 
        "\"_eta\":\"%(progress._eta_str)s\"," 
        "\"speed\":\"%(progress._speed_str)s\"," 
        "\"size\":\"%(progress._total_bytes_str)s\"," 
        "\"downloaded\":\"%(progress._downloaded_bytes_str)s\""
        "}"
    )

    # Budujemy format selektor dla yt-dlp: preferuj połączony muxer (video+audio)
    # ale my wymuszamy konkretny video i najlepsze audio
    fmt_selector = fmt_video
    if audio_id:
        fmt_selector = f"{fmt_video}+{audio_id}"

    # Zapisuj do tymczasowego katalogu; finalny plik przeniesiemy po sukcesie
    out_tpl = str(temp_dir / "%(title)s [%(id)s].%(ext)s")

    cmd = [
        shutil.which("yt-dlp") or "yt-dlp",
        "-f", fmt_selector,
        url,
        "-o", out_tpl,
        "--no-warnings",
        "--ignore-errors",
        "--retries", "30",
        "--fragment-retries", "30",
        "--retry-sleep", "1",
        "--socket-timeout", "4",
        "--progress-template", f"download:{progress_tpl}",
        "--newline",
    ]

    print("[download] cmd:", " ".join(cmd))

    job = jobs[job_id]
    # Uruchom proces w trybie blokującym i czytaj wyjście w wątku
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    job.proc = proc

    q = progress_queues.setdefault(job_id, asyncio.Queue())
    loop = asyncio.get_running_loop()
    done_fut: asyncio.Future = loop.create_future()

    def reader():
        try:
            if proc.stdout is None:
                rc = proc.wait()
                loop.call_soon_threadsafe(done_fut.set_result, rc)
                return
            retrying_flag = False
            for line in proc.stdout:
                txt = (line or "").strip()
                payload = None
                try:
                    payload = json.loads(txt)
                except Exception:
                    payload = None
                # Wykrywanie ponawiania/retry na podstawie logów tekstowych yt-dlp
                if payload is None and txt:
                    low = txt.lower()
                    if ("retry" in low or "retrying" in low):
                        if not retrying_flag:
                            retrying_flag = True
                            loop.call_soon_threadsafe(q.put_nowait, {"type": "retrying", "job_id": job_id, "message": txt})
                if payload and payload.get("status") == "downloading":
                    # Jeśli wcześniej był retry, wyślij zdarzenie wznowienia
                    if retrying_flag:
                        retrying_flag = False
                        loop.call_soon_threadsafe(q.put_nowait, {"type": "resumed", "job_id": job_id})
                    pct_raw = payload.get("pct", "0%")
                    try:
                        pct_val = float(pct_raw.replace("%", "").strip() or 0.0)
                    except Exception:
                        pct_val = 0.0
                    eta_str = payload.get("_eta", "--:--:--")
                    size_str = payload.get("size", "0B")
                    downloaded_str = payload.get("downloaded", "0B")
                    speed_str = payload.get("speed", "0B/s")
                    loop.call_soon_threadsafe(
                        q.put_nowait,
                        {
                            "type": "progress",
                            "job_id": job_id,
                            "percent": pct_val,
                            "eta": eta_str,
                            "size": size_str,
                            "downloaded": downloaded_str,
                            "speed": speed_str,
                        },
                    )
                else:
                    if txt:
                        print("[yt-dlp]", txt)
            rc = proc.wait()
            # Final event
            def finalize():
                job.done = True
                if rc == 0 and not job.cancel:
                    # Przenieś wygenerowane pliki z temp do docelowego katalogu
                    try:
                        for p in temp_dir.iterdir():
                            if p.is_file():
                                dest = out_dir / p.name
                                if dest.exists():
                                    base = dest.stem
                                    suf = dest.suffix
                                    i = 1
                                    while True:
                                        cand = out_dir / f"{base} ({i}){suf}"
                                        if not cand.exists():
                                            dest = cand
                                            break
                                        i += 1
                                shutil.move(str(p), str(dest))
                    except Exception as move_err:
                        print(f"[download] Błąd przenoszenia plików: {move_err}")
                    finally:
                        try:
                            shutil.rmtree(temp_dir, ignore_errors=True)
                        except Exception:
                            pass
                    q.put_nowait({"type": "done", "job_id": job_id, "final": True})
                    print(f"[download] Zakończono {job_id}")
                elif job.cancel:
                    # Usuń wszelkie pobrane fragmenty
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    q.put_nowait({"type": "cancelled", "job_id": job_id, "final": True})
                    print(f"[download] Anulowano {job_id}")
                else:
                    # Błąd – wyczyść fragmenty
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    q.put_nowait({"type": "error", "job_id": job_id, "final": True})
                    print(f"[download] Błąd {job_id}")
                done_fut.set_result(rc)
            loop.call_soon_threadsafe(finalize)
        except Exception as e:
            print("[reader] exception:", e)
            loop.call_soon_threadsafe(done_fut.set_result, -1)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        rc = await done_fut
    finally:
        job.proc = None


@app.post("/api/yt/download")
async def yt_download(data: DownloadRequest):
    url = data.url.strip()
    fmt_id = data.format_id.strip()
    if not url or not fmt_id:
        raise HTTPException(400, "URL i format_id są wymagane")

    dl_dir = Path(data.download_dir).expanduser().resolve() if data.download_dir else DEFAULT_DOWNLOAD_DIR

    job_id = uuid.uuid4().hex
    jobs[job_id] = Job(id=job_id)

    print(f"[download] start job={job_id}, url={url}, fmt={fmt_id}, dir={dl_dir}")

    # Dodaj do kolejki zamiast uruchamiać równolegle
    if DOWNLOAD_QUEUE is None:
        raise HTTPException(500, "Kolejka pobierania nie została zainicjalizowana")

    # Wyślij stan 'queued' do SSE (opcjonalne, przydatne w UI)
    q = progress_queues.setdefault(job_id, asyncio.Queue())
    await q.put({"type": "queued", "job_id": job_id})

    await DOWNLOAD_QUEUE.put(DownloadTask(job_id=job_id, url=url, fmt_video=fmt_id, out_dir=dl_dir))

    return {"job_id": job_id}


# ===== Static HTML =====
