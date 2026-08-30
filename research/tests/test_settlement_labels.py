from datetime import date

from data.weather_settlement_loader import evaluate_condition, evaluate_contract_result
from parsing.weather_contract import WeatherContract


def test_settlement_comparator_logic():
    assert evaluate_condition(90, 89, "gt") == 1
    assert evaluate_condition(90, 90, "gt") == 0
    assert evaluate_condition(90, 90, "gte") == 1
    assert evaluate_condition(32, 33, "lt") == 1
    assert evaluate_condition(32, 32, "lt") == 0
    assert evaluate_condition(32, 32, "lte") == 1


def test_range_bucket_settlement_logic():
    contract = WeatherContract(
        event_ticker="KXHIGHPHIL-26APR30",
        market_ticker="KXHIGHPHIL-26APR30-B66.5",
        title="Will the high temp in Philadelphia be 66-67 degrees?",
        local_date=date(2026, 4, 30),
        variable_type="high_temp",
        contract_type="range_bucket",
        range_low=66,
        range_high=67,
    )
    assert evaluate_contract_result(contract, 66) == 1
    assert evaluate_contract_result(contract, 67) == 1
    assert evaluate_contract_result(contract, 68) == 0


def test_strict_threshold_settlement_logic():
    above = WeatherContract(
        event_ticker="E",
        market_ticker="M",
        variable_type="high_temp",
        contract_type="threshold_above",
        threshold=73,
        comparator="gt",
    )
    at_least = above.model_copy(update={"comparator": "gte"})
    assert evaluate_contract_result(above, 73) == 0
    assert evaluate_contract_result(at_least, 73) == 1
