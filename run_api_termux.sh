#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Start FastAPI server in Termux and open localhost in default browser.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${QD_PORT:-8000}"
HOST="${QD_HOST:-0.0.0.0}"
PYTHON_BIN="${PYTHON_BIN:-python}"
URL="http://127.0.0.1:${PORT}/"

echo "[qDownloader] Starting API on ${HOST}:${PORT}"
echo "[qDownloader] Project dir: ${SCRIPT_DIR}"

if command -v termux-open-url >/dev/null 2>&1; then
  (
    sleep 2
    termux-open-url "$URL" >/dev/null 2>&1 || true
  ) &
else
  echo "[qDownloader] termux-open-url not found. Open manually: ${URL}"
fi

exec "$PYTHON_BIN" -m uvicorn app:app --host "$HOST" --port "$PORT"
