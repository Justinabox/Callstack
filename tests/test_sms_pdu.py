"""Tests for PDU encoder/decoder."""

import pytest

from callstack.sms.pdu import PDUEncoder, PDUDecoder, GSM7_BASIC


def _submit_pdu_fields(pdu: str) -> dict:
    """Return structural SMS-SUBMIT fields needed by multipart tests."""
    pos = 0
    sca_octets = int(pdu[pos:pos + 2], 16)
    pos += 2 + (sca_octets * 2)
    pdu_type = int(pdu[pos:pos + 2], 16)
    pos += 2
    pos += 2  # MR
    address_digits = int(pdu[pos:pos + 2], 16)
    pos += 2
    pos += 2  # TOA
    pos += (address_digits + (address_digits % 2))
    pos += 2  # PID
    pos += 2  # DCS
    pos += 2  # VP
    user_data_length = int(pdu[pos:pos + 2], 16)
    pos += 2
    user_data = bytes.fromhex(pdu[pos:])
    return {
        "pdu_type": pdu_type,
        "user_data_length": user_data_length,
        "user_data": user_data,
    }



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

    def test_roundtrip_extension_table_characters(self):
        text = "Braces {}[] cost €5 ~^|\\"
        packed, count = PDUEncoder.encode_gsm7(text)

        decoded = PDUDecoder.decode_gsm7(packed, count)

        assert decoded == text
        assert count == len(text) + sum(1 for ch in text if ch in "{}[]€~^|\\")

    def test_nbsp_falls_back_without_becoming_extension_escape(self):
        packed, count = PDUEncoder.encode_gsm7("\xa0(")

        decoded = PDUDecoder.decode_gsm7(packed, count)

        assert decoded == "?("

    def test_full_alphabet_coverage(self):
        # Ensure every GSM7 char survives roundtrip
        for i, ch in enumerate(GSM7_BASIC):
            packed, count = PDUEncoder.encode_gsm7(ch)
            decoded = PDUDecoder.decode_gsm7(packed, count)
            assert decoded == ch, f"Failed for char {i}: {repr(ch)}"


