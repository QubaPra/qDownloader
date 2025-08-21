import os
import uuid
import asyncio
import shutil
import math
from typing import Dict, List, Optional
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import yt_dlp

# ── Ścieżki ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ── Aplikacja ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Mobile Video Downloader")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ── Pamięć stanów zadań ───────────────────────────────────────────────────────
class JobState(Dict):
    pass

job_progress: Dict[str, JobState] = {}
job_flags: Dict[str, Dict[str, bool]] = {}     # {"pause": bool, "cancel": bool}
job_dirs: Dict[str, str] = {}                  # job_id -> temp dir
job_tasks: Dict[str, asyncio.Task] = {}        # job_id -> asyncio.Task

ALLOWED_VIDEO_EXTS = {"mp4", "webm", "mkv", "m3u8", "mov"}  # filtrowanie tabeli

# ── Pomocnicze ────────────────────────────────────────────────────────────────
def seconds_to_hhmmss(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"

def round_or_none(x, digits=0):
    if x is None: return None
    return round(x, digits)

def human_size(bytes_: Optional[int]) -> Optional[str]:
    if not bytes_:
        return None
    units = ["B", "KB", "MB", "GB"]
    n = float(bytes_)
    i = 0
    while n >= 1000 and i < len(units)-1:
        n /= 1000.0
        i += 1
    return f"{n:.2f} {units[i]}"

def build_progress_dict(status="queued", **kw):
    d = {
        "status": status,           # queued | starting | probing | downloading | paused | finished | done | error
        "progress": 0.0,            # %
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed": None,
        "eta": None,
        "filename": None,
        "message": None,
        "video_title": None,
        "dest_path": None,
    }
    d.update(kw)
    return d

def pick_best_audio(formats: List[dict]) -> Optional[str]:
    """
    Wybiera najlepszy audio format_id spośród dostępnych formatów audio-only.
    Zwraca format_id (str) lub None.
    Dla debugowania wypisuje ustalony id ścieżki audio.
    """
    audio_streams = []
    for f in formats:
        resolution = (f.get("resolution") or "").lower()
        if resolution == "audio only":
            audio_streams.append(f)

    if not audio_streams:
        print("[pick_best_audio] Brak dostępnych strumieni audio-only")
        return None

    # sortuj po id (str)
    audio_streams.sort(key=lambda x: x.get("format_id", ""))    
    chosen_id = str(audio_streams[0].get("format_id"))
    print(f"[pick_best_audio] Wybrano audio format_id: {chosen_id}")
    return chosen_id


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/probe")
async def api_probe(url: str = Query(..., description="URL filmu")):
    """Pobiera metadane i listę formatów przez yt-dlp (bez pobierania)."""
    try:
        ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Przefiltruj i przygotuj dane do tabeli: tylko VIDEO ONLY (jak w -F: "video only")
    print(f"{info}")
    formats = info.get("formats", [])
    cleaned = []
    for f in formats:
        ext = f.get("ext")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        # bierz tylko video-only (acodec brak/none/unknown) i z dozwolonym rozszerzeniem
        vcodec_ok = vcodec not in (None, "none")  # ma część wideo
        acodec_none = (acodec in (None, "none", "unknown"))
        if not vcodec_ok or not acodec_none:
            continue
        if ext not in ALLOWED_VIDEO_EXTS:
            continue

        height = f.get("height")
        fps = f.get("fps")
        res = f"{height or '-'}p"
        if fps:
            res += f"{int(round(fps))}"
        tbr = f.get("tbr")
        kbps = f"{int(round(tbr))} kb/s" if tbr else "-"
        cleaned.append({
            "format_id": f.get("format_id"),
            "ext": ext,
            "resolution": res,
            "width": f.get("width"),
            "height": height,
            "fps": int(round(fps)) if fps else None,
            "tbr": tbr,
            "kbps": kbps,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "acodec": acodec,
            "vcodec": vcodec,
        })

    # sort od najlepszej (wys. rozdzielczość, potem bitrate)
    cleaned.sort(key=lambda x: ((x.get("height") or 0), (x.get("tbr") or 0)), reverse=True)

    data = {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "duration": seconds_to_hhmmss(info.get("duration")),
        "all_formats_raw": info.get("formats", []),  # do wyboru audio matching
        "table_formats": cleaned
    }
    return data

@app.get("/api/list_dir")
async def api_list_dir(path: Optional[str] = None):
    """Prosty eksplorator serwerowego systemu plików (Termux)."""
    try:
        if path is None or path.strip() == "":
            # punkty startowe sugerowane na Androidzie
            roots = ["/storage", "/sdcard", "/storage/self", "/"]
            result = []
            for r in roots:
                if os.path.exists(r):
                    result.append({"name": os.path.basename(r) or r, "path": r, "is_dir": True})
            return {"cwd": "/", "entries": result}
        else:
            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail="Ścieżka nie istnieje")
            if not os.path.isdir(path):
                raise HTTPException(status_code=400, detail="To nie jest folder")
            entries = []
            # .. (parent)
            parent = os.path.abspath(os.path.join(path, os.pardir))
            if parent != path:
                entries.append({"name": "..", "path": parent, "is_dir": True})
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                try:
                    is_dir = os.path.isdir(full)
                except Exception:
                    continue
                entries.append({"name": name, "path": full, "is_dir": is_dir})
            return {"cwd": path, "entries": entries}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/start_download")
