import asyncio
import contextlib
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
import httpx
from fastapi import APIRouter, HTTPException

import core.state
from core.models import TwitchResolveRequest, TwitchDownloadRequest, Job
from core.config import DEFAULT_DOWNLOAD_DIR
from core.utils import human_size
from html.parser import HTMLParser

router = APIRouter()

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}

class _MetaFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.og_image, self.og_title, self.og_site, self.og_duration, self.og_release_date = None, None, None, None, None
    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "meta": return
        attr = {k.lower(): v for k, v in attrs}
        key = (attr.get("property") or attr.get("name") or "").lower()
        if key in ("og:image", "og:image:secure_url", "twitter:image"): self.og_image = self.og_image or attr.get("content")
        elif key in ("og:title", "twitter:title"): self.og_title = self.og_title or attr.get("content")
        elif key in ("og:site_name",): self.og_site = self.og_site or attr.get("content")
        elif key in ("video:duration", "og:video:duration"): self.og_duration = self.og_duration or attr.get("content")
        elif key in ("og:video:release_date",): self.og_release_date = self.og_release_date or attr.get("content")

def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30) -> bytes:
    response = requests.get(url, headers={**DEFAULT_HEADERS, **(headers or {})}, timeout=timeout)
    response.raise_for_status()
    return response.content

async def _http_head_ok(url: str, timeout: float = 10, retries: int = 2) -> bool:
    """Asynchronously check if URL returns OK status"""
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.head(url, headers=DEFAULT_HEADERS)
                return 200 <= response.status_code < 400
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(0.5)
                continue
            try:
                hdrs = {**DEFAULT_HEADERS, "Range": "bytes=0-1"}
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    response = await client.get(url, headers=hdrs)
                    return 200 <= response.status_code < 500
            except Exception:
                return False
    return False

def _extract_meta_from_html(html: str) -> Dict[str, Optional[str]]:
    p = _MetaFinder()
    try: p.feed(html)
    except Exception: pass
    return {"thumbnail": p.og_image, "title": p.og_title, "site": p.og_site, "duration": p.og_duration, "release_date": p.og_release_date}

def _clean_title(title: Optional[str], channel: Optional[str]) -> Optional[str]:
    if not title: return title
    t = str(title)
    sp = r"[\s\u00A0\u202F]*"
    dash = r"[–—-]"
    if channel:
        t = re.sub(rf"{sp}{dash}{sp}{re.escape(channel)}{sp}(?:na|on)?{sp}twitchu?{sp}$", "", t, flags=re.IGNORECASE)
        t = re.sub(rf"{sp}{dash}{sp}{re.escape(channel)}{sp}$", "", t, flags=re.IGNORECASE)
    t = re.sub(rf"{sp}{dash}{sp}(?:na{sp})?twitchu?{sp}$", "", t, flags=re.IGNORECASE)
    return re.sub(rf"{sp}{dash}{sp}$", "", t).strip()

def _extract_channel_from_title(title: Optional[str]) -> Optional[str]:
    if not title: return None
    m = re.search(rf"[–—-][\s\u00A0\u202F]*([^\-–—|]+?)[\s\u00A0\u202F]*(?:na|on)?[\s\u00A0\u202F]*twitchu?[\s\u00A0\u202F]*$", str(title), flags=re.IGNORECASE)
    if m: return m.group(1).strip()
    m2 = re.search(rf"[–—-][\s\u00A0\u202F]*([^\-–—|]+?)[\s\u00A0\u202F]*$", str(title))
    return m2.group(1).strip() if m2 else None

def _derive_cloudfront_from_thumb(thumb_url: str) -> Optional[str]:
    try:
        m = re.search(r"/cf_vods/([^/]+)/([^/]+)/", urlparse(thumb_url).path)
        return f"https://{m.group(1)}.cloudfront.net/{m.group(2)}/chunked/index-dvr.m3u8" if m else None
    except Exception: return None