class TestMultipartUDH:
    def test_parse_8bit_concatenation_header(self):
        info = PDUDecoder.parse_concatenation_udh(bytes.fromhex("0500037A0201"))
        assert info is not None
        assert info.reference == 0x7A
        assert info.total_parts == 2
        assert info.sequence == 1
        assert info.is_16bit is False

    def test_parse_16bit_concatenation_header(self):
        info = PDUDecoder.parse_concatenation_udh(bytes.fromhex("06080412340302"))
        assert info is not None
        assert info.reference == 0x1234
        assert info.total_parts == 3
        assert info.sequence == 2
        assert info.is_16bit is True

    def test_parse_concatenation_header_ignores_unrelated_udh(self):
        assert PDUDecoder.parse_concatenation_udh(bytes.fromhex("03010A0B")) is None


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

    def test_submit_pdu_length_counts_full_tpdu_after_sca(self):
        for body in ("Hi", "Hello World"):
            pdu, length = PDUEncoder.build_submit_pdu("5551234", body)
            sca_octets = int(pdu[:2], 16)
            expected_tpdu_octets = (len(pdu) // 2) - (1 + sca_octets)
            assert length == expected_tpdu_octets

    def test_build_submit_pdu_rejects_ucs2_required_text_before_encoding(self):
        with pytest.raises(ValueError, match="GSM 03.38") as excinfo:
            PDUEncoder.build_submit_pdu("5550100", "OTP 漢")

        assert "OTP" not in str(excinfo.value)
        assert "漢" not in str(excinfo.value)

    def test_build_submit_pdu_preserves_gsm7_extension_table_characters(self):
        body = "Braces {}[] cost €5 ~^|\\"

        pdu, _ = PDUEncoder.build_submit_pdu("5550100", body)
        fields = _submit_pdu_fields(pdu)
        decoded = PDUDecoder.decode_gsm7(
            fields["user_data"], fields["user_data_length"]
        )

        assert decoded == body

    @pytest.mark.parametrize(
        "recipient",
        [
            "bad\"number",
            "++123",
            "12+34",
            "*123#",
            "+123\n",
            "+123\r",
            "+12",
            "+1234567890123456",
        ],
    )
    def test_build_submit_pdu_rejects_invalid_recipient_before_encoding(self, recipient):
        with pytest.raises(ValueError, match="Invalid SMS recipient"):
            PDUEncoder.build_submit_pdu(recipient, "Hi")


class TestMultipartSubmitPDU:
    def test_build_multipart_submit_pdus_uses_16bit_udh_for_each_segment(self):
        segments = PDUEncoder.build_multipart_submit_pdus(
            "5550100", "A" * 161, reference=0x1234
        )

        assert [segment.sequence for segment in segments] == [1, 2]
        assert {segment.total_parts for segment in segments} == {2}
        assert {segment.reference for segment in segments} == {0x1234}
        assert "".join(segment.body for segment in segments) == "A" * 161
        assert [segment.body for segment in segments] == ["A" * 152, "A" * 9]

        for segment in segments:
            fields = _submit_pdu_fields(segment.pdu)
            assert fields["pdu_type"] & 0x40  # UDHI set
            assert fields["user_data_length"] <= 160
            udh_octets = fields["user_data"][0] + 1
            info = PDUDecoder.parse_concatenation_udh(
                fields["user_data"][:udh_octets]
            )
            assert info is not None
            assert info.is_16bit is True
            assert info.reference == 0x1234
            assert info.total_parts == 2
            assert info.sequence == segment.sequence
            sca_octets = int(segment.pdu[:2], 16)
            expected_tpdu_octets = (len(segment.pdu) // 2) - (1 + sca_octets)
            assert segment.tpdu_length == expected_tpdu_octets

    def test_build_multipart_submit_pdus_never_splits_gsm7_extension_pairs(self):
        body = ("A" * 151) + "€" + ("B" * 10)

        segments = PDUEncoder.build_multipart_submit_pdus(
            "5550100", body, reference=0xBEEF
        )

        assert [segment.body for segment in segments] == ["A" * 151, "€" + ("B" * 10)]
        assert [segment.payload_septets for segment in segments] == [151, 12]

    def test_build_multipart_submit_pdus_rejects_ucs2_required_text_before_encoding(self):
        with pytest.raises(ValueError, match="GSM 03.38"):
            PDUEncoder.build_multipart_submit_pdus(
                "5550100", "A" * 160 + "漢", reference=0x1234
            )

    def test_build_multipart_submit_pdus_rejects_messages_over_255_segments(self):
        body = "A" * ((152 * 255) + 1)

        with pytest.raises(ValueError, match="at most 255 segments"):
            PDUEncoder.build_multipart_submit_pdus(
                "5550100", body, reference=0x1234
            )


class TestDeliverPDU:
    @staticmethod
    def _deliver_pdu(sender: str = "DUO", body: str = "Hi", toa: int = 0xD0) -> str:
        sender_packed, sender_len = PDUEncoder.encode_gsm7(sender)
        body_packed, body_len = PDUEncoder.encode_gsm7(body)
        return (
            "00"  # SCA: use default SMSC
            "04"  # SMS-DELIVER
            f"{sender_len:02X}"
            f"{toa:02X}"
            f"{sender_packed.hex().upper()}"
            "00"  # PID
            "00"  # DCS: GSM 7-bit default alphabet
            "42215241030040"  # SCTS
            f"{body_len:02X}"
            f"{body_packed.hex().upper()}"
        )

    @staticmethod
    def _numeric_deliver_pdu(
        sender: str = "5550123",
        body: str = "Hi",
        toa: int = 0x81,
        sender_encoded: str | None = None,
        sender_len: int | None = None,
    ) -> str:
        if sender_encoded is None:
            sender_encoded, _toa = PDUEncoder.encode_phone_number(sender)
        if sender_len is None:
            sender_len = len(sender.lstrip("+"))
        body_packed, body_len = PDUEncoder.encode_gsm7(body)
        return (
            "00"  # SCA: use default SMSC
            "04"  # SMS-DELIVER
            f"{sender_len:02X}"
            f"{toa:02X}"
            f"{sender_encoded}"
            "00"  # PID
            "00"  # DCS: GSM 7-bit default alphabet
            "42215241030040"  # SCTS
            f"{body_len:02X}"
            f"{body_packed.hex().upper()}"
        )

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

    def test_decode_deliver_pdu_preserves_alphanumeric_sender_and_body(self):
        decoded = PDUDecoder.decode_deliver_pdu(self._deliver_pdu())

        assert decoded is not None
        assert decoded["sender"] == "DUO"
        assert decoded["body"] == "Hi"
        assert decoded["timestamp"] is not None

    @pytest.mark.parametrize("sender", ["ACME/OTP", "BANK#1", "O'HARE", "A+B"])
    def test_decode_deliver_pdu_preserves_valid_gsm7_alphanumeric_sender_punctuation(self, sender):
        decoded = PDUDecoder.decode_deliver_pdu(self._deliver_pdu(sender=sender))

        assert decoded is not None
        assert decoded["sender"] == sender
        assert decoded["body"] == "Hi"
        assert decoded["timestamp"] is not None

    def test_decode_deliver_pdu_preserves_numeric_sender(self):
        sender = "+15551230000"
        sender_encoded, _toa = PDUEncoder.encode_phone_number(sender)
        body_packed, body_len = PDUEncoder.encode_gsm7("Hi")
        pdu = (
            "00"
            "04"
            f"{len(sender.lstrip('+')):02X}"
            "91"
            f"{sender_encoded}"
            "00"
            "00"
            "42215241030040"
            f"{body_len:02X}"
            f"{body_packed.hex().upper()}"
        )

        decoded = PDUDecoder.decode_deliver_pdu(pdu)

        assert decoded is not None
        assert decoded["sender"] == sender
        assert decoded["body"] == "Hi"
        assert decoded["timestamp"] is not None

    def test_decode_deliver_pdu_preserves_odd_length_numeric_sender(self):
        decoded = PDUDecoder.decode_deliver_pdu(
            self._numeric_deliver_pdu(sender="5550123")
        )

        assert decoded is not None
        assert decoded["sender"] == "5550123"
        assert decoded["body"] == "Hi"
        assert decoded["timestamp"] is not None

    @pytest.mark.parametrize(
        "sender_encoded,sender_len",
        [
            ("21A3B5F7", 7),  # A/B are never valid numeric BCD digits.
            ("21F365", 6),  # F padding is not valid inside an even-length address.
            ("１２F３", 3),  # Unicode decimal characters are not ASCII BCD nibbles.
        ],
    )
    def test_decode_deliver_pdu_rejects_malformed_numeric_sender_bcd(
        self, sender_encoded, sender_len
    ):
        pdu = self._numeric_deliver_pdu(
            sender_encoded=sender_encoded,
            sender_len=sender_len,
        )

        assert PDUDecoder.decode_deliver_pdu(pdu) is None

    def test_decode_deliver_pdu_rejects_numeric_sender_with_extra_f_padding(self):
        pdu = self._numeric_deliver_pdu(
            sender_encoded="21F3F5",
            sender_len=5,
        )

        assert PDUDecoder.decode_deliver_pdu(pdu) is None

    def test_decode_deliver_pdu_rejects_truncated_alphanumeric_sender(self):
        sender_packed, sender_len = PDUEncoder.encode_gsm7("DUO")
        body_packed, body_len = PDUEncoder.encode_gsm7("Hi")
        pdu = (
            "00"
            "04"
            f"{sender_len:02X}"
            "D0"
            f"{sender_packed[:-1].hex().upper()}"  # Missing final sender octet.
            "00"
            "00"
            "42215241030040"
            f"{body_len:02X}"
            f"{body_packed.hex().upper()}"
        )

        assert PDUDecoder.decode_deliver_pdu(pdu) is None

    def test_decode_deliver_pdu_rejects_truncated_alphanumeric_sender_with_shifted_valid_timestamp(self):
        pdu = (
            "00"
            "04"
            "03"
            "D0"
            "C4EA"  # Missing final sender octet; following PID byte can be stolen.
            "00"
            "00"
            "42105021010200"  # If shifted, this can still parse as a timestamp.
            "02"
            "C834"
        )

        assert PDUDecoder.decode_deliver_pdu(pdu) is None

    def test_decode_deliver_pdu_rejects_truncated_alphanumeric_sender_with_shifted_empty_body(self):
        pdu = (
            "00"
            "04"
            "03"
            "D0"
            "C4EA"  # Missing final sender octet; following PID byte can be stolen.
            "00"
            "00"
            "00105021010200"  # If shifted, this can still parse as a timestamp.
            "01"
            "00"
        )

        assert PDUDecoder.decode_deliver_pdu(pdu) is None
