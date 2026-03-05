"""Tests for the AT response parser."""

from callstack.protocol.parser import ATResponseParser


def test_parse_signal_quality():
    result = ATResponseParser.parse_signal_quality("+CSQ: 20,0")
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


def test_parse_registration_roaming():
    result = ATResponseParser.parse_registration("+CREG: 0,5")
    assert result == (0, 5)


def test_parse_clip():
    result = ATResponseParser.parse_clip('+CLIP: "+15551234567",145,,,,0')
    assert result == "+15551234567"


def test_parse_clip_no_number():
    result = ATResponseParser.parse_clip('+CLIP: "",128')
    assert result == ""


def test_parse_cmgs():
    result = ATResponseParser.parse_cmgs("+CMGS: 42")
    assert result == 42


def test_parse_cmti():
    result = ATResponseParser.parse_cmti('+CMTI: "SM",3')
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
