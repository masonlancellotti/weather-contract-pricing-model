# Kalshi Weather Edge

> For future Codex sessions, read `docs/CODEX_HANDOFF.md` first. It explains the project purpose, data reality, current limitations, semantic parser version, and what outputs are safe to trust.

Lean research/backtesting/live-monitoring MVP for Kalshi weather markets. The system is deliberately conservative: unclear rules, unclear station/source, stale weather, thin order books, insufficient edge, and disabled live trading all become `SKIP`, not fake alpha.

## What It Does

- Loads public Kalshi market data and order books.
- Filters likely weather markets, focused first on daily high/low temperature contracts.
- Parses city, station, local date, threshold, comparator, source, and settlement warnings.
- Pulls station-level NWS observations and Open-Meteo forecast fallback features.
- Computes conservative binary order book math from YES/NO bid books.
- Produces heuristic fair values and strategy signals.
- Runs a Streamlit dashboard for scanner output, backtest runs, market detail, risk, and data quality.
- Scaffolds paper trading and live order management, with live trading intentionally disabled.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py init-db
```

Public-data mode does not require Kalshi credentials.

## Configuration

Edit `.env`:

```env
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=
KALSHI_ENV=prod
ENABLE_LIVE_TRADING=false
DATABASE_URL=sqlite:///kalshi_weather_edge.db
```

Default risk limits are tiny by design:

```env
MAX_TRADE_DOLLARS=5
MAX_MARKET_EXPOSURE=20
MAX_TOTAL_EXPOSURE=100
MAX_DAILY_LOSS=25
MIN_EDGE_CENTS=7
MIN_SPREAD_CENTS=4
MAX_WEATHER_DATA_AGE_MINUTES=15
ALLOW_MARKET_ORDERS=false
```

## Commands

```powershell
python main.py load-markets
python main.py record-orderbooks --weather-only --interval-seconds 30 --duration-hours 72 --max-markets 100
python main.py resolve-active-weather-stations
python main.py record-weather-observations --from-active-markets --interval-minutes 5 --duration-hours 72
python main.py record-weather-forecasts --from-active-markets --interval-minutes 30 --duration-hours 72
python main.py collect-live --duration-hours 72 --interval-seconds 30 --max-markets 100
python main.py audit-recorded-data
python main.py build-recorded-replay --last-days 3
python main.py backtest-recorded --strategy already_hit --last-days 3 --mode taker --label-quality primary
python main.py sweep-recorded --last-days 3
python main.py validate-signals --last-days 7
python main.py analyze-liquidity --last-days 7
python main.py trading-readiness --last-days 7
python main.py rank-opportunities --weather-only
python main.py daily-trading-research-update --last-days 7
python main.py edge-report --last-days 3
python main.py load-history --weather-only --start 2025-01-01 --end 2026-04-27 --limit 200
python main.py build-exact-settlements --start 2025-01-01 --end 2026-04-27
python main.py build-replay --start 2025-01-01 --end 2026-04-27
python main.py scan-live --max-markets 50
python main.py backtest --strategy already_hit --start 2025-01-01 --end 2026-04-27 --mode taker --label-quality primary
python main.py backtest --strategy late_day_high_fade --start 2025-01-01 --end 2026-04-27 --mode taker --label-quality primary
python main.py dashboard
python main.py paper-trade
```

For Command Prompt, the simplest multi-day collector is:

```cmd
collect_live_for_3_days.bat
```

Leave that window open. It only records live orderbooks every 30 seconds. It does not run scanner, settlement, replay, backtests, or any live trading.

Weather and forecast recording are separate on purpose. To collect as-of weather features too, open separate Command Prompt windows:

```cmd
collect_weather_features_for_3_days.bat
collect_forecasts_for_3_days.bat
```

The backtester will print:

```text
No robust edge found under conservative assumptions.
```

unless local replay data exists and conservative assumptions support a positive result. This is intentional. Historical taker fills use Kalshi candlestick bid/ask proxies, not full historical level-2 orderbook depth.

## Current Data Reality

- Kalshi current orderbooks can be recorded going forward with `record-orderbooks`.
- Historical full L2 orderbooks are likely unavailable from public endpoints.
- Historical candlesticks/trades can support conservative taker and signal tests.
- Passive market-making cannot be truly validated historically without our own recorded orderbooks.
- Exact weather settlement should use NWS Daily Climate Report / CLI products when possible.
- Hourly ASOS/IEM labels are useful but imperfect and should usually be exploratory, not primary.
- Live forecast snapshots may not be reconstructable later. Use `record-weather-forecasts` in a separate terminal if late-day forecast-aware testing matters.
- Do not put weather/NWS calls into the pure orderbook recorder.

## Recommended Immediate Workflow

1. Start the multi-day collector in Command Prompt:
   ```cmd
   collect_live_for_3_days.bat
   ```

2. Optional but recommended for forecast-aware tests: start live weather observations in another terminal:
   ```cmd
   collect_weather_features_for_3_days.bat
   ```

3. Optional but recommended for late-day strategy tests: start forecast snapshots in a third terminal:
   ```cmd
   collect_forecasts_for_3_days.bat
   ```

4. Optional: open the dashboard in another terminal:
   ```powershell
   python main.py dashboard
   ```

5. Optional: run a one-off scanner/paper logger check:
   ```powershell
   python main.py scan-live
   ```

6. Build historical data:
   ```powershell
   python main.py load-history --weather-only --start YYYY-MM-DD --end YYYY-MM-DD --limit 200
   ```

7. Build exact settlement labels where possible:
   ```powershell
   python main.py build-exact-settlements --start YYYY-MM-DD --end YYYY-MM-DD
   ```

8. Build replay:
   ```powershell
   python main.py build-replay --start YYYY-MM-DD --end YYYY-MM-DD
   ```

9. Backtest:
   ```powershell
   python main.py backtest --strategy already_hit --mode taker --label-quality primary
   python main.py backtest --strategy late_day_high_fade --mode taker --label-quality primary
   ```

10. Only if results are promising, run paper trading. Keep live trading disabled.

## Long-Running Orderbook Recorder

The pure orderbook recorder is the preferred "start once and let the machine run" command:

```powershell
python main.py record-orderbooks --weather-only --duration-hours 72 --interval-seconds 30 --max-markets 100
```

Use `--duration-hours 0` to run indefinitely until Ctrl+C. Keep the computer awake and connected to the internet. The pure recorder writes into `kalshi_weather_edge.db`, especially `orderbook_snapshots_live` and `collector_state`.

The pure recorder is intentionally narrow:

- orderbook collection: every 30 seconds by default,
- scanner/signal logging: not run,
- exact settlement/replay refresh: not run,
- live trading: never.

Use `collect_live_plus_analysis.bat` or `python main.py collect-live ...` only when you explicitly want optional scanner/settlement/replay work alongside recording. The orderbook recorder runs isolated in that mode, but pure recording is still safer for unattended multi-day collection.

## Separate Weather Feature Recorders

Run these in separate terminals. They do not place orders and they do not control orderbook recording:

```powershell
python main.py resolve-active-weather-stations
python main.py record-weather-observations --from-active-markets --interval-minutes 5 --duration-hours 72
python main.py record-weather-forecasts --from-active-markets --interval-minutes 30 --duration-hours 72
python main.py weather-recorder-health --last-hours 24
```

Observation snapshots are stored in `weather_observation_snapshots_live`. Forecast snapshots are stored in `weather_forecast_snapshots_live`. Recorded replay uses these only when `ts_recorded` is at or before the replay timestamp, so future forecast revisions cannot leak into a backtest.

These feature recorders are not settlement truth. Primary settlement labels still come from exact NWS Daily Climate Reports when available.

## Recorded Orderbook Analysis

After collecting full live orderbooks for a few days, run:

```powershell
python main.py audit-recorded-data
python main.py build-recorded-replay --last-days 3
python main.py backtest-recorded --strategy already_hit --last-days 3 --mode taker --label-quality primary
python main.py backtest-recorded --strategy late_day_high_fade --last-days 3 --mode taker --label-quality primary
python main.py sweep-recorded --last-days 3
python main.py edge-report --last-days 3
```

Recorded replay uses `orderbook_snapshots_live` as the source of truth. The replay table stores compact features and a source snapshot id instead of duplicating full depth JSON, because duplicating the full book tape can bloat SQLite quickly. Full depth remains available in `orderbook_snapshots_live`.

Interpretation rules:

- Taker results can be considered primary only when settlement labels are high confidence.
- Passive results are approximate unless fills are clearly traded through; queue position is unknown.
- `READY_FOR_TINY_LIVE_TEST` should not appear without a separate positive paper-trading period.
- A positive replay result is only a paper-test candidate until it survives paper fills, exact settlements, fees, and worse-fill sensitivity.

## Trading Research Commands

```powershell
python main.py trading-readiness --last-days 7
python main.py analyze-liquidity --last-days 7
python main.py validate-signals --last-days 7
python main.py rank-opportunities --weather-only
python main.py paper-trade --strategy rank_opportunities --weather-only --mode taker_paper
python main.py daily-trading-research-update --last-days 7
```

These are research and paper-only commands. `analyze-liquidity` treats touched-only passive quotes as no-fill by default and reports adverse selection against future prices. `rank-opportunities` compares executable bid/ask to heuristic fair value after uncertainty, fees, and buffers. `paper-trade` logs simulated paper orders only; it does not send real orders.

## Do Not Trust Yet

- Passive maker P&L without recorded full orderbooks.
- Settlement labels below 0.85 confidence.
- Strategies with fewer than 30 trades.
- Profit that disappears after fees/slippage.
- Profit driven by one outlier.

## Dashboard

```powershell
python main.py dashboard
```

Tabs:

- Live Scanner: active weather markets, parsed fields, bid/ask, fair value, edge, signal, action, skip reason.
- Backtest Results: stored backtest runs and P&L if replay trades exist.
- Market Detail: raw Kalshi payload and parsed contract.
- Risk: paper orders and positions.
- Data Quality: failed/low-confidence parses, warnings, stale/missing data.

## Station Mappings

Defaults live in `data/weather_station_mapper.py`. Prefer overrides in `config/station_overrides.yaml`:

```yaml
NYC:
  default_station: KNYC
  notes: "Verify Kalshi rules; NYC could differ by product."
