import asyncio
import json
import shutil
import subprocess
import threading
import uuid
import time
from pathlib import Path
from typing import Any, Dict, Optional, List

from fastapi import APIRouter, HTTPException

import core.state
from core.models import FormatsRequest, DownloadRequest, Job, DownloadTask
from core.config import DEFAULT_DOWNLOAD_DIR
from core.utils import human_size

router = APIRouter()

@router.post("/api/yt/formats")
async def yt_formats(data: FormatsRequest):
    url = data.url.strip()
    if not url:
        raise HTTPException(400, "URL jest wymagany")

    print(f"[formats] Szukam formatów dla: {url}")
    cmd = [
        shutil.which("yt-dlp") or "yt-dlp",
        "-J", url, "--no-warnings", "--ignore-errors",
    ]

    info = {}
    for attempt in range(2):
        try:
            result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            raise HTTPException(500, "Nie znaleziono yt-dlp w PATH")

        try:
            info = json.loads(result.stdout or "{}")
        except Exception:
            info = None

        if not isinstance(info, dict) or not info:
            if attempt == 0:
                continue
            raise HTTPException(400, f"Nie udało się pobrać danych z URL-a. Sprawdź czy link jest prawidłowy.")

        formats = info.get("formats") or []
        has_video = False
        for f in formats:
            if f.get("vcodec") and f.get("vcodec") != "none" and f.get("acodec") in (None, "none"):
                has_video = True
                break

        if has_video:
            break
        elif attempt == 0:
            print("[formats] Brak powiązanych formatów wideo, ponawiam próbę dla SABR auth...")
            await asyncio.sleep(1)

    if not info:
        print("[formats] błąd, pusta odpowiedź yt-dlp")
        raise HTTPException(500, "Nie udało się pobrać metadanych")

    try:
        core.state.meta_cache_set(url, info)
    except Exception: pass

    title = info.get("title")
    channel = info.get("uploader") or info.get("channel")
    duration = info.get("duration") or 0
    thumbnail = None
    try:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumbnail = sorted(thumbs, key=lambda t: t.get("width", 0) * t.get("height", 0))[-1].get("url")
    except Exception as e:
        print(f"[formats] Błąd podczas pobierania miniatury: {e}")

    formats = info.get("formats") or []
    video_only_entries = []

    best_audio_info = await pick_best_audio(info)
    audio_ext = best_audio_info[1] if best_audio_info else None

    for f in formats:
        if f.get("vcodec") and f.get("vcodec") != "none" and f.get("acodec") in (None, "none"):
            ext = f.get("ext")
            if ext and ext.lower() not in ("mhtml",):
                height = f.get("height") or 0
                fps = f.get("fps")
                res = f"{height}p{int(fps)}" if fps else f"{height}p" if height else None
                tbr = f.get("tbr") or 0
                size_bytes = f.get("filesize") or f.get("filesize_approx")
                size_str = human_size(size_bytes) if size_bytes else "-"

                real_ext = ext
                if audio_ext and ext:
                    if ext == audio_ext:
                        real_ext = ext
                    elif ext == "mp4" and audio_ext == "m4a":
                        real_ext = "mp4"
                    elif ext == "webm" and audio_ext == "webm":
                        real_ext = "webm"
                    else:
                        real_ext = "mkv"

                entry = (
                    int(height) if isinstance(height, (int, float)) else 0,
                    float(tbr) if isinstance(tbr, (int, float)) else 0.0,
                    {
                        "id": f.get("format_id"),
                        "ext": real_ext,
                        "res": res or f.get("format_note") or "",
                        "bitrate": f"{tbr:.0f} kbps" if tbr else "-",
                        "size_est": size_str,
                    },
                )
                video_only_entries.append(entry)

    video_only_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    video_only = [e[2] for e in video_only_entries]

    return {
        "title": title,
        "channel": channel,
        "duration": duration,
        "thumbnail": thumbnail,
        "formats": video_only,
    }


async def pick_best_audio(info_json: Dict[str, Any]) -> Optional[tuple[str, str]]:
    candidates: List[tuple] = []
    for f in info_json.get("formats", []):
        if f.get("acodec") and (f.get("vcodec") in (None, "none") and f.get("acodec") != "none"):
            fmt_id = str(f.get("format_id") or "")
            ext = str(f.get("ext") or "")
            note = str(f.get("format_note") or "")
            if "-drc" in fmt_id.lower() or "drc" in note.lower(): continue

            at = f.get("audio_track") or {}
            at_name = str((at.get("name") if isinstance(at, dict) else "") or note).lower()
            is_original = False
            if isinstance(at, dict):
                is_original = bool(
                    at.get("original") is True or at.get("audioIsOriginal") is True
                    or at.get("audio_is_original") is True
                    or str(at.get("id") or "").lower() in ("original", "main")
                    or "original" in str(at.get("name") or "").lower()
                )
            if not is_original and "original" in note.lower(): is_original = True

            is_default = False
            if isinstance(at, dict):
                is_default = bool(at.get("default") is True or at.get("audioIsDefault") is True or at.get("audio_is_default") is True)

            if any(x in at_name for x in ["description", "audio description", "descriptive", "ad "]): continue

            abr_val = float(f.get("abr") or 0)
            asr_val = float(f.get("asr") or 0)
            if fmt_id:
                candidates.append((is_original, is_default, abr_val, asr_val, fmt_id, ext))

    if not candidates: return None
    candidates.sort(key=lambda t: (t[0], t[1], t[2], t[3]), reverse=True)
    return candidates[0][4], candidates[0][5]

