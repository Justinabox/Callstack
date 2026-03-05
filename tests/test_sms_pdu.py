"""Tests for PDU encoder/decoder."""

from callstack.sms.pdu import PDUEncoder, PDUDecoder, GSM7_BASIC


class TestGSM7Encoding:
    def test_encode_hello(self):
        packed, count = PDUEncoder.encode_gsm7("Hello")
        assert count == 5
        assert len(packed) > 0

    def test_roundtrip(self):
        text = "Hello World"
        packed, count = PDUEncoder.encode_gsm7(text)
        decoded = PDUDecoder.decode_gsm7(packed, count)
        assert decoded == text

    def test_roundtrip_numbers(self):
        text = "12345"
        packed, count = PDUEncoder.encode_gsm7(text)
        decoded = PDUDecoder.decode_gsm7(packed, count)
        assert decoded == text

    def test_roundtrip_special(self):
        text = "Test @$!"
        packed, count = PDUEncoder.encode_gsm7(text)
        decoded = PDUDecoder.decode_gsm7(packed, count)
        assert decoded == text

    def test_full_alphabet_coverage(self):
        # Ensure every GSM7 char survives roundtrip
        for i, ch in enumerate(GSM7_BASIC):
            packed, count = PDUEncoder.encode_gsm7(ch)
            decoded = PDUDecoder.decode_gsm7(packed, count)
            assert decoded == ch, f"Failed for char {i}: {repr(ch)}"


class TestPhoneNumber:
    def test_encode_international(self):
        encoded, toa = PDUEncoder.encode_phone_number("+1234567890")
        assert toa == 0x91
        assert encoded == "2143658709"

    def test_encode_national(self):
        encoded, toa = PDUEncoder.encode_phone_number("1234567890")
        assert toa == 0x81

    def test_encode_odd_length(self):
        encoded, toa = PDUEncoder.encode_phone_number("+12345")
        # 5 digits -> padded to 6 -> "214365" with F -> "2143F5"
        assert "F" in encoded or "f" in encoded.lower()

    def test_decode_international(self):
        number = PDUEncoder.decode_phone_number("2143658709", 0x91)
        assert number == "+1234567890"

    def test_roundtrip(self):
        original = "+447911123456"
        encoded, toa = PDUEncoder.encode_phone_number(original)
        decoded = PDUEncoder.decode_phone_number(encoded, toa)
        assert decoded == original


class TestSubmitPDU:
    def test_build_returns_hex_and_length(self):
        pdu, length = PDUEncoder.build_submit_pdu("+1555", "Hi")
        assert isinstance(pdu, str)
        assert isinstance(length, int)
        assert length > 0
        # PDU should be valid hex
        int(pdu, 16)

    def test_pdu_starts_with_sca(self):
        pdu, _ = PDUEncoder.build_submit_pdu("+1555", "Test")
        # Default SCA length = 00
        assert pdu.startswith("00")


class TestDeliverPDU:
    def test_decode_timestamp(self):
        # 24/12/25 14:30:00 +04 (quarter hours)
        # Swapped BCD: 42 21 52 41 03 00 40
        ts = PDUDecoder.decode_timestamp("42215241030040")
        assert ts is not None
        assert ts.year == 2024
        assert ts.month == 12

    def test_decode_timestamp_invalid(self):
        assert PDUDecoder.decode_timestamp("") is None
        assert PDUDecoder.decode_timestamp("short") is None