```

Rules win over defaults. If a Kalshi contract explicitly names a station, the parser/mapper use that station.

## Strategy Notes

Implemented:

- `AlreadyHitThresholdStrategy`: buys YES only when official station observation has already crossed threshold and confidence gates are high.
- `LateDayHighFadeStrategy`: buys NO late in the day when max-so-far and remaining forecast are safely below threshold.
- `LadderConsistencyStrategy`: detects threshold ladder violations using executable bid/ask relationships, not mids.
- `PassiveMarketMakerStrategy`: quote scaffold around fair value, spread, fee buffer, and confidence gates.

Live trading is not implemented. `live/order_manager.py` raises even if `ENABLE_LIVE_TRADING=true`; that is a deliberate safety catch until paper trading/backtests are validated.

## Data Quality Rules

The system should not trade or backtest a contract when:

- parser confidence is low,
- station/source is unclear,
- exact settlement value cannot be approximated from station observations,
- weather data is stale,
- the book is too thin,
- edge is below fee/slippage/risk buffers.

Open-Meteo is used only as a forecast fallback and should not be treated as settlement truth.

## Tests

```powershell
pytest
```

Critical tests cover:

- Kalshi YES/NO order book math,
- parser tradability gates,
- no-lookahead backtest behavior when replay data is absent,
- ladder consistency using bid/ask,
- fee model behavior.

## Kalshi API References

The client follows Kalshi public market-data docs:

- Public market data base URL and unauthenticated requests: https://docs.kalshi.com/getting_started/quick_start_market_data
- Markets endpoint and pagination: https://docs.kalshi.com/api-reference/market/get-markets
- Orderbook endpoint and YES/NO bid semantics: https://docs.kalshi.com/api-reference/market/get-market-orderbook
- Trades endpoint and pagination: https://docs.kalshi.com/api-reference/market/get-trades
- Demo API root: https://docs.kalshi.com/getting_started/demo_env
