@echo off
setlocal
cd /d "%~dp0"
echo Starting live weather forecast recorder. This is separate from orderbook recording.
echo Leave this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" main.py record-weather-forecasts --from-active-markets --interval-minutes 30 --duration-hours 72
endlocal
