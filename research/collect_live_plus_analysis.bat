@echo off
setlocal
cd /d "%~dp0"
echo Starting Kalshi weather collector with optional scanner/settlement/replay analysis.
echo Prefer collect_live_for_3_days.bat for uninterrupted orderbook recording.
echo Live trading remains disabled. Press Ctrl+C to stop.
".venv\Scripts\python.exe" main.py collect-live --duration-hours 72 --interval-seconds 30 --max-markets 100 --scan-interval-minutes 5 --maintenance-interval-minutes 60 --settlement-lookback-days 10
endlocal
