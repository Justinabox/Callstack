from callstack.webhooks import webhook_signature


def test_webhook_signature_uses_timestamp_and_exact_raw_payload_bytes():
    secret = "key"
    timestamp = "1700000000"
    payload = b"The quick brown fox jumps over the lazy dog"
    expected = "sha256=2f658d6aef4f246e91cd741bbcded7479e9605f9d41c9e248122a117e0e1765b"

    assert webhook_signature(secret, timestamp, payload) == expected
    assert webhook_signature(secret, timestamp, payload[:-1] + b"!") != expected


def test_webhook_signature_preserves_binary_payload_bytes():
    secret = "secrēt"
    timestamp = "1700000001"
    payload = b"\x00\xff\r\n"
    expected = "sha256=e1be73a32e9c015fb89017b34427a16057f5f00b19718447b2c82054d59ad104"

    assert webhook_signature(secret, timestamp, payload) == expected
