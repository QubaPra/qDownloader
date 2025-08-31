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
import contextlib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from jinja2 import Environment, FileSystemLoader, select_autoescape
from urllib.request import Request as UrlRequest, urlopen
from urllib.parse import quote, urlparse
from html.parser import HTMLParser

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

# ===== Twitch Schemas =====
class TwitchResolveRequest(BaseModel):
    url: str


class TwitchDownloadRequest(BaseModel):
    m3u8_url: str
    download_dir: Optional[str] = None
    ext: str = "mp4"  # mp4|ts|webm|mkv
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


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

# ======== Twitch helpers =========

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Save-Data": "on",
    "Connection": "close",
}


class _MetaFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.og_image: Optional[str] = None
        self.og_title: Optional[str] = None
        self.og_site: Optional[str] = None
        self.og_duration: Optional[str] = None

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "meta":
            return
        attr = {k.lower(): v for k, v in attrs}
        key = (attr.get("property") or attr.get("name") or "").lower()
        if key in ("og:image", "og:image:secure_url", "twitter:image"):
            self.og_image = self.og_image or attr.get("content")
        elif key in ("og:title", "twitter:title"):
            self.og_title = self.og_title or attr.get("content")
        elif key in ("og:site_name",):
            self.og_site = self.og_site or attr.get("content")
        elif key in ("video:duration", "og:video:duration"):
            self.og_duration = self.og_duration or attr.get("content")


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 20) -> bytes:
    req = UrlRequest(url, headers={**DEFAULT_HEADERS, **(headers or {})})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_head_ok(url: str, timeout: float = 10) -> bool:
    try:
        req = UrlRequest(url, headers=DEFAULT_HEADERS, method="HEAD")
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, 'status', 200) < 400
    except Exception:
        # Fallback: mały GET z Range, aby sprawdzić dostępność
        try:
            hdrs = {**DEFAULT_HEADERS, "Range": "bytes=0-1"}
            req = UrlRequest(url, headers=hdrs)
            with urlopen(req, timeout=timeout) as resp:
                return 200 <= getattr(resp, 'status', 206) < 500  # 206 Partial Content akceptowalny
        except Exception:
            return False


def _extract_meta_from_html(html: str) -> Dict[str, Optional[str]]:
    p = _MetaFinder()
    try:
        p.feed(html)
    except Exception:
        pass
    return {
        "thumbnail": p.og_image,
        "title": p.og_title,
        "site": p.og_site,
        "duration": p.og_duration,
    }


def _twitch_oembed(vod_url: str) -> Dict[str, Optional[str]]:
    oembed = f"https://www.twitch.tv/oembed?format=json&url={quote(vod_url, safe='')}"
    try:
        data = _http_get(oembed, headers={"Accept": "application/json"})
        payload = json.loads(data.decode("utf-8", errors="ignore"))
        return {
            "title": payload.get("title"),
            "channel": payload.get("author_name"),
            "thumbnail": payload.get("thumbnail_url") or payload.get("thumbnail"),
        }
    except Exception:
        return {}


def _clean_title(title: Optional[str], channel: Optional[str]) -> Optional[str]:
    """Wycina z końca tytułu fragmenty typu:
    - " – <kanał> na Twitchu"
    - " — <kanał> na Twitchu"
    - " - <kanał> na Twitchu"
    - oraz same „ – Twitch/na Twitchu”.
    Dodatkowo toleruje brak fragmentu „na Twitchu” i różne rodzaje myślników.
    """
    if not title:
        return title
    t = str(title)
    # Różne myślniki: en dash, em dash, zwykły minus
    dash = r"[–—-]"
    # Spacje, w tym NBSP i wąskie niełamiące
    sp = r"[\s\u00A0\u202F]*"
    if channel:
        # " – <kanał> (na|on)? Twitch(u)?" na końcu
        pat1 = re.compile(rf"{sp}{dash}{sp}{re.escape(channel)}{sp}(?:na|on)?{sp}twitchu?{sp}$", re.IGNORECASE)
        t = re.sub(pat1, "", t)
        # lub samo " – <kanał>" na końcu
        pat2 = re.compile(rf"{sp}{dash}{sp}{re.escape(channel)}{sp}$", re.IGNORECASE)
        t = re.sub(pat2, "", t)
    # Ogólne: " – Twitch/na Twitchu" na końcu
    pat3 = re.compile(rf"{sp}{dash}{sp}(?:na{sp})?twitchu?{sp}$", re.IGNORECASE)
    t = re.sub(pat3, "", t)
    # Ewentualne pozostałości typu " –" na samym końcu
    t = re.sub(rf"{sp}{dash}{sp}$", "", t)
    return t.strip()


