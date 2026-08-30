from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from config import settings
from data.kalshi_client import KalshiClient
from data.kalshi_market_loader import KalshiMarketLoader
from data.storage import Storage
from data.weather_station_mapper import StationMapper
from parsing.market_parser import WeatherMarketParser
from parsing.weather_contract import WeatherContract


@dataclass(frozen=True)
class ActiveStationResolveResult:
    markets: int
    stations: int
    low_confidence: int
    warnings: list[str]
    rows: list[dict]

    def to_dict(self) -> dict:
        return {
            "markets": self.markets,
            "stations": self.stations,
            "low_confidence": self.low_confidence,
            "warnings": self.warnings,
            "rows": self.rows,
        }

    def to_text(self) -> str:
        lines = [
            f"markets={self.markets}",
            f"stations={self.stations}",
            f"low_confidence_mappings={self.low_confidence}",
        ]
        for row in self.rows[:100]:
            lines.append(
                f"{row.get('market_ticker')} city={row.get('city')} station={row.get('station_code')} "
                f"confidence={row.get('mapping_confidence')} reason={row.get('mapping_reason')} warnings={row.get('warnings') or ''}"
            )
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"- {warning}" for warning in self.warnings[:50])
        return "\n".join(lines)


class ActiveWeatherStationResolver:
    """Resolve active Kalshi weather markets to the stations worth recording.

    Rules/title station mentions beat curated defaults. Curated defaults are
    useful for data collection, but low-confidence mappings should stay out of
    primary P&L until manually verified against the Kalshi rule text.
    """

    def __init__(
        self,
        client: KalshiClient | None = None,
        storage: Storage | None = None,
        parser: WeatherMarketParser | None = None,
        mapper: StationMapper | None = None,
    ):
        self.storage = storage or Storage()
        self.client = client or KalshiClient()
        self.parser = parser or WeatherMarketParser()
        self.mapper = mapper or StationMapper()
        self.loader = KalshiMarketLoader(client=self.client, storage=self.storage, parser=self.parser)

    def resolve_active(self, weather_only: bool = True, max_markets: int | None = None, persist: bool = True) -> ActiveStationResolveResult:
        self.storage.init_db()
        max_markets = max_markets or settings.orderbook_record_max_markets
        if weather_only:
            markets = self.loader.load_active_weather_markets(persist=True, max_pages=1, max_series=max(1, min(max_markets, 25)))
        else:
            markets = list(self.client.iter_markets(status="open", limit=1000, max_pages=1))
        rows: list[dict] = []
        warnings: list[str] = []
        for market in markets[:max_markets]:
            contract = self.parser.parse(market)
            row = self._row_for_contract(contract)
            rows.append(row)
            if persist:
                self.storage.upsert_active_weather_station_map(row)
            if row["mapping_confidence"] < 0.75:
                warnings.append(f"{contract.market_ticker}: low-confidence station mapping")
        stations = {row["station_code"] for row in rows if row.get("station_code")}
        return ActiveStationResolveResult(
            markets=len(rows),
            stations=len(stations),
            low_confidence=sum(1 for row in rows if float(row.get("mapping_confidence") or 0.0) < 0.75),
            warnings=warnings,
            rows=rows,
        )

    def _row_for_contract(self, contract: WeatherContract) -> dict:
        mapping = self.mapper.resolve(contract.city, contract.station_code)
        warnings = list(contract.warnings)
        if mapping is None:
            warnings.append("no station mapping found")
            station_code = None
            confidence = 0.0
            reason = "missing_station_mapping"
        else:
            station_code = mapping.station_code
            confidence = min(float(contract.station_confidence or mapping.confidence), 0.99) if contract.station_code else mapping.confidence
            reason = "explicit_station_from_rules" if contract.station_code else f"curated_default_mapping:{mapping.source}"
            if mapping.notes:
                warnings.append(mapping.notes)
            if not contract.station_code:
                warnings.append("station inferred from curated mapping; verify Kalshi rules before primary trading")
        return {
            "market_ticker": contract.market_ticker,
            "event_ticker": contract.event_ticker,
            "city": contract.city,
            "station_code": station_code,
            "station_name": mapping.station_name if mapping else None,
            "wfo": mapping.wfo if mapping else None,
            "timezone": mapping.timezone if mapping else None,
            "settlement_source_type": contract.settlement_source,
            "source_url_or_hint": contract.settlement_source,
            "mapping_confidence": confidence,
            "mapping_reason": reason,
            "warnings": "; ".join(sorted(set(warnings))),
            "parser_version": contract.parser_version,
            "updated_at": datetime.now(timezone.utc),
        }


def unique_station_mappings(rows: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for row in rows:
        station = row.get("station_code")
        if not station:
            continue
        current = deduped.get(station)
        if current is None or float(row.get("mapping_confidence") or 0.0) > float(current.get("mapping_confidence") or 0.0):
            deduped[station] = row
    return sorted(deduped.values(), key=lambda item: str(item.get("station_code")))


def rows_to_json(rows: list[dict]) -> str:
    return json.dumps(rows, default=str)
