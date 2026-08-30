# Edge Research Playbook

## Daily Workflow

1. Keep the orderbook recorder running.
2. Keep separate weather observation and forecast recorders running when testing forecast-aware strategies.
3. Run the scanner/live signal logger separately if needed.
4. Build exact settlements for newly resolved markets.
5. Build recorded replay.
6. Run recorded sweeps.
7. Generate an edge report.
8. Check the dashboard recommendation.
9. Do not trade real money unless gates are passed.

## Commands

```powershell
python main.py record-orderbooks --weather-only --interval-seconds 30
python main.py resolve-active-weather-stations
python main.py record-weather-observations --from-active-markets --interval-minutes 5
python main.py record-weather-forecasts --from-active-markets --interval-minutes 30
python main.py scan-live
python main.py audit-recorded-data
python main.py build-exact-settlements --start YYYY-MM-DD --end YYYY-MM-DD
python main.py build-recorded-replay --last-days 3
python main.py sweep-recorded --last-days 3
python main.py validate-signals --last-days 7
python main.py analyze-liquidity --last-days 7
python main.py trading-readiness --last-days 7
python main.py rank-opportunities --weather-only
python main.py daily-trading-research-update --last-days 7
python main.py edge-report --last-days 3
python main.py dashboard
```

Semantic cleanup and validation:

```powershell
python main.py project-status
python main.py reparse-contracts --weather-only --parser-version v2_range_bucket_semantics
python main.py rebuild-settlement-labels --weather-only --settlement-version v2_range_bucket_semantics
python main.py validate-settlement-labels --weather-only
python main.py validate-settlement-sources
python main.py mark-stale-runs --before-parser-version v2_range_bucket_semantics
python main.py rebuild-clean-edge-analysis --last-days 3 --dry-run
python main.py rebuild-clean-edge-analysis --last-days 3
```

Collector diagnostics:

```powershell
python main.py collector-health --last-hours 24
python main.py weather-recorder-health --last-hours 24
python main.py diagnose-settlement-skips --last-days 7
python main.py debug-city-settlements --city Austin --last-days 10
python main.py validate-orderbook-depths --last-days 3
```

## Research Questions

- Are markets stale after a threshold is already hit?
- Are late-day high-temp markets overpriced when the threshold is unlikely?
- Do ladder monotonicity violations exist and are they executable?
- Can wide spreads be passively quoted with enough edge?
- Does any signal beat future or closing prices?
- Does any strategy survive fees and worse fills?
- Do passive fills beat future prices, or are they adverse-selection traps?
- Are current fair-value gaps large enough after uncertainty, fees, and risk buffers?

## Strategy Gates

- Minimum 30 filled trades for preliminary confidence.
- Prefer 100+ trades for stronger confidence.
- Positive net P&L after fees.
- Survives 2x fees.
- Survives 1-cent and 3-cent worse fills.
- Not dependent on top 1 or top 3 trades.
- Primary settlement labels.
- No stale parser or settlement versions.
- Interpretable trade reasons.
- Signal must beat future/closing prices, not just settlement.
- Passive fills must be traded-through or otherwise high-evidence, not touched-only.

## Paper/Live Gates

Paper testing may start only when a strategy has preliminary positive evidence.

Tiny live testing only comes after paper results agree with clean backtests. Live trading must remain disabled by default. The default recommendation should be `KEEP_COLLECTING_DATA` unless evidence is strong.

Run `python main.py trading-readiness --last-days 7` before recommending paper/live testing. If it says `NOT_READY_NO_EDGE`, do not soften that result.
