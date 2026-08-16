from app import calculate_network_delta


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
