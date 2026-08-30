# Weather Contract Pricing Model

An automated system for pricing daily temperature contracts on **Kalshi** and **Polymarket**.
It reads each market's settlement rules to extract the city, weather station, local date,
threshold, and comparator; pulls **National Weather Service** station observations and
forecasts; and runs a Gaussian model over the forecast and recent history to produce its
own probability for each outcome. Where the model's fair value diverges from the market by
more than a configurable edge threshold, a gated execution layer can act on it.

The system is built to **refuse rather than guess**: unclear rules, an ambiguous
settlement station, stale weather data, a thin order book, or insufficient edge all return
a skip instead of a signal.

> **Safety posture.** `LIVE_ENABLED=false` is the default and `KILL_SWITCH` halts all
> order placement. Nothing trades until both the config flag and your own credentials say
> otherwise; the risk engine enforces per-order, per-market, and per-venue exposure caps
> plus pre-flight checks either way.

This repository holds two generations of the project:

| Where | What | State |
|---|---|---|
| repository root | The trading bot: single-process asyncio engine, Gaussian pricing model, dual-venue adapters (Kalshi RSA-PSS, Polymarket EIP-712 + HMAC), risk engine, SQLite persistence, Streamlit dashboard. | 46 hermetic tests, green |
| [`research/`](./research) | The research MVP that came first: market-rules parser (city / station / date / threshold / comparator), NWS Daily Climate Report settlement-label builder, live orderbook + weather recorders, replay-based strategy sweeps, liquidity and adverse-selection analysis, a trading-readiness scorecard. | Research tooling; live trading intentionally disabled |

## Architecture

```
main.py              — Single asyncio process. All tasks in one loop.
config.py            — Pydantic-validated config from .env
storage.py           — SQLite persistence (WAL mode)
models.py            — Shared dataclasses
dashboard.py         — Streamlit local dashboard (read-only from DB)
venues/
  kalshi.py          — Kalshi REST + WS adapter (RSA-PSS auth)
  polymarket.py      — Polymarket CLOB adapter (EIP-712 + HMAC auth)
weather/
  mapping.py         — City → NWS station mappings (settlement-verified)
  providers.py       — NWS obs, NWS forecast, IEM ASOS history, Open-Meteo
  model.py           — Gaussian model: forecast + history → bucket probs
strategy/
  pricing.py         — Edge calculation: fair_prob vs market_prob
  risk.py            — Kill switch, safe mode, pre-flight checks, caps
  execution.py       — Order placement with idempotency, stale cleanup
  engine.py          — Main strategy tick: model → signals → execution
tests/               — pytest unit tests (no live API calls)
```

## Quickstart

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env               # runs against Kalshi demo, live trading off
python main.py                     # market discovery + weather + signals; orders suppressed
pytest tests/ -q                   # 46 tests, no live API calls
streamlit run dashboard.py         # read-only dashboard in a second terminal
```

Credential setup (only needed to go beyond public data) is documented in
[`.env.example`](./.env.example): Kalshi wants an RSA key pair from
kalshi.com/account/api, Polymarket a Polygon wallet key, and a free NOAA CDO token
improves historical labels.

## What the research phase established

The `research/` MVP was pointed at live markets for several weeks of recording before the
bot was written, and its conclusions shaped the design (full notes in
[`research/docs/`](./research/docs)):

- **Settlement is the real parsing problem.** Kalshi temperature markets settle on
  official NWS Daily Climate Report products from a specific station. Hourly observations
  and IEM ASOS are usable fallback labels but imperfect — so the parser prefers exact
  sources, tracks a semantics version (`v2_range_bucket_semantics`), and flags
  low-confidence station mappings for manual verification instead of trusting them.
- **Wide spreads are not free money.** A passive quote that gets filled often got filled
  because fair value just moved against you. The replay tooling treats touched-only
  passive quotes as no-fill by default and only counts traded-through fills as evidence.
- **Historical L2 order books are effectively unavailable publicly**, which is why the MVP
  ships its own live orderbook recorder — recorded forward data is the scarce asset.
- **Skip is a feature.** Unclear rules, stale weather, thin books, or thin edge produce
  `SKIP`, not a fake signal — the readiness scorecard has to pass before paper trading is
  even considered, and paper evidence is required before any live-readiness claim.

## License

MIT
