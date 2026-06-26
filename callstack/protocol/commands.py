"""AT command constants and builders."""

import re

_PHONE_RE = re.compile(r'^[0-9+*#]+$')
_SMS_RECIPIENT_RE = re.compile(r'^\+?[0-9]{3,15}$')
_USSD_BREAKOUT_CHARS = frozenset({'"', "\r", "\n"})


def _validate_phone(number: str) -> str:
    """Validate and sanitize a phone number for AT commands."""
    if not number or not _PHONE_RE.match(number):
        raise ValueError(f"Invalid phone number: {number!r} (only digits, +, *, # allowed)")
    return number


def _validate_sms_recipient(number: str) -> str:
    """Validate and sanitize an SMS recipient for AT+CMGS."""
    if not isinstance(number, str) or not _SMS_RECIPIENT_RE.fullmatch(number):
        raise ValueError(
            f"Invalid SMS recipient: {number!r} "
            "(use optional leading + followed by 3-15 digits)"
        )
    return number


def _validate_ussd_code(code: str) -> str:
    """Validate USSD/menu input before embedding it in a quoted AT command."""
    if not isinstance(code, str) or not code:
        raise ValueError("Invalid USSD code")
    if any(char in code for char in _USSD_BREAKOUT_CHARS):
        raise ValueError("Invalid USSD code")
    return code


def _validate_ussd_encoding(encoding: int) -> int:
    """Validate USSD data coding scheme values used by AT+CUSD."""
    if type(encoding) is not int or not (0 <= encoding <= 255):
        raise ValueError("Invalid USSD encoding")
    return encoding


class ATCommand:
    """Common AT command strings."""

    # Basic
    AT = "AT"
    ECHO_OFF = "ATE0"
    RESET = "ATZ"
    INFO = "ATI"

    # Call control
    DIAL = "ATD"          # + number + ;
    ANSWER = "ATA"
    HANGUP = "ATH"

    # Caller ID
    CLIP_ENABLE = "AT+CLIP=1"
    COLP_ENABLE = "AT+COLP=1"

    # Disconnect control
    CVHU = "AT+CVHU=0"

    # Audio
    CPCMREG_ON = "AT+CPCMREG=1"
    CPCMREG_OFF = "AT+CPCMREG=0"

    # SMS
    SMS_TEXT_MODE = "AT+CMGF=1"
    SMS_PDU_MODE = "AT+CMGF=0"
    SMS_CHARSET_GSM = 'AT+CSCS="GSM"'
    SMS_NOTIFY = "AT+CNMI=2,1,0,1,0"
    SMS_DELIVERY_REPORT = "AT+CSMP=49,167,0,0"

    # Network
    SIGNAL_QUALITY = "AT+CSQ"
    REGISTRATION = "AT+CREG?"
    PACKET_REGISTRATION = "AT+CGREG?"
    LTE_REGISTRATION = "AT+CEREG?"
    OPERATOR = "AT+COPS?"

    @staticmethod
    def dial(number: str) -> str:
        return f"ATD{_validate_phone(number)};"

    @staticmethod
    def send_sms(number: str) -> str:
        return f'AT+CMGS="{_validate_sms_recipient(number)}"'

    _VALID_SMS_STATUSES = frozenset({
        "ALL", "REC UNREAD", "REC READ", "STO UNSENT", "STO SENT",
    })

    @staticmethod
    def read_sms(index: int) -> str:
        if not isinstance(index, int) or index < 0:
            raise ValueError(f"Invalid SMS index: {index!r} (must be non-negative integer)")
        return f"AT+CMGR={index}"

    @staticmethod
    def delete_sms(index: int) -> str:
        if not isinstance(index, int) or index < 0:
            raise ValueError(f"Invalid SMS index: {index!r} (must be non-negative integer)")
        return f"AT+CMGD={index}"

    @staticmethod
    def list_sms(status: str = "ALL") -> str:
        if status not in ATCommand._VALID_SMS_STATUSES:
            raise ValueError(
                f"Invalid SMS status: {status!r} (must be one of {sorted(ATCommand._VALID_SMS_STATUSES)})"
            )
        return f'AT+CMGL="{status}"'

    DELETE_ALL_SMS = "AT+CMGD=1,4"

    # SIM PIN
    CPIN_QUERY = "AT+CPIN?"

    @staticmethod
    def cpin_enter(pin: str) -> str:
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 8):
            raise ValueError(f"Invalid PIN: must be 4-8 digits")
        return f'AT+CPIN="{pin}"'

    @staticmethod
    def cpin_puk(puk: str, new_pin: str) -> str:
        if not puk or not puk.isdigit() or not (8 <= len(puk) <= 8):
            raise ValueError(f"Invalid PUK: must be 8 digits")
        if not new_pin or not new_pin.isdigit() or not (4 <= len(new_pin) <= 8):
            raise ValueError(f"Invalid new PIN: must be 4-8 digits")
        return f'AT+CPIN="{puk}","{new_pin}"'

    # DTMF
    _VALID_DTMF = frozenset("0123456789*#ABCD")

    @staticmethod
    def _dtmf_duration_units(duration_ms: int | None) -> int | None:
        if duration_ms is None:
            return None
        if type(duration_ms) is not int:
            raise ValueError("DTMF duration must be an integer number of milliseconds")
        if duration_ms == 0:
            return None
        if not (100 <= duration_ms <= 25500) or duration_ms % 100 != 0:
            raise ValueError(
                "DTMF duration must be 0 for modem default or 100-25500 ms "
                "in 100 ms increments"
            )
        return duration_ms // 100

    @staticmethod
    def send_dtmf(digit: str, duration_ms: int | None = None) -> str:
        if len(digit) != 1 or digit not in ATCommand._VALID_DTMF:
            raise ValueError(f"Invalid DTMF digit: {digit!r} (must be single char: 0-9, *, #, A-D)")
        duration_units = ATCommand._dtmf_duration_units(duration_ms)
        if duration_units is None:
            return f"AT+VTS={digit}"
        return f"AT+VTS={digit},{duration_units}"

    # USSD
    @staticmethod
    def ussd_send(code: str, encoding: int = 15) -> str:
        return f'AT+CUSD=1,"{_validate_ussd_code(code)}",{_validate_ussd_encoding(encoding)}'

    USSD_CANCEL = "AT+CUSD=2"
