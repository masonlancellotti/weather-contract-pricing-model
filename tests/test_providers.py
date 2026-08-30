from config import cfg
from weather import providers


def test_cdo_daily_highs_use_standard_unit_values(monkeypatch):
    monkeypatch.setattr(cfg, "noaa_cdo_token", "token", raising=False)
    monkeypatch.setattr(
        providers,
        "_get",
        lambda *args, **kwargs: {
            "results": [
                {"date": "2026-04-19T00:00:00", "value": 72.0},
                {"date": "2026-04-20T00:00:00", "value": 68.0},
            ]
        },
    )
    providers._history_cache.clear()
    result = providers.get_cdo_daily_highs("GHCND:USW00023174", days_back=2)
    assert result == [
        {"date": "2026-04-19", "high_f": 72.0},
        {"date": "2026-04-20", "high_f": 68.0},
    ]
