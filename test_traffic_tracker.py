from datetime import datetime, timezone

from app import (
    DEFAULT_HOMEPAGE_PERIOD,
    calculate_network_delta,
    homepage_period_since,
)


def test_calculate_network_delta_uses_positive_increments():
    previous = {"container_a": {"rx_bytes": 100, "tx_bytes": 200}}
    current = {"container_a": {"rx_bytes": 180, "tx_bytes": 230}}

    assert calculate_network_delta(previous, current) == {"download": 80, "upload": 30}


def test_calculate_network_delta_counts_new_container_from_zero():
    previous = {"container_a": {"rx_bytes": 100, "tx_bytes": 200}}
    current = {"container_b": {"rx_bytes": 250, "tx_bytes": 300}}

    assert calculate_network_delta(previous, current) == {"download": 250, "upload": 300}


def test_calculate_network_delta_never_goes_negative():
    previous = {"container_a": {"rx_bytes": 200, "tx_bytes": 300}}
    current = {"container_a": {"rx_bytes": 150, "tx_bytes": 250}}

    assert calculate_network_delta(previous, current) == {"download": 0, "upload": 0}


def test_homepage_period_defaults_to_alltime():
    period, since = homepage_period_since(None)
    assert period == DEFAULT_HOMEPAGE_PERIOD == "alltime"
    assert since is None


def test_homepage_period_today_starts_at_midnight_utc():
    now = datetime(2026, 8, 17, 18, 30, tzinfo=timezone.utc)
    period, since = homepage_period_since("today", now)
    assert period == "today"
    assert since == "2026-08-17T00:00:00+00:00"


def test_homepage_period_30d_uses_rolling_30_days():
    now = datetime(2026, 8, 17, 18, 30, tzinfo=timezone.utc)
    period, since = homepage_period_since("30d", now)
    assert period == "30d"
    assert since == "2026-07-18T18:30:00+00:00"


def test_invalid_homepage_period_falls_back_to_alltime():
    period, since = homepage_period_since("week")
    assert period == "alltime"
    assert since is None
