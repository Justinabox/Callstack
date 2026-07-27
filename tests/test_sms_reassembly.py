"""Tests for the pure in-memory multipart SMS reassembly accumulator."""

import pytest

from callstack.sms.pdu import MultipartInfo
from callstack.sms.reassembly import MultipartAccumulator


def test_reassembles_8bit_parts_out_of_order():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info_seq2 = MultipartInfo(reference=1, total_parts=2, sequence=2)
    info_seq1 = MultipartInfo(reference=1, total_parts=2, sequence=1)

    assert acc.add_part("+1555", info_seq2, "World", now=0) is None
    result = acc.add_part("+1555", info_seq1, "Hello", now=1)

    assert result == "HelloWorld"
    assert acc.pending_group_count == 0


def test_16bit_and_8bit_groups_with_same_reference_do_not_collide():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info_8bit_seq1 = MultipartInfo(reference=5, total_parts=2, sequence=1, is_16bit=False)
    info_16bit_seq1 = MultipartInfo(reference=5, total_parts=2, sequence=1, is_16bit=True)

    assert acc.add_part("+1555", info_8bit_seq1, "A", now=0) is None
    assert acc.add_part("+1555", info_16bit_seq1, "B", now=0) is None
    assert acc.pending_group_count == 2

    info_8bit_seq2 = MultipartInfo(reference=5, total_parts=2, sequence=2, is_16bit=False)
    result_8bit = acc.add_part("+1555", info_8bit_seq2, "C", now=0)
    assert result_8bit == "AC"

    info_16bit_seq2 = MultipartInfo(reference=5, total_parts=2, sequence=2, is_16bit=True)
    result_16bit = acc.add_part("+1555", info_16bit_seq2, "D", now=0)
    assert result_16bit == "BD"


def test_duplicate_sequence_before_completion_keeps_first_fragment():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info_seq1 = MultipartInfo(reference=2, total_parts=2, sequence=1)

    assert acc.add_part("+1555", info_seq1, "First", now=0) is None
    assert acc.add_part("+1555", info_seq1, "Second", now=1) is None

    info_seq2 = MultipartInfo(reference=2, total_parts=2, sequence=2)
    result = acc.add_part("+1555", info_seq2, "Tail", now=2)

    assert result == "FirstTail"


def test_incomplete_group_expires_after_max_age():
    acc = MultipartAccumulator(max_age=10, max_groups=10)
    info_seq1 = MultipartInfo(reference=3, total_parts=2, sequence=1)

    assert acc.add_part("+1555", info_seq1, "Stale", now=0) is None
    removed = acc.expire(now=11)

    assert removed == 1
    assert acc.pending_group_count == 0

    info_seq2 = MultipartInfo(reference=3, total_parts=2, sequence=2)
    assert acc.add_part("+1555", info_seq2, "New", now=11) is None
    assert acc.pending_group_count == 1


def test_oldest_incomplete_group_is_evicted_when_bound_exceeded():
    acc = MultipartAccumulator(max_age=1000, max_groups=2)
    info_a = MultipartInfo(reference=10, total_parts=2, sequence=1)
    info_b = MultipartInfo(reference=11, total_parts=2, sequence=1)
    info_c = MultipartInfo(reference=12, total_parts=2, sequence=1)

    assert acc.add_part("+1555", info_a, "A", now=0) is None
    assert acc.add_part("+1555", info_b, "B", now=1) is None
    assert acc.pending_group_count == 2

    # third distinct group exceeds the bound; oldest (reference=10) is evicted
    assert acc.add_part("+1555", info_c, "C", now=2) is None
    assert acc.pending_group_count == 2

    info_b_seq2 = MultipartInfo(reference=11, total_parts=2, sequence=2)
    assert acc.add_part("+1555", info_b_seq2, "B2", now=3) == "BB2"

    info_c_seq2 = MultipartInfo(reference=12, total_parts=2, sequence=2)
    assert acc.add_part("+1555", info_c_seq2, "C2", now=4) == "CC2"

    # reference=10's original fragment is gone: completing it now starts a fresh group
    info_a_seq2 = MultipartInfo(reference=10, total_parts=2, sequence=2)
    assert acc.add_part("+1555", info_a_seq2, "A2", now=5) is None


