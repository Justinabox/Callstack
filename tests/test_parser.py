"""Tests for the AT response parser."""

from callstack.protocol.parser import ATResponseParser


def test_parse_signal_quality():
    result = ATResponseParser.parse_signal_quality("+CSQ: 20,0")
    assert result == (20, 0)


def test_parse_signal_quality_accepts_optional_comma_whitespace():
    result = ATResponseParser.parse_signal_quality("+CSQ: 20, 0")
    assert result == (20, 0)


def test_parse_signal_quality_no_signal():
    result = ATResponseParser.parse_signal_quality("+CSQ: 99,99")
    assert result == (99, 99)


def test_parse_signal_quality_invalid():
    result = ATResponseParser.parse_signal_quality("OK")
    assert result is None


def test_parse_registration():
    result = ATResponseParser.parse_registration("+CREG: 0,1")
    assert result == (0, 1)


def test_parse_registration_accepts_optional_comma_whitespace():
    result = ATResponseParser.parse_registration("+CREG: 0, 1")
    assert result == (0, 1)


def test_parse_registration_roaming():
    result = ATResponseParser.parse_registration("+CREG: 0,5")
    assert result == (0, 5)


def test_parse_packet_registration_roaming():
    result = ATResponseParser.parse_registration("+CGREG: 0,5")
    assert result == (0, 5)


def test_parse_lte_registration_home():
    result = ATResponseParser.parse_registration("+CEREG: 0,1")
    assert result == (0, 1)


def test_parse_registration_one_field_home():
    result = ATResponseParser.parse_registration("+CREG: 1")
    assert result == (0, 1)


def test_parse_packet_registration_one_field_roaming():
    result = ATResponseParser.parse_registration("+CGREG: 5")
    assert result == (0, 5)


def test_parse_lte_registration_one_field_searching():
    result = ATResponseParser.parse_registration("+CEREG: 2")
    assert result == (0, 2)


def test_parse_registration_rejects_malformed_trailing_fields():
    assert ATResponseParser.parse_registration("+CREG: 1,foo") is None
    assert ATResponseParser.parse_registration("+CGREG: 5,bar") is None
    assert ATResponseParser.parse_registration("+CREG: 0,1x") is None
    assert ATResponseParser.parse_registration("+CREG: 0,1,garbage") is None
    assert ATResponseParser.parse_registration("+CREG: 0,1,2x") is None
    assert ATResponseParser.parse_registration('+CEREG: 2,1,"zzzz"') is None


def test_parse_registration_accepts_verbose_lte_fields():
    result = ATResponseParser.parse_registration('+CEREG: 2,1,"ABCD","12345678",7')
    assert result == (2, 1)


def test_parse_registration_accepts_spaced_verbose_fields():
    assert (
        ATResponseParser.parse_registration('+CGREG: 2, 5, "ABCD", "12345678", 7')
        == (2, 5)
    )
    assert (
        ATResponseParser.parse_registration('+CEREG: 2, 1, "ABCD", "12345678", 7, ,')
        == (2, 1)
    )


def test_parse_registration_accepts_verbose_lte_empty_optional_fields():
    assert ATResponseParser.parse_registration('+CEREG: 2,1,"ABCD","12345678",7,,') == (2, 1)
    assert (
        ATResponseParser.parse_registration(
            '+CEREG: 2,1,"ABCD","12345678",7,,,"00000001","00000110"'
        )
        == (2, 1)
    )


def test_parse_registration_accepts_verbose_packet_empty_optional_fields():
    result = ATResponseParser.parse_registration('+CGREG: 2,5,"ABCD","12345678",7,,')
    assert result == (2, 5)


def test_parse_registration_rejects_malformed_leading_fields_before_verbose_tail():
    assert ATResponseParser.parse_registration('+CEREG: 2,foo,"ABCD","12345678",7,,') is None


def test_parse_registration_rejects_empty_only_verbose_tail():
    assert ATResponseParser.parse_registration("+CREG: 0,1,") is None
    assert ATResponseParser.parse_registration("+CREG: 0,1,,,") is None


def test_parse_clip():
    result = ATResponseParser.parse_clip('+CLIP: "+155****4567",145,,,,0')
    assert result == "+155****4567"


def test_parse_clip_no_number():
    result = ATResponseParser.parse_clip('+CLIP: "",128')
    assert result == ""


def test_parse_cmgs():
    result = ATResponseParser.parse_cmgs("+CMGS: 42")
    assert result == 42


def test_parse_cmti():
    result = ATResponseParser.parse_cmti('+CMTI: "SM",3')
    assert result == ("SM", 3)


def test_parse_cmti_accepts_optional_comma_whitespace():
    result = ATResponseParser.parse_cmti('+CMTI: "SM", 3')
    assert result == ("SM", 3)


def test_parse_cmt():
    result = ATResponseParser.parse_cmt('+CMT: "+15551234567","","2024/01/15,10:30:00+00"')
    assert result == "+15551234567"


def test_parse_prefix():
    result = ATResponseParser.parse_prefix("+CSQ: 20,0")
    assert result is not None
    assert result.command == "CSQ"
    assert result.raw == "20,0"


def test_parse_prefix_no_match():
    result = ATResponseParser.parse_prefix("OK")
    assert result is None
