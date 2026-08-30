from data.nws_climate_report_parser import parse_cli_report


def test_parse_cli_daily_high_low_precip():
    raw = """
CLIMATE REPORT
NATIONAL WEATHER SERVICE PEACHTREE CITY GA
420 AM EDT THU APR 16 2026

...THE ATLANTA CLIMATE SUMMARY FOR APRIL 15 2026...

TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         85   3:51 PM
  MINIMUM         60   6:44 AM

PRECIPITATION (IN)
  YESTERDAY        0.00
"""
    parsed = parse_cli_report(raw)
    assert parsed.report_date.isoformat() == "2026-04-15"
    assert parsed.high_temp == 85
    assert parsed.low_temp == 60
    assert parsed.precip == 0
    assert parsed.confidence >= 0.9