async def api_start_download(request: Request):
    """
    Start pobierania w stylu yt-dlp -F:
    - lista do pobrania pokazuje warianty 'video only'
    - przy starcie dobieramy najlepszy 'audio only' i łączymy jako "video_id+audio_id"
    - jeśli brak 'audio only', pobieramy samo wideo "video_id"
    """
    body = await request.json()
    url = body.get("url")
    video_format_id = body.get("format_id")
    # prefer_ext ignorujemy – brak wymogu zgodności rozszerzeń audio i wideo
    dest_path = body.get("dest_path")

    if not url or not video_format_id or not dest_path:
        raise HTTPException(status_code=400, detail="Brak wymaganych pól: url, format_id, dest_path")

    # Utwórz job
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOADS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    job_dirs[job_id] = job_dir
    job_flags[job_id] = {"pause": False, "cancel": False}

    # Wstępny probe, aby dobrać audio-id i tytuł
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        all_formats = info.get("formats", [])
        print(f"[start_download] probe: znaleziono {len(all_formats)} formatów dla URL={url}")
        # wybierz audio id spośród audio-only (bez dopasowywania rozszerzeń)
        audio_id = pick_best_audio(all_formats)

        if audio_id:
            format_selector = f"{video_format_id}+{audio_id}"
            print(f"[start_download] używam selector={format_selector}")
        else:
            # Brak audio-only — pobierz tylko video
            format_selector = str(video_format_id)
            print(f"[start_download] brak strumieni audio-only; pobieram tylko video format {video_format_id}")

        job_progress[job_id] = build_progress_dict(
            status="starting",
            video_title=info.get("title"),
            dest_path=dest_path,
            selected_format=format_selector
        )
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        del job_dirs[job_id]
        del job_flags[job_id]
        raise HTTPException(status_code=400, detail=f"Nie udało się przygotować pobierania: {e}")

    # Odpal pobieranie asynchronicznie
    task = asyncio.create_task(download_job(job_id, url, format_selector, dest_path))
    job_tasks[job_id] = task
    return {"job_id": job_id}

@app.post("/api/pause")
async def api_pause(request: Request):
    body = await request.json()
    job_id = body.get("job_id")
    if job_id not in job_progress:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania")
    job_flags[job_id]["pause"] = True
    return {"ok": True}

@app.post("/api/resume")
async def api_resume(request: Request):
    body = await request.json()
    job_id = body.get("job_id")
    if job_id not in job_progress:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania")
    if not job_flags[job_id]["pause"]:
        return {"ok": True}  # nic do zrobienia
    # uruchom ponownie używając tych samych parametrów
    state = job_progress[job_id]
    if state.get("status") not in ("paused", "error"):
        raise HTTPException(status_code=400, detail="Zadanie nie jest w stanie 'paused' ani 'error'")
    url = state.get("source_url")
    fmt = state.get("selected_format")
    dest_path = state.get("dest_path")
    # Nowe zadanie pobierania (kontynuacja z --continue)
    t = asyncio.create_task(download_job(job_id, url, fmt, dest_path, resume=True))
    job_tasks[job_id] = t
    return {"ok": True}

