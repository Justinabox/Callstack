class CallstackError(Exception):
    """Base exception for all Callstack errors."""


class TransportError(CallstackError):
    """Error in the transport layer (serial port issues, disconnection)."""


class ATError(CallstackError):
    """Base for AT command errors."""


class ATTimeoutError(ATError):
    """AT command timed out waiting for a response."""


class ATCommandError(ATError):
    """AT command returned an error response."""

    def __init__(self, command: str, error_lines: list[str]):
        self.command = command
        self.error_lines = error_lines
        super().__init__(f"AT command failed: {command} -> {error_lines}")


class InvalidStateTransition(CallstackError):
    """Attempted an invalid state machine transition."""

    def __init__(self, from_state, to_state):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Invalid transition: {from_state} -> {to_state}")


class CallError(CallstackError):
    """Base for voice call errors."""


class DialError(CallError):
    """Failed to initiate an outbound call."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        super().__init__(f"Dial failed: {lines}")


class AnswerError(CallError):
    """Failed to answer an incoming call."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        super().__init__(f"Answer failed: {lines}")


class AudioFormatError(CallstackError):
    """WAV file does not match the required modem audio format."""


class SMSError(CallstackError):
    """Base for SMS errors."""


class SMSSendError(SMSError):
    """Failed to send an SMS message."""

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(f"SMS send failed: {detail}")


class SMSReadError(SMSError):
    """Failed to read an SMS message from storage."""

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(f"SMS read failed: {detail}")
