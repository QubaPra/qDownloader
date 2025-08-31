# qDownloader (YouTube/Twitch) — edukacyjny mobilny web downloader

Nowoczesna, mobilna aplikacja webowa (dark + glow) do pobierania materiałów z YouTube i (w przyszłości) Twitch. Na razie gotowa jest karta YouTube.

Uwaga: Używaj wyłącznie do własnych, edukacyjnych celów oraz zgodnie z regulaminami serwisów i obowiązującym prawem.

## Funkcje (MVP)
- Zakładki: YouTube (aktywny), Twitch (placeholder)
- Wklejasz URL — automatycznie wykrywa dostępne jakości przez `yt-dlp -F`
- Podgląd: miniatura, tytuł, kanał, czas trwania
- Lista jakości (tylko video-only) z informacjami: ext, res, bitrate, szac. rozmiar
- Kolejka pobierania: możesz dodawać kolejne podczas pobierania
- Pasek postępu, pozostały czas, anulowanie z potwierdzeniem
- Backend: Python + FastAPI, streaming logów/progresu przez SSE

## Uruchomienie
1. Zainstaluj zależności:
```
pip install -r requirements.txt
```
2. Uruchom serwer (Windows, cmd):
```
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
3. Otwórz w przeglądarce: http://localhost:8000

## Konfiguracja pobierania
- Domyślna ścieżka zapisu: `./downloads` względem katalogu uruchomienia
- Możesz wskazać własną ścieżkę w pierwszym polu UI (opcjonalnie)
- Aplikacja dobiera najlepsze dostępne audio automatycznie do wybranego video-only
- Pobieranie odporne na chwilowe braki internetu (przełączniki `--no-part` i retry)

## Techniczne
- API:
  - `POST /api/yt/formats` — zwraca metadane i listę formatów (video-only)
  - `POST /api/yt/download` — uruchamia pobieranie wybranego formatu (z najlepszym audio)
  - `GET /api/progress/{job_id}` — SSE z postępem
  - `DELETE /api/cancel/{job_id}` — anulowanie pobierania
- Frontend: `templates/index.html`, `static/style.css`, `static/app.js`

## Ostrzeżenie prawne
- Nie zachęcamy do łamania praw autorskich ani regulaminów. Odpowiadasz za sposób użycia narzędzia.
