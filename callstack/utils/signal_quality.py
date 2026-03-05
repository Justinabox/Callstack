"""RSSI/BER parsing and human-readable signal descriptions."""


def rssi_to_dbm(rssi: int) -> int | None:
    """Convert AT+CSQ RSSI value (0-31, 99) to dBm.

    Returns None for unknown (99).
    """
    if rssi == 99:
        return None
    return -113 + (2 * rssi)


def rssi_to_description(rssi: int) -> str:
    """Human-readable signal strength from RSSI value."""
    if rssi == 99:
        return "unknown"
    dbm = rssi_to_dbm(rssi)
    if dbm is None:
        return "unknown"
    if dbm >= -70:
        return "excellent"
    if dbm >= -85:
        return "good"
    if dbm >= -100:
        return "fair"
    return "poor"


def ber_to_description(ber: int) -> str:
    """Human-readable bit error rate from BER value (0-7, 99)."""
    if ber == 99:
        return "unknown"
    if ber <= 1:
        return "excellent"
    if ber <= 3:
        return "good"
    if ber <= 5:
        return "fair"
    return "poor"
