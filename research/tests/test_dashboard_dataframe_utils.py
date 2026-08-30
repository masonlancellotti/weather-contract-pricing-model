import pandas as pd

from dashboard.dataframe_utils import flatten_json_columns, make_unique_columns


def test_flatten_json_columns_prefixes_payload_keys_and_keeps_unique_columns():
    frame = pd.DataFrame(
        [
            {
                "market_ticker": "A",
                "edge_cents": 1,
                "payload": {"market_ticker": "B", "edge_cents": 2, "nested": {"x": 3}},
            }
        ]
    )
    result = flatten_json_columns(frame)
    assert "market_ticker" in result.columns
    assert "payload_market_ticker" in result.columns
    assert "payload_edge_cents" in result.columns
    assert "payload_nested.x" in result.columns
    assert len(result.columns) == len(set(result.columns))


def test_make_unique_columns_suffixes_remaining_duplicates():
    frame = pd.DataFrame([[1, 2, 3]], columns=["edge_cents", "edge_cents", "edge_cents"])
    result = make_unique_columns(frame)
    assert list(result.columns) == ["edge_cents", "edge_cents__2", "edge_cents__3"]