async def run_download(job_id: str, url: str, fmt_video: str, out_dir: Path):
    info = core.state.meta_cache_get_by_url(url)
    if not info:
        cmd_info = [shutil.which("yt-dlp") or "yt-dlp", "-J", url, "--no-warnings", "--ignore-errors"]
        result_info = await asyncio.to_thread(subprocess.run, cmd_info, capture_output=True, text=True, check=False)
        if result_info.returncode != 0: raise RuntimeError("Nie udało się pobrać metadanych do wyboru audio")
        info = json.loads(result_info.stdout or "{}")
        try: core.state.meta_cache_set(url, info)
        except Exception: pass

    best_audio_info = await pick_best_audio(info)
    audio_id = best_audio_info[0] if best_audio_info else None
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = out_dir / f".tmp_{job_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    progress_tpl = (
        "{"
        "\"status\":\"downloading\","
        "\"pct\":\"%(progress._percent_str)s\","
        "\"raw_eta\":%(progress.eta)s,"
        "\"speed\":\"%(progress._speed_str)s\","
        "\"size\":\"%(progress._total_bytes_str)s\","
        "\"downloaded\":\"%(progress._downloaded_bytes_str)s\","
        "\"downloaded_bytes\":%(progress.downloaded_bytes|0)s,"
        "\"total_bytes\":%(progress.total_bytes|0)s"
        "}"
    )

    fmt_selector = f"{fmt_video}+{audio_id}" if audio_id else fmt_video
    out_tpl = str(temp_dir / "%(title)s [%(id)s].%(ext)s")

    cmd = [
        shutil.which("yt-dlp") or "yt-dlp", "-f", fmt_selector, url, "-o", out_tpl,
        "--no-warnings", "--ignore-errors", "--retries", "30", "--fragment-retries", "30",
        "--retry-sleep", "1", "--socket-timeout", "4", "--progress-template", f"download:{progress_tpl}", "--newline"
    ]

    job = core.state.jobs[job_id]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
    job.proc = proc

    q = core.state.progress_queues.setdefault(job_id, asyncio.Queue())
    loop = asyncio.get_running_loop()
    done_fut: asyncio.Future = loop.create_future()

    def reader():
        ema_speed = None
        last_calc_mono = time.monotonic()
        last_downloaded_bytes = 0.0

        try:
            if proc.stdout is None:
                rc = proc.wait()
                loop.call_soon_threadsafe(done_fut.set_result, rc)
                return
            retrying_flag = False
            for line in proc.stdout:
                txt = (line or "").strip()
                payload = None
                if txt.startswith("download:{"):
                    txt = txt[len("download:"):]
                try: payload = json.loads(txt)
                except Exception: pass
                if payload is None and txt:
                    low = txt.lower()
                    if "error" in low or "failed" in low:
                        print(f"[youtube] yt-dlp błąd: {txt}")
                    if ("retry" in low or "retrying" in low):
                        if not retrying_flag:
                            retrying_flag = True
                            loop.call_soon_threadsafe(q.put_nowait, {"type": "retrying", "job_id": job_id, "message": txt})
                if payload and payload.get("status") == "downloading":
                    if retrying_flag:
                        retrying_flag = False
                        loop.call_soon_threadsafe(q.put_nowait, {"type": "resumed", "job_id": job_id})

                    pct_raw = payload.get("pct", "0%")
                    try: pct_val = float(pct_raw.replace("%", "").strip() or 0.0)
                    except Exception: pct_val = 0.0

                    raw_dl = payload.get("downloaded_bytes")
                    total_bytes = payload.get("total_bytes")
                    dl_bytes = float(raw_dl) if isinstance(raw_dl, (int, float)) else 0.0
                    if not isinstance(total_bytes, (int, float)) and pct_val > 0:
                        total_bytes = dl_bytes / (pct_val / 100.0)
                    elif not isinstance(total_bytes, (int, float)):
                        total_bytes = 0.0

                    now_mono = time.monotonic()
                    dt = now_mono - last_calc_mono
                    if dt >= 0.5 and dl_bytes > 0:
                        dp = dl_bytes - last_downloaded_bytes
                        if dp >= 0 and last_downloaded_bytes > 0:
                            inst_speed = dp / dt
                            if ema_speed is None: ema_speed = inst_speed
                            else: ema_speed = 0.3 * inst_speed + 0.7 * ema_speed
                        last_calc_mono = now_mono
                        last_downloaded_bytes = dl_bytes
                    elif last_downloaded_bytes == 0 and dl_bytes > 0:
                        last_downloaded_bytes = dl_bytes
                        last_calc_mono = now_mono

                    if ema_speed and ema_speed > 0.001 and total_bytes > dl_bytes:
                        eta_sec = (total_bytes - dl_bytes) / ema_speed
                        if eta_sec > 86399: eta = "--:--:--"
                        else: eta = time.strftime("%H:%M:%S", time.gmtime(eta_sec))
                    else:
                        eta = "--:--:--"

                    size_str = payload.get("size", "0B").replace("MiB", "MB").replace("GiB", "GB").replace("KiB", "KB")
                    dl_str = payload.get("downloaded", "0B").replace("MiB", "MB").replace("GiB", "GB").replace("KiB", "KB")
                    speed_str = payload.get("speed", "0B/s").replace("MiB/s", "MB/s").replace("GiB/s", "GB/s").replace("KiB/s", "KB/s")

                    # Przechowuj ostatnie dane postępu w job dla przywracania stanu
                    job.last_progress = {
                        "percent": pct_val,
                        "eta": eta,
                        "size": size_str,
                        "downloaded": dl_str,
                        "speed": speed_str,
                    }

                    loop.call_soon_threadsafe(q.put_nowait, {
                        "type": "progress", "job_id": job_id, "percent": pct_val,
                        "eta": eta, "size": size_str,
                        "downloaded": dl_str, "speed": speed_str,
                    })
            rc = proc.wait()
            def finalize():
                job.done = True
                if rc == 0 and not job.cancel:
                    try:
                        for p in temp_dir.iterdir():
                            if p.is_file():
                                dest = out_dir / p.name
                                if dest.exists():
                                    base, suf = dest.stem, dest.suffix
                                    i = 1
                                    while True:
                                        cand = out_dir / f"{base} ({i}){suf}"
                                        if not cand.exists():
                                            dest = cand
                                            break
                                        i += 1
                                shutil.move(str(p), str(dest))
                    except Exception as move_err: print(f"[download] Błąd: {move_err}")
                    finally:
                        try: shutil.rmtree(temp_dir, ignore_errors=True)
                        except Exception: pass
                    job.last_time = 0.0  # YouTube nie wspiera resuming
                    q.put_nowait({"type": "done", "job_id": job_id, "final": True})
                elif job.cancel:
                    try: shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception: pass
                    job.last_time = 0.0
                    q.put_nowait({"type": "cancelled", "job_id": job_id, "final": True})
                else:
                    print(f"[youtube] Zakończono z błędem (kod {rc}) dla zadania {job_id}")
                    try: shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception: pass
                    job.error = "Download failed or incomplete"
                    job.last_time = 0.0  # YouTube nie wspiera resuming
                    q.put_nowait({"type": "error", "job_id": job_id, "final": True})
                done_fut.set_result(rc)
            loop.call_soon_threadsafe(finalize)
        except Exception as e:
            print(f"[youtube] Wyjątek w wątku pobierania: {e}")
            loop.call_soon_threadsafe(done_fut.set_result, -1)

    threading.Thread(target=reader, daemon=True).start()
    try: rc = await done_fut
    finally: job.proc = None

@router.post("/api/yt/download")
async def yt_download(data: DownloadRequest):
    url = data.url.strip()
    fmt_id = data.format_id.strip()
    if not url or not fmt_id: raise HTTPException(400, "URL i format_id są wymagane")

    dl_dir = Path(data.download_dir).expanduser().resolve() if data.download_dir else DEFAULT_DOWNLOAD_DIR
    job_id = uuid.uuid4().hex

    core.state.jobs[job_id] = Job(
        id=job_id,
        platform='youtube',
        meta=data.meta if data.meta else {"url": url},
        req_data={"url": url, "format_id": fmt_id, "download_dir": data.download_dir},
        info_text=data.info_text or fmt_id
    )

    if core.state.DOWNLOAD_QUEUE is None: raise HTTPException(500, "Kolejka pobierania nierozpoczęta")

    q = core.state.progress_queues.setdefault(job_id, asyncio.Queue())
    await q.put({"type": "queued", "job_id": job_id})
    await core.state.DOWNLOAD_QUEUE.put(DownloadTask(job_id=job_id, url=url, fmt_video=fmt_id, out_dir=dl_dir))

    return {"job_id": job_id}