@app.post("/api/cancel")
async def api_cancel(request: Request):
    body = await request.json()
    job_id = body.get("job_id")
    if job_id not in job_progress:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania")
    job_flags[job_id]["cancel"] = True
    return {"ok": True}

@app.websocket("/ws/{job_id}")
async def ws_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        while True:
            if job_id not in job_progress:
                await websocket.send_json({"error": "job_id not found"})
            else:
                await websocket.send_json(job_progress[job_id])
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

# ── Właściwe pobieranie ──────────────────────────────────────────────────────
async def download_job(job_id: str, url: str, format_selector: str, dest_path: str, resume: bool = False):
    job_dir = job_dirs[job_id]
    outtmpl = os.path.join(job_dir, "%(title).150s.%(ext)s")

    # stan
    state = job_progress.get(job_id, build_progress_dict())
    state.update({
        "status": "starting",
        "progress": 0.0,
        "source_url": url,
        "dest_path": dest_path,
        "selected_format": format_selector
    })
    job_progress[job_id] = state

    def progress_hook(d):
        st = job_progress.get(job_id, {})
        if job_flags[job_id]["cancel"]:
            # przerwij pobieranie
            raise yt_dlp.utils.DownloadError("Zatrzymano przez użytkownika (anulowano).")
        if job_flags[job_id]["pause"]:
            raise yt_dlp.utils.DownloadError("Wstrzymano przez użytkownika.")

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100.0) if total else None
            st.update({
                "status": "downloading",
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "progress": pct or 0.0,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "filename": d.get("filename"),
                "message": None
            })
        elif status == "finished":
            st.update({
                "status": "finished",
                "progress": 100.0,
                "message": "Pobieranie zakończone. Finalizuję...",
                "filename": d.get("filename")
            })
        job_progress[job_id] = st

    ydl_opts = {
        "format": format_selector,              # np. 234+411 albo fallback
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
        "continuedl": True,                     # umożliwia wznowienie
        "concurrent_fragment_downloads": 4,     # trochę szybciej na HLS/DASH
        "merge_output_format": None,            # yt-dlp dobierze rozsądnie
    }

    loop = asyncio.get_event_loop()
    try:
        def run_ydl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        job_progress[job_id]["status"] = "downloading"
        await loop.run_in_executor(None, run_ydl)

        # przenieś wynik do dest_path
        files = [f for f in os.listdir(job_dir) if not f.startswith(".")]
        if not files:
            raise Exception("Nie znaleziono plików wyjściowych po pobraniu.")

        os.makedirs(dest_path, exist_ok=True)
        for f in files:
            src = os.path.join(job_dir, f)
            dst = os.path.join(dest_path, f)
            shutil.move(src, dst)

        job_progress[job_id].update({
            "status": "done",
            "progress": 100.0,
            "message": f"Gotowe. Zapisano do {dest_path}"
        })

        # posprzątaj po sukcesie
        shutil.rmtree(job_dir, ignore_errors=True)
        job_dirs.pop(job_id, None)

    except yt_dlp.utils.DownloadError as de:
        msg = str(de)
        if "Wstrzymano przez użytkownika" in msg:
            job_progress[job_id].update({"status": "paused", "message": "Wstrzymano. Możesz wznowić.", "progress": job_progress[job_id].get("progress", 0.0)})
            # nie czyścimy katalogu, aby można było wznowić
        elif "Zatrzymano przez użytkownika" in msg:
            job_progress[job_id].update({"status": "error", "message": "Anulowano pobieranie."})
            # Anulowanie — sprzątamy
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dirs.pop(job_id, None)
        else:
            job_progress[job_id].update({"status": "error", "message": msg})
            # pozostaw katalog, by ewentualnie wznowić
    except Exception as e:
        job_progress[job_id].update({"status": "error", "message": str(e)})
    finally:
        # wyczyść flagi pauzy (na wszelki wypadek)
        if job_id in job_flags:
            job_flags[job_id]["pause"] = False
            if job_progress.get(job_id, {}).get("status") == "done":
                job_flags[job_id]["cancel"] = False