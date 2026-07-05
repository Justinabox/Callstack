"""Public package API export tests."""

import callstack
from callstack.errors import SMSPersistenceError as DirectSMSPersistenceError


def test_top_level_exports_sms_persistence_error():
    """Partial-success SMS errors are catchable from the public API."""
    from callstack import SMSPersistenceError

    assert SMSPersistenceError is DirectSMSPersistenceError
    assert "SMSPersistenceError" in callstack.__all__