def test_invalid_multipart_info_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=1, total_parts=0, sequence=1)

    with pytest.raises(ValueError):
        acc.add_part("+1555", bad_info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_invalid_sequence_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=1, total_parts=2, sequence=3)

    with pytest.raises(ValueError):
        acc.add_part("+1555", bad_info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_invalid_is_16bit_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=1, total_parts=2, sequence=1, is_16bit="yes")

    with pytest.raises(ValueError):
        acc.add_part("+1555", bad_info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_invalid_now_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info = MultipartInfo(reference=1, total_parts=2, sequence=1)

    with pytest.raises(ValueError):
        acc.add_part("+1555", info, "Body", now=float("nan"))

    assert acc.pending_group_count == 0


def test_invalid_max_age_configuration_raises():
    with pytest.raises(ValueError):
        MultipartAccumulator(max_age=0, max_groups=10)


def test_invalid_max_groups_configuration_raises():
    with pytest.raises(ValueError):
        MultipartAccumulator(max_age=60, max_groups=True)


def test_different_senders_with_same_group_key_do_not_mix():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info_seq1 = MultipartInfo(reference=7, total_parts=2, sequence=1)
    info_seq2 = MultipartInfo(reference=7, total_parts=2, sequence=2)

    assert acc.add_part("+1555", info_seq1, "Alice1", now=0) is None
    assert acc.add_part("+1666", info_seq1, "Bob1", now=0) is None
    assert acc.pending_group_count == 2

    result_alice = acc.add_part("+1555", info_seq2, "Alice2", now=1)
    assert result_alice == "Alice1Alice2"
    assert acc.pending_group_count == 1

    result_bob = acc.add_part("+1666", info_seq2, "Bob2", now=2)
    assert result_bob == "Bob1Bob2"
    assert acc.pending_group_count == 0


def test_bool_reference_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=True, total_parts=2, sequence=1)

    with pytest.raises(ValueError):
        acc.add_part("+1555", bad_info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_bool_total_parts_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=1, total_parts=True, sequence=1)

    with pytest.raises(ValueError):
        acc.add_part("+1555", bad_info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_bool_sequence_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=1, total_parts=2, sequence=True)

    with pytest.raises(ValueError):
        acc.add_part("+1555", bad_info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_max_age_true_raises():
    with pytest.raises(ValueError):
        MultipartAccumulator(max_age=True, max_groups=10)


def test_max_age_nan_raises():
    with pytest.raises(ValueError):
        MultipartAccumulator(max_age=float("nan"), max_groups=10)


def test_max_age_inf_raises():
    with pytest.raises(ValueError):
        MultipartAccumulator(max_age=float("inf"), max_groups=10)


def test_max_groups_negative_raises():
    with pytest.raises(ValueError):
        MultipartAccumulator(max_age=60, max_groups=-1)


def test_max_groups_float_raises():
    with pytest.raises(ValueError):
        MultipartAccumulator(max_age=60, max_groups=2.5)


def test_error_messages_do_not_leak_sender_or_body():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=1, total_parts=0, sequence=1)

    with pytest.raises(ValueError) as exc_info:
        acc.add_part("secret-sender", bad_info, "secret-body", now=0)

    message = str(exc_info.value)
    assert "secret-sender" not in message
    assert "secret-body" not in message


def test_repr_does_not_leak_sender_or_body():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info = MultipartInfo(reference=1, total_parts=2, sequence=1)
    acc.add_part("secret-sender", info, "secret-body", now=0)

    text = repr(acc)

    assert "secret-sender" not in text
    assert "secret-body" not in text


def test_total_parts_exceeding_protocol_max_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    bad_info = MultipartInfo(reference=1, total_parts=256, sequence=1)

    with pytest.raises(ValueError):
        acc.add_part("+1555", bad_info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_non_multipart_info_metadata_raises_value_error_not_attribute_error():
    acc = MultipartAccumulator(max_age=60, max_groups=10)

    with pytest.raises(ValueError):
        acc.add_part("+1555", object(), "Body", now=0)

    assert acc.pending_group_count == 0


def test_non_str_sender_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info = MultipartInfo(reference=1, total_parts=2, sequence=1)

    with pytest.raises(ValueError):
        acc.add_part(12345, info, "Body", now=0)

    assert acc.pending_group_count == 0


def test_non_str_body_raises_without_mutating_state():
    acc = MultipartAccumulator(max_age=60, max_groups=10)
    info = MultipartInfo(reference=1, total_parts=2, sequence=1)

    with pytest.raises(ValueError):
        acc.add_part("+1555", info, 12345, now=0)

    assert acc.pending_group_count == 0


def test_pending_group_repr_does_not_leak_sender_or_body():
    from callstack.sms.reassembly import _PendingGroup

    group = _PendingGroup(total_parts=2, first_seen=0.0, parts={1: "secret-body"})

    text = repr(group)

    assert "secret-body" not in text
