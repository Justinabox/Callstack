"""Tests for SMS data types."""

from callstack.sms.types import SMS, DeliveryReport, SMSStatus


def test_sms_defaults():
    sms = SMS()
    assert sms.sender == ""
    assert sms.recipient == ""
    assert sms.body == ""
    assert sms.id is None
    assert sms.storage_index is None


def test_sms_is_incoming():
    assert SMS(status="unread").is_incoming
    assert SMS(status="read").is_incoming
    assert SMS(status="REC UNREAD").is_incoming
    assert SMS(status="REC READ").is_incoming
    assert not SMS(status="sent").is_incoming
    assert not SMS(status="STO SENT").is_incoming


def test_sms_status_enum():
    assert SMSStatus.UNREAD.value == "REC UNREAD"
    assert SMSStatus.ALL.value == "ALL"


def test_delivery_report_defaults():
    dr = DeliveryReport()
    assert dr.reference == 0
    assert dr.status == ""
