# Project Purpose

## Mission

This repo is a Kalshi-only weather prediction-market research system. The goal is to find a real, tradable edge as quickly as possible, starting with daily temperature markets, while refusing comforting fake P&L.

Live trading is disabled until conservative backtests and paper trading justify tiny real-money testing. The system should help decide: trade tiny in paper, keep collecting data, change the strategy, or abandon the idea.

## Core Philosophy

Think like a market maker and risk manager, not a gambler.

- No midpoint-fill fantasy.
- No edge claims without fees, spreads, slippage, fills, settlement truth, and robustness checks.
- Historical full L2 Kalshi orderbooks are likely unavailable, so recorded live orderbooks are a core asset.
- Exact NWS Daily Climate Report settlement labels matter because hourly ASOS/METAR observations can differ from official reports.
- Parser and settlement correctness are edge-protection gates. Range/bucket contracts must not be treated as simple thresholds.
- If no edge exists, the correct output is: "No reliable edge found yet."

## Current Research Targets

- Already-hit threshold lag or stale pricing.
- Late-day high-temperature fade.
- Ladder consistency and monotonicity violations.
- Wide-spread passive market-making.
- Future passive replay using recorded full orderbooks.

There are two separate edge families:

- Fair-value edge: estimate contract probability from as-of weather/forecast data and compare it to executable bid/ask after fees and uncertainty buffers.
- Liquidity edge: quote passively in wide spreads only if conservative fill evidence and adverse-selection diagnostics say fills are not mostly bad fills.

These must not be combined into one vague P&L number.

## Non-Goals For Now

- Live automated trading.
- Twitter, news, or sentiment bots.
- Deep learning.
- Non-weather categories.
- Beautiful UI polish.
- Overfit parameter mining.
- Any strategy that cannot be explained trade by trade.

## Decision Standard

A strategy is not promising unless it:

- Uses correct contract parsing.
- Uses high-confidence settlement labels.
- Produces positive net P&L after fees.
- Survives worse fills.
- Is not driven by one outlier.
- Has enough sample size.
- Beats future or closing prices in signal tests.
- For passive liquidity, shows fills beat future prices and do not rely on touched-only fills.
- Can be explained clearly.