def _extract_channel_from_title(title: Optional[str]) -> Optional[str]:
    """Spróbuj wyłuskać nazwę kanału z końcówki tytułu, np.:
    "… – youngmulti na Twitchu" -> "youngmulti"
    "… - youngmulti" -> "youngmulti"
    Zwraca None, jeśli nie można jednoznacznie wykryć.
    """
    if not title:
        return None
    t = str(title)
    dash = r"[–—-]"
    sp = r"[\s\u00A0\u202F]*"
    # Najpierw wariant z „na Twitchu”
    m = re.search(rf"{dash}{sp}([^\-–—|]+?){sp}(?:na|on)?{sp}twitchu?{sp}$", t, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Potem ogólny: po myślniku do końca
    m2 = re.search(rf"{dash}{sp}([^\-–—|]+?){sp}$", t)
    if m2:
        return m2.group(1).strip()
    return None


def _derive_cloudfront_from_thumb(thumb_url: str) -> Optional[str]:
    try:
        u = urlparse(thumb_url)
        m = re.search(r"/cf_vods/([^/]+)/([^/]+)/", u.path)
        if not m:
            return None
        id1, id2 = m.group(1), m.group(2)
        return f"https://{id1}.cloudfront.net/{id2}/chunked/index-dvr.m3u8"
    except Exception:
        return None


def _parse_m3u8_duration(m3u8_text: str) -> float:
    total = 0.0
    for line in m3u8_text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                # #EXTINF:4.000,
                val = line.split(":", 1)[1]
                sec = float(val.split(",", 1)[0].strip())
                total += sec
            except Exception:
                continue
    return total


def _estimate_size_bytes(quality: str, duration_sec: float) -> Optional[int]:
    # Szacunkowe bitrate (kbps) na podstawie resolution@fps
    kbps_map = {
        "1080p60": 8000,
        "1080p30": 5000,
        "720p60": 4500,
        "720p30": 3000,
        "480p30": 1500,
        "360p30": 800,
        "160p30": 250,
        "chunked": 8000,
    }
    kbps = kbps_map.get(quality)
    if kbps is None or duration_sec <= 0:
        return None
    bps = kbps * 1000
    return int(duration_sec * (bps / 8))


def _quality_order(q: str) -> tuple:
    # sortuj po wysokości i fps
    m = re.match(r"(\d+)p(\d+)?", q)
    if not m:
        return (0, 0)
    h = int(m.group(1))
    fps = int(m.group(2) or 0)
    return (h, fps)


def _build_quality_urls(chunked_url: str) -> List[Dict[str, str]]:
    qualities = [
        "1080p60", "1080p30", "720p60", "720p30",
        "480p30", "360p30", "160p30",
    ]
    out = []
    for q in qualities:
        out.append({"label": q, "url": chunked_url.replace("chunked", q)})
    # na końcu dodaj oryginalny 'chunked'
    out.append({"label": "chunked", "url": chunked_url})
    return out


def _safe_decode(b: bytes) -> str:
    return b.decode("utf-8", errors="ignore")


@app.post("/api/twitch/resolve")
async def twitch_resolve(data: TwitchResolveRequest):
    url = data.url.strip()
    if not url:
        raise HTTPException(400, "URL jest wymagany")

    # Jeśli to już jest adres m3u8 – użyj go wprost
    base_m3u8 = None
    meta: Dict[str, Optional[str]] = {"title": None, "channel": None, "thumbnail": None}
    duration_sec: float = 0.0

    if ".m3u8" in url:
        base_m3u8 = url
    else:
        # Pobierz HTML strony i meta (thumbnail/title/channel/duration)
        try:
            html = _safe_decode(_http_get(url))
            meta_html = _extract_meta_from_html(html)
            meta["thumbnail"] = meta_html.get("thumbnail") or None
            meta["title"] = meta_html.get("title") or None
            # channel z meta brak – dostarcz oEmbed
        except Exception:
            pass
        # oEmbed (title, channel, thumbnail)
        oemb = _twitch_oembed(url)
        # Preferuj oEmbed dla title/channel, bo HTML często ma sufiks "na Twitchu"
        if oemb.get("title"):
            meta["title"] = oemb.get("title")
        if oemb.get("channel"):
            meta["channel"] = oemb.get("channel")
        if oemb.get("thumbnail") and not meta.get("thumbnail"):
            meta["thumbnail"] = oemb.get("thumbnail")

        # Spróbuj zbudować m3u8 z miniatury (cf_vods)
        thumb = meta.get("thumbnail")
        if thumb:
            base_m3u8 = _derive_cloudfront_from_thumb(thumb)

    if not base_m3u8:
        raise HTTPException(400, "Nie udało się ustalić adresu M3U8 (wklej URL m3u8 lub VOD Twitch)")

    # Policz długość z listy odtwarzania (chunked)
    try:
        playlist_text = _safe_decode(_http_get(base_m3u8, headers={"Accept": "application/vnd.apple.mpegurl"}))
        duration_sec = _parse_m3u8_duration(playlist_text)
    except Exception:
        duration_sec = 0.0

    # Sprawdź dostępne jakości przez HEAD
    q_urls = _build_quality_urls(base_m3u8)
    available: List[Dict[str, Any]] = []
    seen_labels: set[str] = set()
    for item in q_urls:
        u = item["url"]
        lbl = item["label"]
        # Nie pokazuj surowego 'chunked' – relabel na 1080p60
        if lbl == "chunked":
            lbl = "1080p60"
        if _http_head_ok(u):
            if lbl in seen_labels:
                continue
            seen_labels.add(lbl)
            est = _estimate_size_bytes(lbl, duration_sec)
            available.append({
                "label": lbl,
                "m3u8": u,
                "size_est": human_size(est) if est else "-",
            })

    # Sortuj jakość od najlepszej (wysokość/fps) z 'chunked' na szczycie
    available.sort(key=lambda x: _quality_order(x["label"]), reverse=True)

    # Jeżeli kanał nadal nieznany, spróbuj wyłuskać go z tytułu
    if not meta.get("channel") and meta.get("title"):
        ch = _extract_channel_from_title(meta.get("title"))
        if ch:
            meta["channel"] = ch

    cleaned_title = _clean_title(meta.get("title"), meta.get("channel"))

    return {
        "base_m3u8": base_m3u8,
        "title": cleaned_title,
        "channel": meta.get("channel"),
        "thumbnail": meta.get("thumbnail"),
        "duration": int(duration_sec),
        "qualities": available,
    }


async def run_download_twitch(job_id: str, m3u8_url: str, out_dir: Path, ext: str, start_sec: Optional[float], end_sec: Optional[float]):
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = out_dir / f".tmp_{job_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Wyjściowy plik (bezpieczne rozszerzenia)
    ext = (ext or "mp4").lower()
    if ext not in ("mp4", "ts", "mkv", "webm"):
        ext = "mp4"

    # Nazwa wyjściowa (bez tytułu – nie mamy metadanych tutaj); użyj czasu
    fname = time.strftime("twitch_%Y%m%d_%H%M%S") + f".{ext}"
    out_tmp = str(temp_dir / fname)
    out_final = out_dir / fname

    # Oblicz -t lub -to
    ss_args: List[str] = []
    dur_args: List[str] = []
    try:
        if start_sec and start_sec > 0:
            ss_args = ["-ss", str(float(start_sec))]
        if end_sec and (not start_sec or end_sec > start_sec):
            # użyj -to jako bezwzględnego końca względem wejścia po -ss
            if start_sec and start_sec > 0:
                dur_args = ["-t", str(float(end_sec - start_sec))]
            else:
                dur_args = ["-to", str(float(end_sec))]
    except Exception:
        ss_args, dur_args = [], []

    # Format wyjściowy / muxer
    mux_map = {
        "mp4": "mp4",
        "ts": "mpegts",
        "mkv": "matroska",
        "webm": "webm",
    }
    mux = mux_map.get(ext, "mp4")

    # ffmpeg komenda (kopiowanie strumieni i retry)
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        # retry & reconnect
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_at_eof", "1",
        # próbuj ponawiać co 1 s
        "-reconnect_delay_max", "1",
        "-rw_timeout", "5000000",  # 5s w us
    ] + ss_args + [
        "-i", m3u8_url,
    ] + dur_args + [
        "-c", "copy",
        "-f", mux,
        out_tmp,
        "-progress", "pipe:1",
        "-nostats",
    ]

    print("[twitch] cmd:", " ".join(cmd))

    job = jobs[job_id]
    q = progress_queues.setdefault(job_id, asyncio.Queue())
    loop = asyncio.get_running_loop()

    # Uruchom jedną instancję ffmpeg i dołóż watchdog stanu połączenia:
    if job.cancel:
        return

    done_fut: asyncio.Future = loop.create_future()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    job.proc = proc

    # Całkowita długość na potrzeby % (na podstawie end_sec-start_sec, jeśli podano)
    total_dur = None
    if start_sec is not None and end_sec is not None and end_sec > start_sec:
        total_dur = max(0.0, float(end_sec - start_sec))

    # Wspólny stan do watchdog'a
    last_out_time_val = 0.0  # w sekundach
    last_advance_mono = time.monotonic()
    retrying_ui = False
    attempts = 0
    watchdog_stop = asyncio.Event()

    def reader():
        nonlocal last_out_time_val, last_advance_mono
        try:
            if proc.stdout is None:
                rc = proc.wait()
                loop.call_soon_threadsafe(done_fut.set_result, rc)
                return
            state: Dict[str, str] = {}
            for line in proc.stdout:
                line = (line or "").strip()
                if not line:
                    continue
                # # Debug: surowe wyjście ffmpeg (pomaga zdiagnozować problemy z reconnect)
                # try:
                #     print(f"[ffmpeg raw][{job_id}] {line}")
                # except Exception:
                #     print("[ffmpeg raw] <unable to format line>")
                if "=" not in line:
                    print("[ffmpeg]", line)
                    continue
                k, v = line.split("=", 1)
                state[k] = v
                if k == "out_time_ms":
                    try:
                        new_time = float(v) / 1_000_000.0
                        if new_time > last_out_time_val + 1e-6:
                            last_out_time_val = new_time
                            last_advance_mono = time.monotonic()
                    except Exception:
                        pass
                if k == "progress" and v in ("continue", "end"):
                    downloaded = state.get("total_size")
                    try:
                        downloaded_b = int(downloaded) if downloaded else 0
                    except Exception:
                        downloaded_b = 0
                    percent = 0.0
                    eta = "--:--:--"
                    if total_dur and total_dur > 0:
                        p = min(100.0, max(0.0, (last_out_time_val / total_dur) * 100.0))
                        percent = p
                        elapsed = last_out_time_val
                        remain = max(0.0, total_dur - elapsed)
                        eta = time.strftime("%H:%M:%S", time.gmtime(remain))
                    payload = {
                        "type": "progress" if v == "continue" else "done",
                        "job_id": job_id,
                        "percent": percent,
                        "eta": eta,
                        "size": "?",
                        "downloaded": human_size(downloaded_b) if downloaded_b else "0B",
                        "speed": state.get("speed", "Unknown"),
                    }
                    loop.call_soon_threadsafe(q.put_nowait, payload)
            rc = proc.wait()
            loop.call_soon_threadsafe(done_fut.set_result, rc)
        except Exception as e:
            print("[twitch reader] exception:", e)
            loop.call_soon_threadsafe(done_fut.set_result, -1)

    async def watchdog():
        nonlocal retrying_ui, attempts
        try:
            while not watchdog_stop.is_set():
                await asyncio.sleep(1)
                if job.cancel or proc.poll() is not None:
                    break
                since = time.monotonic() - last_advance_mono
                if since >= 5:
                    if not retrying_ui:
                        retrying_ui = True
                        attempts = 0
                        await q.put({"type": "retrying", "job_id": job_id})
                    else:
                        attempts += 1
                        if attempts >= 30:
                            # Brak postępu przez 5s + 30 prób co 1s – przerwij i pozwól ścieżce błędu zadziałać
                            try:
                                proc.terminate()
                            except Exception:
                                pass
                            break
                else:
                    # Wznowiono postęp – powrót do normalnego stanu UI
                    if retrying_ui:
                        retrying_ui = False
                        attempts = 0
                        await q.put({"type": "resumed", "job_id": job_id})
        finally:
            watchdog_stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    wd_task = asyncio.create_task(watchdog())

    rc = await done_fut
    watchdog_stop.set()
    with contextlib.suppress(Exception):
        await wd_task
    job.proc = None

    if rc == 0 and not job.cancel:
        # sukces
        try:
            if Path(out_tmp).exists():
                dest = out_final
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
                shutil.move(out_tmp, dest)
        except Exception as e:
            print("[twitch] move error:", e)
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
        await q.put({"type": "done", "job_id": job_id, "final": True})
    elif job.cancel:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
        await q.put({"type": "cancelled", "job_id": job_id, "final": True})
    else:
        # błąd po nieudanych próbach wznowienia
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
        await q.put({"type": "error", "job_id": job_id, "final": True})


