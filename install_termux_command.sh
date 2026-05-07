#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Install a global Termux command: qdownloader
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$PREFIX/bin/qdownloader"

cat > "$TARGET" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
exec bash "$SCRIPT_DIR/run_api_termux.sh"
EOF

chmod +x "$TARGET"

echo "[qDownloader] Installed command: qdownloader"
echo "Try: qdownloader"
