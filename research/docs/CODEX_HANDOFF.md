# Codex Handoff

## Before Coding, Read These Files

1. `docs/PROJECT_PURPOSE.md`
2. `docs/DATA_REALITY.md`
3. `docs/DIRECTORY_MAP.md`
4. `docs/CURRENT_STATUS.md`
5. `docs/EDGE_RESEARCH_PLAYBOOK.md`

## Rules For Future Codex Agents

- Do not summarize the chat and guess architecture. Inspect repo files and these docs first.
- Do not build live trading unless explicitly asked and gates are passed.
- Do not claim profitability from stale runs.
- Do not use midpoint fills.
- Do not ignore fees.
- Do not use low-confidence settlement labels in primary results.
- Do not mix approximate passive P&L with conservative taker P&L.
- Do not modify parser or settlement semantics without invalidating old runs.
- Do not optimize strategies until parser and settlement labels are validated.
- Always check `parser_version`, `settlement_version`, and stale run flags before trusting P&L.
- Keep `record-orderbooks` isolated. Do not put weather, NWS, settlement, replay, scanner, or dashboard work inside the pure orderbook recorder.
- Weather observations and forecast snapshots are recorded by separate commands. They may fail without stopping orderbook collection.
- Run `python main.py trading-readiness --last-days 7` before recommending paper testing or any live-trading work.
- Do not build live trading before readiness gates pass and the user explicitly asks for it.
- Do not call passive liquidity profitable unless fill evidence survives adverse-selection checks.

## Current Mental Model

This is a data/research engine. The valuable asset is recorded orderbook data, recorded as-of weather/forecast data, and exact settlement data. The goal is finding whether edge exists, not making dashboard numbers green.

Every strategy result must be explainable trade by trade. If the data is incomplete, say what is missing. If no edge exists, say: "No reliable edge found yet."

## When Continuing Work

1. Run tests.
2. Run `python main.py audit-recorded-data`.
3. Run `python main.py project-status`.
4. Check parser version and stale runs.
5. Check settlement label quality.
6. Check `python main.py weather-recorder-health --last-hours 24` if forecast-aware strategies are involved.
7. Build replay only after data quality passes.
8. Run sweep.
9. Run `python main.py validate-signals --last-days 7`.
10. Run `python main.py analyze-liquidity --last-days 7`.
11. Run `python main.py trading-readiness --last-days 7`.
12. Generate edge report.
13. Update `docs/CURRENT_STATUS.md`.

## How To End Every Implementation Response

End with:

- What changed.
- What commands were run.
- Test results.
- Data counts if known.
- What is trustworthy.
- What is not trustworthy.
- Whether to keep collecting data.
- Whether real-money trading is justified.
- Next exact command.

## Project Status Command

Use:

```powershell
python main.py project-status
```

It prints docs path, key DB counts, latest audit verdict, latest edge report, latest recommendation, recorder freshness warning, stale run counts, and active parser/settlement versions.