@app.post("/api/twitch/download")
async def twitch_download(data: TwitchDownloadRequest):
    m3u8_url = data.m3u8_url.strip()
    if not m3u8_url:
        raise HTTPException(400, "m3u8_url jest wymagany")

    dl_dir = Path(data.download_dir).expanduser().resolve() if data.download_dir else DEFAULT_DOWNLOAD_DIR

    job_id = uuid.uuid4().hex
    jobs[job_id] = Job(id=job_id)

    if DOWNLOAD_QUEUE is None:
        raise HTTPException(500, "Kolejka pobierania nie została zainicjalizowana")

    # W tej implementacji zadanie Twitch uruchamiane jest natychmiast (nie przez yt-dlp),
    # ale wciąż używamy tej samej kolejki dla spójności.
    q = progress_queues.setdefault(job_id, asyncio.Queue())
    await q.put({"type": "queued", "job_id": job_id})

    async def _runner():
        try:
            await run_download_twitch(job_id, m3u8_url, dl_dir, data.ext, data.start_sec, data.end_sec)
        except Exception as e:
            q = progress_queues.setdefault(job_id, asyncio.Queue())
            await q.put({"type": "error", "job_id": job_id, "final": True, "message": str(e)})

    # Włóż do kolejki jako callable, by zachować sekwencyjność z YouTube (kolejka ogólna)
    class _CallableTask:
        def __init__(self, job_id: str):
            self.job_id = job_id
    # Wstawiamy do kolejki lekko-hackowo: umieszczenie DownloadTask z url=m3u8 i fmt "twitch" nie pasuje,
    # dlatego odpalimy osobny worker tu lokalnie bez kolizji z yt-dlp.
    # Prościej: uruchom bezpośrednio jako osobne zadanie (poza kolejką), ale mamy już queue dla komunikatu 'queued'.
    asyncio.create_task(_runner())

    return {"job_id": job_id}