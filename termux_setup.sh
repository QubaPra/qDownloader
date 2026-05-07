#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Install system and Python dependencies required by qDownloader in Termux.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[qDownloader] Updating packages..."
pkg update -y
pkg upgrade -y

echo "[qDownloader] Installing required Termux packages..."
pkg install -y python ffmpeg git termux-tools

echo "[qDownloader] Installing Python requirements..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[qDownloader] Making helper scripts executable..."
chmod +x run_api_termux.sh install_termux_command.sh install_termux_shortcut.sh

echo "[qDownloader] Done."
echo "Next: ./install_termux_command.sh"
echo "Then run: qdownloader"
