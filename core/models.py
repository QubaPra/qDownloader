import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from pydantic import BaseModel
from pathlib import Path

@dataclass
class Job:
    id: str
    platform: Optional[str] = None  # 'youtube' lub 'twitch'
    meta: Optional[Dict[str, Any]] = None # meta karty, np. z formatów
    req_data: Optional[Dict[str, Any]] = None # parametry requestu by móc wznowić
    info_text: Optional[str] = None # tekst widoczny na pasku, np. '1080p60 .ts'
    proc: Optional[asyncio.subprocess.Process] = None
    created_at: float = field(default_factory=time.time)
    cancel: bool = False
    done: bool = False
    error: Optional[str] = None
    last_progress: Optional[Dict[str, Any]] = None # ostatnie dane postępu (downloaded, size, speed)
    last_time: float = 0.0 # ile sekund pobierano zanim error (dla Twitch resume)

@dataclass
class DownloadTask:
    job_id: str
    url: str
    fmt_video: str
    out_dir: Path

class FormatsRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    download_dir: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    info_text: Optional[str] = None

class TwitchResolveRequest(BaseModel):
    url: str

class TwitchDownloadRequest(BaseModel):
    m3u8_url: str
    download_dir: Optional[str] = None
    ext: str = "ts"
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None
    title: Optional[str] = None
    release_date: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    info_text: Optional[str] = None
    original_url: Optional[str] = None
