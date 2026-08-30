@echo off
setlocal
cd /d "%~dp0"
echo Starting pure Kalshi weather orderbook recorder. Live trading remains disabled.
echo Leave this window open. Press Ctrl+C to stop.
echo Logs are written to the console and data is stored in kalshi_weather_edge.db.
".venv\Scripts\python.exe" main.py record-orderbooks --weather-only --interval-seconds 30 --duration-hours 72 --max-markets 100
endlocal
