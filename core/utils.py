import re

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
