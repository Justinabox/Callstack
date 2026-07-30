"""Pure, in-memory accumulator for reassembling inbound multipart SMS."""

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from callstack.sms.pdu import MultipartInfo

_GroupKey = tuple[str, int, int, bool]


def _validate_real_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-bool real number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _validate_max_age(max_age: object) -> float:
    value = _validate_real_number(max_age, "max_age")
    if value <= 0:
        raise ValueError("max_age must be positive")
    return value


def _validate_max_groups(max_groups: object) -> int:
    if isinstance(max_groups, bool) or not isinstance(max_groups, int):
        raise ValueError("max_groups must be a non-bool int")
    if max_groups <= 0:
        raise ValueError("max_groups must be positive")
    return max_groups


_MAX_TOTAL_PARTS = 255  # 3GPP TS 23.040 concatenation total-parts is a single octet


def _validate_multipart_info(info: MultipartInfo) -> None:
    if not isinstance(info, MultipartInfo):
        raise ValueError("info must be a MultipartInfo")
    for name in ("reference", "total_parts", "sequence"):
        value = getattr(info, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"multipart info field {name!r} must be a non-bool int")
    if not 1 <= info.total_parts <= _MAX_TOTAL_PARTS:
        raise ValueError(f"multipart info total_parts must be between 1 and {_MAX_TOTAL_PARTS}")
    if not 1 <= info.sequence <= info.total_parts:
        raise ValueError("multipart info sequence out of range")
    if not isinstance(info.is_16bit, bool):
        raise ValueError("multipart info field 'is_16bit' must be a bool")
    max_reference = 65535 if info.is_16bit else 255
    if not 0 <= info.reference <= max_reference:
        raise ValueError("multipart info reference out of range")


@dataclass
class _PendingGroup:
    total_parts: int
    first_seen: float
    parts: dict[int, str] = field(default_factory=dict)
    segment_identities: dict[int, bytes] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"_PendingGroup(total_parts={self.total_parts!r}, "
            f"first_seen={self.first_seen!r}, received={len(self.parts)})"
        )


@dataclass(frozen=True)
class CompletedMultipart:
    """Reassembled payload and opaque per-segment identities."""

    body: str
    segment_identities: tuple[bytes, ...]

    def __repr__(self) -> str:
        return f"CompletedMultipart(part_count={len(self.segment_identities)})"


class MultipartAccumulator:
    """Pure, in-memory accumulator for inbound concatenated SMS fragments.

    Groups fragments by sender and concatenation metadata so that 8-bit and
    16-bit references never collide, then releases the joined body once every
    sequence number from 1..total_parts has
    arrived. Callers supply a monotonic ``now`` value on every call; this
    class never reads the wall clock.
    """

    def __init__(self, *, max_age: float, max_groups: int):
        self._max_age = _validate_max_age(max_age)
        self._max_groups = _validate_max_groups(max_groups)
        self._pending: "OrderedDict[_GroupKey, _PendingGroup]" = OrderedDict()

    @property
    def pending_group_count(self) -> int:
        """Number of incomplete groups currently held in memory."""
        return len(self._pending)

    def add_part(
        self,
        sender: str,
        info: MultipartInfo,
        body: str,
        now: float,
    ) -> Optional[str]:
        """Add one multipart fragment; return the joined body once complete.

        Returns None while the group is still incomplete. Fails closed with
        ValueError, without mutating any state, for invalid metadata or an
        invalid ``now`` value.

        Before admitting the fragment, this implicitly expires groups older
        than ``max_age`` as of ``now`` (same as calling ``expire(now)``), but
        the expired count is discarded. Call ``expire(now)`` directly if you
        need to observe how many groups were expired.
        """
        completed = self.add_part_with_identity(sender, info, body, b"", now)
        return None if completed is None else completed.body

    def add_part_with_identity(
        self,
        sender: str,
        info: MultipartInfo,
        body: str,
        segment_identity: bytes,
        now: float,
    ) -> Optional[CompletedMultipart]:
        """Add a part and return the completed body with opaque identities.

        The first identity observed for a sequence wins with its matching body,
        preserving duplicate-part behavior while giving callers a deterministic
        identity sequence after out-of-order completion.
        """
        if not isinstance(sender, str):
            raise ValueError("sender must be a string")
        if not isinstance(body, str):
            raise ValueError("body must be a string")
        if not isinstance(segment_identity, bytes):
            raise ValueError("segment_identity must be bytes")
        _validate_multipart_info(info)
        validated_now = _validate_real_number(now, "now")

        self._expire(validated_now)

        key: _GroupKey = (sender, info.reference, info.total_parts, info.is_16bit)
        group = self._pending.get(key)
        if group is None:
            if len(self._pending) >= self._max_groups:
                self._pending.popitem(last=False)
            group = _PendingGroup(total_parts=info.total_parts, first_seen=validated_now)
            self._pending[key] = group

        if info.sequence not in group.parts:
            group.parts[info.sequence] = body
            group.segment_identities[info.sequence] = segment_identity

        if len(group.parts) < group.total_parts:
            return None

        completed = CompletedMultipart(
            body="".join(group.parts[seq] for seq in range(1, group.total_parts + 1)),
            segment_identities=tuple(
                group.segment_identities[seq] for seq in range(1, group.total_parts + 1)
            ),
        )
        del self._pending[key]
        return completed

    def expire(self, now: float) -> int:
        """Remove incomplete groups older than max_age; return count removed."""
        validated_now = _validate_real_number(now, "now")
        return self._expire(validated_now)

    def _expire(self, now: float) -> int:
        expired_keys = [
            key for key, group in self._pending.items()
            if now - group.first_seen >= self._max_age
        ]
        for key in expired_keys:
            del self._pending[key]
        return len(expired_keys)

    def __repr__(self) -> str:
        return f"MultipartAccumulator(pending_group_count={len(self._pending)})"
