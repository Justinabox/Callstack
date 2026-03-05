"""Tests for signal quality utilities."""

from callstack.utils.signal_quality import (
    rssi_to_dbm,
    rssi_to_description,
    ber_to_description,
)


class TestRssiToDbm:
    def test_zero(self):
        assert rssi_to_dbm(0) == -113

    def test_mid(self):
        assert rssi_to_dbm(15) == -83

    def test_max(self):
        assert rssi_to_dbm(31) == -51

    def test_unknown(self):
        assert rssi_to_dbm(99) is None


class TestRssiToDescription:
    def test_excellent(self):
        assert rssi_to_description(25) == "excellent"  # -63 dBm

    def test_good(self):
        assert rssi_to_description(18) == "good"  # -77 dBm

    def test_fair(self):
        assert rssi_to_description(10) == "fair"  # -93 dBm

    def test_poor(self):
        assert rssi_to_description(3) == "poor"  # -107 dBm

    def test_unknown(self):
        assert rssi_to_description(99) == "unknown"


class TestBerToDescription:
    def test_excellent(self):
        assert ber_to_description(0) == "excellent"
        assert ber_to_description(1) == "excellent"

    def test_good(self):
        assert ber_to_description(2) == "good"
        assert ber_to_description(3) == "good"

    def test_fair(self):
        assert ber_to_description(4) == "fair"
        assert ber_to_description(5) == "fair"

    def test_poor(self):
        assert ber_to_description(6) == "poor"
        assert ber_to_description(7) == "poor"

    def test_unknown(self):
        assert ber_to_description(99) == "unknown"
