from weather.mapping import get_city


def test_get_city_resolves_aliases():
    assert get_city("LAX").city_code == "LA"
    assert get_city("SFO").city_code == "SF"
    assert get_city("PHL").city_code == "PHIL"
    assert get_city("SAT").city_code == "SATX"


def test_new_supported_cities_present():
    assert get_city("AUS").kalshi_series == "KXHIGHAUS"
    assert get_city("DC").kalshi_series == "KXHIGHTDC"
    assert get_city("OKC").kalshi_series == "KXHIGHTOKC"


def test_cdo_station_ids_are_present():
    assert get_city("LA").cdo_station.startswith("GHCND:")
    assert get_city("NYC").cdo_station.startswith("GHCND:")
    assert get_city("SATX").cdo_station.startswith("GHCND:")


def test_houston_coordinates_match_hobby_station():
    hou = get_city("HOU")
    assert round(hou.lat, 4) == 29.6375
    assert round(hou.lon, 4) == -95.2825
