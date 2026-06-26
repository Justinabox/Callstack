"""PDU (Protocol Data Unit) encoder/decoder for SMS.

Handles GSM 7-bit default alphabet encoding/decoding and PDU frame
construction for modems operating in PDU mode (AT+CMGF=0).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


# GSM 7-bit default alphabet (3GPP TS 23.038)
GSM7_BASIC = (
    "@\xa3$\xa5\xe8\xe9\xf9\xec\xf2\xc7\n\xd8\xf8\r\xc5\xe5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e"
    "\x1b\xc6\xe6\xdf\xc9 !\"#\xa4%&'()*+,-./0123456789:;<=>?"
    "\xa1ABCDEFGHIJKLMNOPQRSTUVWXYZ\xc4\xd6\xd1\xdc\xa7"
    "\xbfabcdefghijklmnopqrstuvwxyz\xe4\xf6\xf1\xfc\xe0"
)

# GSM 03.38 extension table characters are encoded as ESC (0x1B)
# followed by the extension-table code.
GSM7_ESCAPE = 0x1B
GSM7_EXTENSION = {
    "\f": 0x0A,
    "^": 0x14,
    "{": 0x28,
    "}": 0x29,
    "\\": 0x2F,
    "[": 0x3C,
    "~": 0x3D,
    "]": 0x3E,
    "|": 0x40,
    "€": 0x65,
}

# Reverse lookup for encoding/decoding
_GSM7_ENCODE = {c: i for i, c in enumerate(GSM7_BASIC)}
_GSM7_EXTENSION_DECODE = {code: char for char, code in GSM7_EXTENSION.items()}
_SMS_RECIPIENT_RE = re.compile(r"\+?[0-9]{3,15}\Z")
_ALPHANUMERIC_ORIGINATOR_CHARS = frozenset(chr(code) for code in range(0x20, 0x7F))


def _validate_sms_recipient(recipient: str) -> str:
    """Validate a PDU-mode SMS destination address before semi-octet encoding."""
    if not isinstance(recipient, str) or not _SMS_RECIPIENT_RE.fullmatch(recipient):
        raise ValueError(
            f"Invalid SMS recipient: {recipient!r} "
            "(use optional leading + followed by 3-15 digits)"
        )
    return recipient


class PDUEncoder:
    """Encode SMS messages into PDU format."""

    @staticmethod
    def encode_phone_number(number: str) -> tuple[str, int]:
        """Encode a phone number for PDU.

        Returns (encoded_hex, type_of_address).
        """
        if number.startswith("+"):
            toa = 0x91  # International
            number = number[1:]
        else:
            toa = 0x81  # Unknown/national

        # Pad with F if odd length, then swap nibbles
        if len(number) % 2:
            number += "F"

        encoded = ""
        for i in range(0, len(number), 2):
            encoded += number[i + 1] + number[i]

        return encoded, toa

    @staticmethod
    def decode_phone_number(encoded: str, toa: int) -> str:
        """Decode a PDU phone number back to string."""
        # Swap nibbles back
        number = ""
        for i in range(0, len(encoded), 2):
            number += encoded[i + 1] + encoded[i]

        # Remove trailing F padding
        number = number.rstrip("Ff")

        if toa == 0x91:
            number = "+" + number
        return number

    @staticmethod
    def encode_gsm7(text: str) -> tuple[bytes, int]:
        """Encode text using GSM 7-bit alphabet, packed into septets.

        Returns (packed_bytes, septet_count).
        """
        septets = []
        for char in text:
            extension_code = GSM7_EXTENSION.get(char)
            if extension_code is not None:
                septets.extend((GSM7_ESCAPE, extension_code))
                continue

            code = _GSM7_ENCODE.get(char)
            if code is None:
                code = _GSM7_ENCODE.get("?", 0x3F)
            septets.append(code)

        # Pack 7-bit values into bytes
        packed = bytearray()
        bits = 0
        byte_val = 0
        for septet in septets:
            byte_val |= (septet << bits)
            bits += 7
            while bits >= 8:
                packed.append(byte_val & 0xFF)
                byte_val >>= 8
                bits -= 8
        if bits > 0:
            packed.append(byte_val & 0xFF)

        return bytes(packed), len(septets)

    @staticmethod
    def build_submit_pdu(recipient: str, body: str) -> tuple[str, int]:
        """Build a complete SMS-SUBMIT PDU.

        Returns (pdu_hex_string, tpdu_length) for use with AT+CMGS=<length>.
        """
        # SCA (Service Center Address) - use default (length=0)
        sca = "00"

        # PDU type: SMS-SUBMIT, relative VP, no SRR, no UDHI
        pdu_type = "31"

        # Message reference (let the modem assign)
        mr = "00"

        # Destination address
        recipient = _validate_sms_recipient(recipient)
        clean = recipient.lstrip("+")
        da_len = f"{len(clean):02X}"
        da_encoded, da_toa = PDUEncoder.encode_phone_number(recipient)
        da = da_len + f"{da_toa:02X}" + da_encoded

        # Protocol ID
        pid = "00"

        # Data coding scheme (GSM 7-bit)
        dcs = "00"

        # Validity period (relative, 1 day = 167)
        vp = "A7"

        # User data
        packed, septet_count = PDUEncoder.encode_gsm7(body)
        udl = f"{septet_count:02X}"
        ud = packed.hex().upper()

        tpdu = pdu_type + mr + da + pid + dcs + vp + udl + ud
        pdu = sca + tpdu

        # TPDU length is byte count of everything after SCA
        tpdu_len = len(tpdu) // 2

        return pdu, tpdu_len


@dataclass(frozen=True)
class MultipartInfo:
    """Concatenated SMS metadata parsed from a user-data header."""

    reference: int
    total_parts: int
    sequence: int
    is_16bit: bool = False


class PDUDecoder:
    """Decode SMS messages from PDU format."""

    @staticmethod
    def parse_concatenation_udh(udh: bytes) -> Optional[MultipartInfo]:
        """Parse concatenated SMS metadata from a user-data header.

        Accepts a complete UDH byte string including the leading UDHL byte.
        Supports both 8-bit (IEI 0x00) and 16-bit (IEI 0x08) concatenation
        reference formats from 3GPP TS 23.040. Returns None when the header
        is malformed or does not contain concatenation metadata.
        """
        if not udh:
            return None

        declared_len = udh[0]
        if len(udh) < declared_len + 1:
            return None

        end = 1 + declared_len
        pos = 1
        while pos + 2 <= end:
            iei = udh[pos]
            ie_len = udh[pos + 1]
            pos += 2
            ie_end = pos + ie_len
            if ie_end > end:
                return None

            if iei == 0x00 and ie_len == 3:
                reference = udh[pos]
                total_parts = udh[pos + 1]
                sequence = udh[pos + 2]
                if total_parts and 1 <= sequence <= total_parts:
                    return MultipartInfo(reference, total_parts, sequence)
            elif iei == 0x08 and ie_len == 4:
                reference = (udh[pos] << 8) | udh[pos + 1]
                total_parts = udh[pos + 2]
                sequence = udh[pos + 3]
                if total_parts and 1 <= sequence <= total_parts:
                    return MultipartInfo(reference, total_parts, sequence, is_16bit=True)

            pos = ie_end

        return None

    @staticmethod
    def decode_gsm7(data: bytes, septet_count: int) -> str:
        """Decode GSM 7-bit packed data back to text."""
        septets = []
        bits = 0
        byte_val = 0

        for b in data:
            byte_val |= (b << bits)
            bits += 8
            while bits >= 7 and len(septets) < septet_count:
                septets.append(byte_val & 0x7F)
                byte_val >>= 7
                bits -= 7

        decoded = []
        idx = 0
        while idx < len(septets):
            septet = septets[idx]
            if septet == GSM7_ESCAPE:
                if idx + 1 >= len(septets):
                    decoded.append(GSM7_BASIC[septet])
                    idx += 1
                    continue

                extension_char = _GSM7_EXTENSION_DECODE.get(septets[idx + 1])
                if extension_char is not None:
                    decoded.append(extension_char)
                    idx += 2
                    continue

            decoded.append(GSM7_BASIC[septet] if septet < len(GSM7_BASIC) else "?")
            idx += 1

        return "".join(decoded)

    @staticmethod
    def decode_timestamp(ts_hex: str) -> Optional[datetime]:
        """Decode PDU timestamp (7 octets, semi-octet BCD).

        Format: YY MM DD HH MM SS TZ (each byte has swapped nibbles).
        """
        if len(ts_hex) < 14:
            return None

        def swap_bcd(hex_pair: str) -> int:
            return int(hex_pair[1] + hex_pair[0])

        try:
            year = 2000 + swap_bcd(ts_hex[0:2])
            month = swap_bcd(ts_hex[2:4])
            day = swap_bcd(ts_hex[4:6])
            hour = swap_bcd(ts_hex[6:8])
            minute = swap_bcd(ts_hex[8:10])
            second = swap_bcd(ts_hex[10:12])

            # Timezone: semi-octet BCD with swapped nibbles, sign in bit 3
            # of the high nibble of the raw byte
            raw_tz_byte = int(ts_hex[12:14], 16)
            tz_sign = -1 if raw_tz_byte & 0x08 else 1
            raw_tz_clean = raw_tz_byte & ~0x08
            tz_quarters = ((raw_tz_clean & 0x0F) * 10) + ((raw_tz_clean >> 4) & 0x0F)
            tz_offset = timedelta(minutes=tz_sign * tz_quarters * 15)

            return datetime(year, month, day, hour, minute, second,
                            tzinfo=timezone(tz_offset))
        except (ValueError, IndexError):
            return None

    @staticmethod
    def decode_deliver_pdu(pdu_hex: str) -> Optional[dict]:
        """Decode an SMS-DELIVER PDU.

        Returns dict with keys: sender, body, timestamp, or None on failure.
        """
        try:
            pos = 0

            # SCA length
            sca_len = int(pdu_hex[pos:pos + 2], 16)
            pos += 2 + (sca_len * 2)

            # PDU type
            pdu_type = int(pdu_hex[pos:pos + 2], 16)
            pos += 2

            # Sender address length (number of digits)
            oa_len = int(pdu_hex[pos:pos + 2], 16)
            pos += 2
            oa_toa = int(pdu_hex[pos:pos + 2], 16)
            pos += 2

            is_alphanumeric_sender = (oa_toa & 0x70) == 0x50
            if is_alphanumeric_sender:
                # For alphanumeric originators, oa_len is a GSM 7-bit septet
                # count and the address bytes are packed septets.
                oa_octets = (oa_len * 7 + 7) // 8
                oa_hex_len = oa_octets * 2
                if pos + oa_hex_len > len(pdu_hex):
                    return None
                oa_hex = pdu_hex[pos:pos + oa_hex_len]
                pos += oa_hex_len
                sender = PDUDecoder.decode_gsm7(bytes.fromhex(oa_hex), oa_len)
                if not sender or any(ch not in _ALPHANUMERIC_ORIGINATOR_CHARS for ch in sender):
                    return None
            else:
                # Numeric originators are semi-octet encoded decimal digits.
                oa_hex_len = oa_len + (oa_len % 2)
                if pos + oa_hex_len > len(pdu_hex):
                    return None
                oa_hex = pdu_hex[pos:pos + oa_hex_len]
                pos += oa_hex_len
                sender = PDUEncoder.decode_phone_number(oa_hex, oa_toa)

            # PID
            pos += 2
            # DCS
            dcs = int(pdu_hex[pos:pos + 2], 16)
            pos += 2

            # Timestamp (7 octets = 14 hex chars)
            ts_hex = pdu_hex[pos:pos + 14]
            pos += 14
            timestamp = PDUDecoder.decode_timestamp(ts_hex)
            if is_alphanumeric_sender and timestamp is None:
                return None

            # User data length
            udl = int(pdu_hex[pos:pos + 2], 16)
            pos += 2
            ud_hex = pdu_hex[pos:]

            # Decode based on DCS
            if (dcs & 0x0C) == 0x08:
                # UCS2: UDL is an octet count.
                ud_hex_len = udl * 2
                if len(ud_hex) != ud_hex_len:
                    return None
                body = bytes.fromhex(ud_hex[:ud_hex_len]).decode("utf-16-be", errors="replace")
            elif (dcs & 0x0C) == 0x04:
                # 8-bit: UDL is an octet count.
                ud_hex_len = udl * 2
                if len(ud_hex) != ud_hex_len:
                    return None
                body = bytes.fromhex(ud_hex[:ud_hex_len]).decode("latin-1", errors="replace")
            else:
                # GSM 7-bit: UDL is a septet count.
                ud_octets = (udl * 7 + 7) // 8
                ud_hex_len = ud_octets * 2
                if len(ud_hex) != ud_hex_len:
                    return None
                body = PDUDecoder.decode_gsm7(bytes.fromhex(ud_hex[:ud_hex_len]), udl)

            return {
                "sender": sender,
                "body": body,
                "timestamp": timestamp,
            }

        except (ValueError, IndexError):
            return None
