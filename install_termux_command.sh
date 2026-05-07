#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Create a global Termux command: qdownloader
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$PREFIX/bin/qdownloader"

ln -sf "$SCRIPT_DIR/run_api_termux.sh" "$TARGET"
chmod +x "$SCRIPT_DIR/run_api_termux.sh" "$TARGET"

echo "[qDownloader] Installed command: qdownloader"
echo "Try: qdownloader"
