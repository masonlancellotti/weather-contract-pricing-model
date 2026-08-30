# Data Reality

## Kalshi Data

- Current live orderbooks can be recorded going forward.
- Current live orderbooks are the scarce data priority. Keep `record-orderbooks` pure and isolated.
- Historical full L2 orderbooks are likely unavailable publicly.
- Historical candlesticks and trades can support conservative taker and signal tests.
- Passive maker tests from historical candlesticks/trades are approximate.
- Recorded live full orderbooks improve passive replay, but still do not reveal true queue position.
- Wide spreads are not free money. A fill can mean adverse selection: the quote gets hit because fair value just moved against us.
- Touched-only passive quotes should be treated as no-fill by default.
- Traded-through fills are stronger evidence, but still approximate without queue position and exact trade prints.

## Weather Data

- Kalshi weather markets often settle using official NWS Daily Climate Report / Climatological Report products.
- Exact NWS reports should be preferred for primary settlement labels.
- Hourly station observations and IEM ASOS are useful fallback labels, but they are imperfect.
- Open-Meteo and reanalysis data must not be used as settlement truth.
- Settlement labels below the confidence threshold should not be used in primary P&L.
- Live station observations can be recorded separately with `record-weather-observations`. They improve as-of features but are still not final settlement truth.
- Live forecast snapshots can be recorded separately with `record-weather-forecasts`. They are fragile because historical forecast snapshots may not be reconstructable later.
- Weather recorders may fail or time out without stopping orderbook collection. That separation is intentional.
- Exact final settlement labels are still built separately from NWS Daily Climate Reports when available.

## Contract Semantics

Range/bucket markets are dangerous if parsed incorrectly. A market saying `66-67 degrees` means the final high or low must land inside that bucket. It is not equivalent to `<66`, `<=67`, `>66`, or `>=66`.

Current parser and settlement version: `v2_range_bucket_semantics`.

Old P&L generated before this version is stale unless explicitly regenerated and labeled non-stale.

## No-Lookahead Rules

- Features at timestamp T can only use weather observations at or before T.
- Final settlement can only be used for final labels and payout.
- Future observations, final highs/lows, and forecast revisions cannot leak into replay features.
- Recorded forecasts are usable at replay timestamp T only if `ts_recorded <= T`.
- Recorded live observations are usable at replay timestamp T only if `ts_recorded <= T` and the observation time is not in the future.

## Known Data Risks

- Station mapping errors.
- Range/bucket market parser errors.
- Settlement source metadata inconsistencies.
- Missing NWS climate reports for some stations/dates.
- Missing recorded forecast snapshots for late-day strategy windows.
- Low-confidence active market-to-station mappings, especially cities where Kalshi may use a different official station than the obvious airport/city code.
- Sparse orderbook snapshots.
- Low liquidity and fake tradability.
- Duplicate stale backtest runs.
- Old runs generated with old parser or settlement semantics.

## Trust Levels

- Conservative taker backtests with high-confidence labels: most trustworthy.
- Recorded live as-of weather/forecast replay: better for model tests than reconstructed weather when available.
- Signal-only tests: useful, but not P&L.
- Passive maker results without recorded full books: approximate.
- Passive maker results with recorded full books: better, but queue-uncertain.
- Passive liquidity research is only interesting when fills beat future prices after 5/15/30/60 minutes, not merely when final settlement works out.
- Any result from stale parser/settlement versions: invalid for edge decisions.
