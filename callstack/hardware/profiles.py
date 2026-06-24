"""Pure modem capability profile classification helpers.

The helpers in this module classify already-known identity strings. They do not
probe hardware, open serial ports, or execute AT commands.
"""

from callstack.hardware.discovery import ModemCapabilities, ModemIdentity

_QUECTEL_MODEL_HINTS = (
    "BC",
    "BG",
    "EC",
    "EG",
    "EM",
    "EP",
    "FC",
    "MC",
    "M95",
    "M66",
    "RG",
    "RM",
    "UC",
    "UG",
)


def _identity_text(identity: ModemIdentity) -> str:
    return " ".join((identity.manufacturer, identity.model, identity.revision)).upper()


def _looks_like_simcom(identity: ModemIdentity) -> bool:
    text = _identity_text(identity)
    return "SIMCOM" in text or "SIM7600" in text or "SIM868" in text


def _looks_like_quectel(identity: ModemIdentity) -> bool:
    text = _identity_text(identity)
    model = identity.model.strip().upper()
    return "QUECTEL" in text or any(model.startswith(prefix) for prefix in _QUECTEL_MODEL_HINTS)


def classify_capabilities(identity: ModemIdentity) -> ModemCapabilities:
    """Return a conservative capability profile for modem identity strings."""

    if _looks_like_simcom(identity):
        return ModemCapabilities(
            sms_text_mode="supported",
            sms_pdu_mode="supported",
            delivery_reports="supported",
            ussd="supported",
            voice_calls="supported",
            dtmf_send="supported",
        )

    if _looks_like_quectel(identity):
        return ModemCapabilities(
            sms_text_mode="supported",
            sms_pdu_mode="supported",
            delivery_reports="supported",
            ussd="supported",
        )

    return ModemCapabilities()


def profile_notes(identity: ModemIdentity) -> tuple[str, ...]:
    """Return short, actionable notes explaining the pure classification."""

    if _looks_like_simcom(identity):
        return (
            "SIMCom-like identity matched; common SMS, USSD, voice call, and DTMF send support marked supported.",
            "PCM audio and GNSS remain unknown until explicit model evidence or a manual probe confirms them.",
        )

    if _looks_like_quectel(identity):
        return (
            "Quectel-like identity matched; common SMS and USSD support marked supported.",
            "Voice, DTMF, PCM audio, and GNSS remain unknown until explicit model evidence or a manual probe confirms them.",
        )

    return (
        "Unknown modem identity; all capabilities remain unknown.",
        "Add a safe manual profile or run a future non-sensitive capability probe before relying on features.",
    )
