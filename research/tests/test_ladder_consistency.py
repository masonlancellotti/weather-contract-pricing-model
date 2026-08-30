from datetime import date

from parsing.weather_contract import WeatherContract
from strategies.ladder_consistency import LadderConsistencyStrategy


def _contract(ticker: str, threshold: float) -> WeatherContract:
    return WeatherContract(
        event_ticker="KXHIGHNY-26APR27",
        market_ticker=ticker,
        title=ticker,
        city="New York",
        local_date=date(2026, 4, 27),
        variable_type="high_temp",
        threshold=threshold,
        comparator="gte",
        unit="F",
        settlement_source="NWS/NOAA",
        yes_condition=f"high_temp gte {threshold}",
        parse_confidence=0.95,
        station_confidence=0.95,
        is_tradable=True,
    )


def test_ladder_violation_uses_bid_ask_not_mid():
    contracts = [_contract("LOWER", 88), _contract("HIGHER", 90)]
    features = {"LOWER": {"yes_ask": 40}, "HIGHER": {"yes_bid": 48}}
    signals = LadderConsistencyStrategy().generate_group(contracts, features)
    assert {signal.action for signal in signals} == {"BUY_YES", "SELL_YES"}
    assert all(signal.edge_cents >= 3 for signal in signals)