def _extract_username_from_m3u8(m3u8_url: str) -> Optional[str]:
    """Extract username from Twitch m3u8 URL pattern: {hash}_{username}_{vod_id}_{timestamp}/chunked/..."""
    try:
        path = urlparse(m3u8_url).path
        folder = path.split('/')[1] if len(path.split('/')) > 1 else ""
        parts = folder.split('_')
        if len(parts) >= 2:
            return parts[1]
    except Exception: pass
    return None

def _extract_vod_id_from_m3u8(m3u8_url: str) -> Optional[str]:
    """Extract VOD ID from Twitch m3u8 URL pattern: {hash}_{username}_{vod_id}_{timestamp}/chunked/..."""
    try:
        path = urlparse(m3u8_url).path
        folder = path.split('/')[1] if len(path.split('/')) > 1 else ""
        parts = folder.split('_')
        if len(parts) >= 3:
            return parts[2]
    except Exception: pass
    return None

async def _extract_frame_from_m3u8_segment(m3u8_url: str, base_m3u8: str) -> Optional[str]:
    """Extract first frame from m3u8 segment as base64 thumbnail"""
    try:
        import base64, tempfile
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(m3u8_url, headers={"Accept": "application/vnd.apple.mpegurl"})
            playlist = response.text
        lines = [l.strip() for l in playlist.splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return None
        parsed_url = urlparse(base_m3u8)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{'/'.join(parsed_url.path.split('/')[:-1])}/"
        segment_url = lines[0] if lines[0].startswith("http") else base_url + lines[0]

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(segment_url)
            segment_data = response.content

        # Operacje dyskowe w wątku
        def _write_and_extract():
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp_seg:
                tmp_seg.write(segment_data)
                tmp_seg_path = tmp_seg.name

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_jpg:
                tmp_jpg_path = tmp_jpg.name

            try:
                cmd = [shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", tmp_seg_path, "-vframes", "1", "-q:v", "5", tmp_jpg_path]
                subprocess.run(cmd, capture_output=True, timeout=5, check=False)
                if Path(tmp_jpg_path).exists() and Path(tmp_jpg_path).stat().st_size > 0:
                    with open(tmp_jpg_path, "rb") as f:
                        return f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"
            finally:
                Path(tmp_seg_path).unlink(missing_ok=True)
                Path(tmp_jpg_path).unlink(missing_ok=True)
            return None

        return await asyncio.to_thread(_write_and_extract)
    except Exception: pass
    return None

async def _create_storyboard_gif(scheme: str, netloc: str, folder: str, vod_id: str) -> Optional[str]:
    """Create animated GIF from storyboard grid (5 columns, frame height ~90px)"""
    try:
        import base64, tempfile, io
        from PIL import Image

        # Pobierz grid (jeden plik ze wszystkimi klatkami)
        storyboard_url = f"{scheme}://{netloc}/{folder}/storyboards/{vod_id}-low-0.jpg"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(storyboard_url)
            if resp.status_code != 200:
                return None
            grid_data = resp.content

        # Wszystkie operacje z plikami i obrazami w osobnym wątku
        def _process_grid():
            import base64, tempfile, io
            from PIL import Image
            
            grid_img = Image.open(io.BytesIO(grid_data))
            grid_width, grid_height = grid_img.size

            # Grid ma 5 kolumn
            cols = 5
            frame_width = grid_width // cols
            frame_height = frame_width * 9 // 16  # Aspect ratio 16:9 (standardowy dla Twitcha)

            rows = (grid_height + frame_height - 1) // frame_height  # Zaokrągli w górę
            frames = []

            # Rozpakuj klatki ze gridu
            for row in range(rows):
                for col in range(cols):
                    left = col * frame_width
                    top = row * frame_height
                    right = left + frame_width
                    bottom = top + frame_height

                    # Sprawdź granice
                    if right <= grid_width and bottom <= grid_height:
                        frame = grid_img.crop((left, top, right, bottom))
                        frames.append(frame)
                    elif top < grid_height and left < grid_width:
                        # Ostatnia klatka może być obcięta
                        frame = grid_img.crop((left, top, min(right, grid_width), min(bottom, grid_height)))
                        frames.append(frame)

            if not frames:
                return None

            if len(frames) == 1:
                # Jeśli tylko jedna klatka, zwróć JPG zamiast GIF
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    frames[0].save(tmp.name, "JPEG")
                    with open(tmp.name, "rb") as f:
                        result = f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"
                    Path(tmp.name).unlink(missing_ok=True)
                    return result

            # Utwórz animowany GIF (0.5 sekundy per klatka = 500ms)
            with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp_gif:
                tmp_gif_path = tmp_gif.name

            try:
                frames[0].save(tmp_gif_path, save_all=True, append_images=frames[1:], duration=500, loop=0)
                with open(tmp_gif_path, "rb") as f:
                    return f"data:image/gif;base64,{base64.b64encode(f.read()).decode()}"
            finally:
                Path(tmp_gif_path).unlink(missing_ok=True)

        return await asyncio.to_thread(_process_grid)
    except Exception: pass
    return None

async def _get_twitch_metadata_via_gql(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        query = {
            "operationName": "VideoMetadata",
            "variables": {"videoID": video_id},
            "query": "query VideoMetadata($videoID: ID!) { video(id: $videoID) { title, creator { login }, previewThumbnailURL(width:1280,height:720), createdAt, lengthSeconds, seekPreviewsURL } }"
        }
        headers = {"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post("https://gql.twitch.tv/gql", json=[query], headers=headers)
            response.raise_for_status()
            data = response.json()[0].get("data", {}).get("video")
        if not data:
            return None

        # Parse base_m3u8 from seekPreviewsURL
        base_m3u8 = None
        seek_url = data.get("seekPreviewsURL")
        if seek_url:
            match = re.search(r"(https://[^/]+/[^/]+)", seek_url)
            if match:
                base_m3u8 = f"{match.group(1)}/chunked/index-dvr.m3u8"

        return {
            "title": data.get("title"),
            "channel": data.get("creator", {}).get("login") if data.get("creator") else None,
            "thumbnail": data.get("previewThumbnailURL"),
            "release_date": data.get("createdAt"),
            "duration": data.get("lengthSeconds"),
            "base_m3u8": base_m3u8
        }
    except Exception:
        return None

def _extract_video_id_from_url(url: str) -> Optional[str]:
    match = re.search(r"videos/(\d+)", url)
    return match.group(1) if match else None

def _parse_m3u8_duration(m3u8_text: str) -> float:
    return sum(float(line.split(":", 1)[1].split(",", 1)[0].strip()) for line in m3u8_text.splitlines() if line.startswith("#EXTINF:"))

def _estimate_size_bytes(quality: str, duration_sec: float) -> Optional[int]:
    kbps = {"1080p60": 8000, "1080p30": 5000, "720p60": 4500, "720p30": 3000, "480p30": 1500, "360p30": 800, "160p30": 250, "chunked": 8000}.get(quality)
    return int(duration_sec * (kbps * 1000 / 8)) if kbps and duration_sec > 0 else None

def _quality_order(q: str):
    m = re.match(r"(\d+)p(\d+)?", q)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)

def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\x00-\x1f\x7f]', '', re.sub(r'[<>:"/\\|?*]', '', name))[:200].strip() or "twitch_video"

@router.post("/api/twitch/resolve")
async def twitch_resolve(data: TwitchResolveRequest):
    url = data.url.strip()
    if not url: raise HTTPException(400, "URL jest wymagany")
    base_m3u8 = url if ".m3u8" in url else None
    meta = {"title": None, "channel": None, "thumbnail": None, "release_date": None, "duration": 0.0}
    duration_sec = 0.0

    if not base_m3u8:
        video_id = _extract_video_id_from_url(url)
        if video_id:
            gql_meta = await _get_twitch_metadata_via_gql(video_id)
            if gql_meta:
                base_m3u8 = gql_meta.get("base_m3u8")
                meta.update({
                    "title": gql_meta.get("title"),
                    "channel": gql_meta.get("channel"),
                    "thumbnail": gql_meta.get("thumbnail"),
                    "release_date": gql_meta.get("release_date"),
                })
                duration_sec = float(gql_meta.get("duration") or 0.0)

        if not meta.get("title"):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(url, headers=DEFAULT_HEADERS)
                    html = response.text
                meta_html = _extract_meta_from_html(html)
                # Only update if useful HTML metadata is found (prevent "Twitch" overwrites)
                if meta_html.get("title") and meta_html["title"].lower() != "twitch":
                    meta.update({k: meta_html.get(k) for k in meta if meta_html.get(k)})
            except Exception: pass

        if not base_m3u8 and meta.get("thumbnail"):
            base_m3u8 = _derive_cloudfront_from_thumb(meta["thumbnail"])

    if not base_m3u8: raise HTTPException(400, 'Nie udało się ustalić adresu M3U8')

    is_direct_m3u8 = '.m3u8' in url
    direct_m3u8_username = None
    timestamp_title = time.strftime('twitch_%Y%m%d_%H%M%S')

    if is_direct_m3u8:
        # Bezpośredni M3U8 - pobierz pierwszą klatkę z segmentu (bez animacji)
        direct_m3u8_username = _extract_username_from_m3u8(base_m3u8)
        meta['title'] = timestamp_title
        if direct_m3u8_username:
            meta['channel'] = direct_m3u8_username
        thumb = await _extract_frame_from_m3u8_segment(base_m3u8, base_m3u8)
        if thumb:
            meta['thumbnail'] = thumb
    else:
        # Twitch URL - generuj animowany GIF ze storyboards
        video_id = _extract_video_id_from_url(url)
        if video_id:
            parsed = urlparse(base_m3u8)
            path_parts = parsed.path.split('/')
            folder = path_parts[1] if len(path_parts) > 1 else ''
            gif_data = await _create_storyboard_gif(parsed.scheme, parsed.netloc, folder, video_id)
            if gif_data:
                meta['thumbnail'] = gif_data

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(base_m3u8, headers={'Accept': 'application/vnd.apple.mpegurl'})
            duration_sec = _parse_m3u8_duration(response.text)
    except Exception: pass

    qualities = ["1080p60", "1080p30", "720p60", "720p30", "480p30", "360p30", "160p30", "chunked"]
    available, seen_labels = [], set()
    for q in qualities:
        u = base_m3u8 if q == "chunked" else base_m3u8.replace("chunked", q)
        lbl = "1080p60" if q == "chunked" else q
        if lbl not in seen_labels and await _http_head_ok(u):
            seen_labels.add(lbl)
            est = _estimate_size_bytes(lbl, duration_sec)
            available.append({"label": lbl, "m3u8": u, "size_est": human_size(est) if est else "-"})

    available.sort(key=lambda x: _quality_order(x["label"]), reverse=True)

    if not meta.get("channel") and meta.get("title"): meta["channel"] = _extract_channel_from_title(meta.get("title"))
    cleaned_title = _clean_title(meta.get("title"), meta.get("channel"))
    display_title = cleaned_title

    if cleaned_title and meta.get("release_date"):
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(meta["release_date"].replace("Z", "+00:00"))
            title_cut = cleaned_title.split("|")[0].strip() if "|" in cleaned_title else cleaned_title
            display_title = f"{dt.day}_{dt.month} {title_cut}"
        except Exception: pass

    return {"base_m3u8": base_m3u8, "title": display_title, "channel": meta.get("channel"), "thumbnail": meta.get("thumbnail"), "duration": int(duration_sec), "release_date": meta.get("release_date"), "qualities": available, "username": direct_m3u8_username}

async def run_download_twitch(job_id, m3u8_url, out_dir, ext, start_sec, end_sec, title=None, release_date=None):
    print(f"[twitch] Start download job {job_id}: {m3u8_url}")
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = out_dir / f".tmp_{job_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    playlist_content = None
    try:
        print(f"[twitch] Fetching M3U8 playlist...")
        playlist_content = requests.get(m3u8_url, headers={"Accept": "application/vnd.apple.mpegurl"}, timeout=10).content.decode("utf-8", "ignore")
        parsed_url = urlparse(m3u8_url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{'/'.join(parsed_url.path.split('/')[:-1])}/"
        modified_lines = [base_url + line.strip().replace("-unmuted.ts", "-muted.ts") if line.strip() and not line.startswith("#") and line.strip().endswith(".ts") else line.rstrip('\r\n') for line in playlist_content.splitlines()]
        local_m3u8_path = temp_dir / "playlist.m3u8"
        with open(local_m3u8_path, "w", encoding="utf-8") as f: f.write("\n".join(modified_lines))
        m3u8_input = str(local_m3u8_path)
    except Exception: m3u8_input = m3u8_url

    ext = ext.lower() if ext and ext.lower() in ("mp4", "ts", "mkv", "webm") else "ts"
    print(f"[twitch] Starting ffmpeg with ext={ext}, start={start_sec}, end={end_sec}")

    # Jeśli title to timestamp "twitch_YYYYMMDD_HHMMSS" i mamy username, dodaj do nazwy pliku
    if title and _sanitize_filename(title):
        sanitized = _sanitize_filename(title)
        # Sprawdzamy czy to timestamp zaczynający się na "twitch_"
        if sanitized.startswith("twitch_") and len(sanitized) > 7:
            username = _extract_username_from_m3u8(m3u8_url)
            if username:
                fname = f"{sanitized}_{username}.{ext}"
            else:
                fname = f"{sanitized}.{ext}"
        else:
            fname = f"{sanitized}.{ext}"
    else:
        fname = time.strftime("twitch_%Y%m%d_%H%M%S") + f".{ext}"

    out_final = out_dir / fname
    base_name = out_final.stem
    counter = 1
    while out_final.exists():
        new_name = f"{base_name} ({counter}).{ext}"
        out_final = out_dir / new_name
        counter += 1

    out_tmp = str(temp_dir / out_final.name)

    ss_args = ["-ss", str(float(start_sec))] if start_sec and start_sec > 0 else []
    dur_args = []
    if end_sec and (not start_sec or end_sec > start_sec):
        dur_args = ["-t", str(float(end_sec - start_sec))] if start_sec and start_sec > 0 else ["-to", str(float(end_sec))]

    mux = {"mp4": "mp4", "ts": "mpegts", "mkv": "matroska", "webm": "webm"}.get(ext, "mp4")
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-protocol_whitelist", "file,http,https,tcp,tls,crypto"] + ss_args + ["-i", m3u8_input] + dur_args + ["-c", "copy", "-f", mux, out_tmp, "-progress", "pipe:1", "-nostats"]

    job = core.state.jobs[job_id]
    q = core.state.progress_queues.setdefault(job_id, asyncio.Queue())
    loop = asyncio.get_running_loop()

    if job.cancel:
        print(f"[twitch] Job {job_id} is cancelled before start")
        return

    done_fut = loop.create_future()
    print(f"[twitch] Starting ffmpeg process: {cmd[:5]}...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
    job.proc = proc
    print(f"[twitch] FFmpeg process started, PID={proc.pid}")

    # Determine total_dur if not fully bounded
    total_dur = float(end_sec - start_sec) if start_sec is not None and end_sec is not None and end_sec > start_sec else None
    if total_dur is None:
        try:
            if playlist_content:
                parsed_dur = _parse_m3u8_duration(playlist_content)
                if start_sec:
                    total_dur = max(0.0, parsed_dur - start_sec)
                else:
                    total_dur = parsed_dur
        except Exception:
            pass

    last_out_time_val, last_advance_mono, retrying_ui, attempts = 0.0, time.monotonic(), False, 0
    watchdog_stop = asyncio.Event()

    # Do śledzenia realnej prędkości pobierania (EMA)
    ema_speed = None
    last_calc_mono = time.monotonic()
    last_calc_out_time = 0.0

    def reader():
        nonlocal last_out_time_val, last_advance_mono, ema_speed, last_calc_mono, last_calc_out_time
        try:
            if proc.stdout is None:
                loop.call_soon_threadsafe(done_fut.set_result, proc.wait())
                return
            state = {}
            for line in proc.stdout:
                line = (line or "").strip()
                if not line or "=" not in line: continue
                k, v = line.split("=", 1)
                state[k] = v
                if k == "out_time_ms":
                    try:
                        new_time = float(v) / 1000000.0
                        if new_time > last_out_time_val + 1e-6: last_out_time_val, last_advance_mono = new_time, time.monotonic()
                    except Exception: pass
                if k == "progress" and v in ("continue", "end"):
                    percent = min(100.0, max(0.0, (last_out_time_val / total_dur) * 100.0)) if total_dur and total_dur > 0 else 0.0

                    # Exponential Moving Average (EMA) lokalnej prędkości
                    now_mono = time.monotonic()
                    dt = now_mono - last_calc_mono
                    if dt >= 0.5:
                        dp = last_out_time_val - last_calc_out_time
                        if dp >= 0:
                            inst_speed = dp / dt
                            if ema_speed is None:
                                ema_speed = inst_speed
                            else:
                                ema_speed = 0.3 * inst_speed + 0.7 * ema_speed
                        last_calc_mono = now_mono
                        last_calc_out_time = last_out_time_val

                    # Obliczanie ETA na bazie płynnej prędkości
                    if ema_speed and ema_speed > 0.001 and total_dur and total_dur > last_out_time_val:
                        eta_sec = (total_dur - last_out_time_val) / ema_speed
                        if eta_sec > 86399:
                            eta = "--:--:--"
                        else:
                            eta = time.strftime("%H:%M:%S", time.gmtime(eta_sec))
                    else:
                        eta = "--:--:--"

                    # Calculate estimated size dynamically
                    current_bytes = int(state.get("total_size", 0))
                    size_str = "?"
                    if current_bytes > 0 and last_out_time_val > 0 and total_dur and total_dur > 0:
                        est_total_bytes = current_bytes * (total_dur / last_out_time_val)
                        size_str = human_size(est_total_bytes)
                    elif current_bytes > 0:
                        size_str = human_size(current_bytes) # fallback

                    # Przechowuj ostatnie dane postępu w job dla przywracania stanu
                    job.last_progress = {
                        "percent": percent,
                        "eta": eta,
                        "size": size_str,
                        "downloaded": human_size(current_bytes),
                        "speed": state.get("speed", "Unknown")
                    }

                    loop.call_soon_threadsafe(q.put_nowait, {
                        "type": "progress",
                        "job_id": job_id,
                        "percent": percent,
                        "eta": eta,
                        "size": size_str,
                        "downloaded": human_size(current_bytes),
                        "speed": state.get("speed", "Unknown")
                    })
            loop.call_soon_threadsafe(done_fut.set_result, proc.wait())
        except Exception: loop.call_soon_threadsafe(done_fut.set_result, -1)

    async def watchdog():
        nonlocal retrying_ui, attempts
        try:
            while not watchdog_stop.is_set():
                await asyncio.sleep(1)
                if job.cancel or proc.poll() is not None: break
                if time.monotonic() - last_advance_mono >= 5:
                    if not retrying_ui:
                        retrying_ui, attempts = True, 0
                        await q.put({"type": "retrying", "job_id": job_id})
                    else:
                        attempts += 1
                        if attempts >= 30:
                            try: proc.terminate()
                            except Exception: pass
                            break
                elif retrying_ui:
                    retrying_ui, attempts = False, 0
                    await q.put({"type": "resumed", "job_id": job_id})
        finally: watchdog_stop.set()

    threading.Thread(target=reader, daemon=True).start()
    print(f"[twitch] Reader thread started")
    wd_task = asyncio.create_task(watchdog())

    print(f"[twitch] Awaiting ffmpeg completion...")
    rc = await done_fut
    print(f"[twitch] FFmpeg completed with rc={rc}")
    watchdog_stop.set()
    with contextlib.suppress(Exception): await wd_task
    job.proc = None

    if rc == 0 and total_dur and last_out_time_val > 0 and last_out_time_val < total_dur - 15.0:
        rc = -1

    if rc == 0 and not job.cancel:
        print(f"[twitch] Download successful, moving file")
        try: shutil.move(out_tmp, out_final) if Path(out_tmp).exists() else None
        except Exception: pass
        finally: shutil.rmtree(temp_dir, ignore_errors=True)
        job.done = True
        job.last_time = last_out_time_val
        await q.put({"type": "done", "job_id": job_id, "final": True, "last_time": last_out_time_val})
        print(f"[twitch] Sent 'done' message for {job_id}")
    elif job.cancel:
        print(f"[twitch] Download cancelled")
        shutil.rmtree(temp_dir, ignore_errors=True)
        job.done = True
        job.last_time = last_out_time_val
        await q.put({"type": "cancelled", "job_id": job_id, "final": True, "last_time": last_out_time_val})
    else:
        print(f"[twitch] Download error or incomplete")
        if ext == "ts" and Path(out_tmp).exists() and Path(out_tmp).stat().st_size > 0:
            try:
                shutil.move(out_tmp, out_final)
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        job.done = True
        job.error = "Download error or incomplete"
        job.last_time = last_out_time_val
        await q.put({"type": "error", "job_id": job_id, "final": True, "last_time": last_out_time_val})

@router.post("/api/twitch/download")
async def twitch_download(data: TwitchDownloadRequest):
    m3u8_url = data.m3u8_url.strip()
    if not m3u8_url: raise HTTPException(400, "m3u8_url jest wymagany")

    dl_dir = Path(data.download_dir).expanduser().resolve() if data.download_dir else DEFAULT_DOWNLOAD_DIR
    job_id = uuid.uuid4().hex
    core.state.jobs[job_id] = Job(
        id=job_id,
        platform='twitch',
        meta=data.meta if data.meta else {"url": data.original_url},
        req_data={
            "m3u8_url": m3u8_url, "download_dir": data.download_dir,
            "ext": data.ext, "start_sec": data.start_sec,
            "end_sec": data.end_sec, "title": data.title,
            "release_date": data.release_date,
            "original_url": data.original_url
        },
        info_text=data.info_text or f"{data.ext}"
    )

    if core.state.DOWNLOAD_QUEUE is None: raise HTTPException(500, "Kolejka pobierania nie została zainicjalizowana")

    q = core.state.progress_queues.setdefault(job_id, asyncio.Queue())
    await q.put({"type": "queued", "job_id": job_id})

    if core.state.DOWNLOAD_QUEUE is None: raise HTTPException(500, "Kolejka pobierania nie została zainicjalizowana")

    async def _runner():
        try:
            if not core.state.PARALLEL_DOWNLOADS and core.state.DOWNLOAD_SEMAPHORE:
                async with core.state.DOWNLOAD_SEMAPHORE:
                    await run_download_twitch(job_id, m3u8_url, dl_dir, data.ext, data.start_sec, data.end_sec, data.title, data.release_date)
            else:
                await run_download_twitch(job_id, m3u8_url, dl_dir, data.ext, data.start_sec, data.end_sec, data.title, data.release_date)
        except Exception as e:
            q = core.state.progress_queues.get(job_id)
            if q:
                await q.put({"type": "error", "job_id": job_id, "final": True, "message": str(e)})

    asyncio.create_task(_runner())
    return {"job_id": job_id}
