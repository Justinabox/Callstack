"""AT command constants and builders."""

import re

_PHONE_RE = re.compile(r'^[0-9+*#]+$')


def _validate_phone(number: str) -> str:
    """Validate and sanitize a phone number for AT commands."""
    if not number or not _PHONE_RE.match(number):
        raise ValueError(f"Invalid phone number: {number!r} (only digits, +, *, # allowed)")
    return number


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
    SMS_NOTIFY = "AT+CNMI=2,2,0,1,0"
    SMS_DELIVERY_REPORT = "AT+CSMP=49,167,0,0"

    # Network
    SIGNAL_QUALITY = "AT+CSQ"
    REGISTRATION = "AT+CREG?"
    OPERATOR = "AT+COPS?"

    @staticmethod
    def dial(number: str) -> str:
        return f"ATD{_validate_phone(number)};"

    @staticmethod
    def send_sms(number: str) -> str:
        return f'AT+CMGS="{_validate_phone(number)}"'

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
    def send_dtmf(digit: str) -> str:
        if len(digit) != 1 or digit not in ATCommand._VALID_DTMF:
            raise ValueError(f"Invalid DTMF digit: {digit!r} (must be single char: 0-9, *, #, A-D)")
        return f"AT+VTS={digit}"

    # USSD
    @staticmethod
    def ussd_send(code: str, encoding: int = 15) -> str:
        return f'AT+CUSD=1,"{code}",{encoding}'

    USSD_CANCEL = "AT+CUSD=2"
