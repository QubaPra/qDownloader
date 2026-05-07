@echo off
cd /d "%~dp0"

echo [+] Uruchamianie API qDownloader...
echo.

REM Otworz przegledarke w tle (bez nowego okna)
start "" http://localhost:8000/

REM Uruchom Uvicorn w obecnym oknie
echo [+] Uruchamianie serwera API na http://localhost:8000/
echo.
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
