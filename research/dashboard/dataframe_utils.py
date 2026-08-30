from __future__ import annotations

import pandas as pd


def flatten_json_columns(frame: pd.DataFrame, json_columns: list[str] | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame
    json_columns = json_columns or [
        column
        for column in frame.columns
        if column == "payload" or column.endswith("_payload") or column.endswith("_json") or column in {"metadata", "raw_json"}
    ]
    result = frame.copy()
    for column in json_columns:
        if column not in result.columns:
            continue
        expanded = pd.json_normalize(result[column].map(_coerce_dict)).add_prefix(f"{column}_")
        result = pd.concat([result.drop(columns=[column]).reset_index(drop=True), expanded.reset_index(drop=True)], axis=1)
    return make_unique_columns(result)


def make_unique_columns(frame: pd.DataFrame) -> pd.DataFrame:
    seen: dict[str, int] = {}
    columns: list[str] = []
    for raw_column in frame.columns:
        column = str(raw_column)
        seen[column] = seen.get(column, 0) + 1
        if seen[column] == 1:
            columns.append(column)
        else:
            columns.append(f"{column}__{seen[column]}")
    result = frame.copy()
    result.columns = columns
    return result


def _coerce_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    return {}
