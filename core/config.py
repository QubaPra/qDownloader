from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DOWNLOAD_DIR = ROOT / "downloads"
DEFAULT_DOWNLOAD_DIR.mkdir(exist_ok=True)
