@echo off
setlocal
cd /d "%~dp0"
echo Starting live station observation recorder. This is separate from orderbook recording.
echo Leave this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" main.py record-weather-observations --from-active-markets --interval-minutes 5 --duration-hours 72
endlocal
