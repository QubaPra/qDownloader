#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Create shortcut script for Termux:Widget app.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHORTCUT_DIR="$HOME/.shortcuts"
SHORTCUT_FILE="$SHORTCUT_DIR/qdownloader"

mkdir -p "$SHORTCUT_DIR"

cat > "$SHORTCUT_FILE" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/run_api_termux.sh"
EOF

chmod +x "$SHORTCUT_FILE"

echo "[qDownloader] Shortcut script created: $SHORTCUT_FILE"
echo "Install app 'Termux:Widget' from F-Droid / Play Store, then add widget to home screen."
echo "Tap 'qdownloader' from widget to start API + open browser."
