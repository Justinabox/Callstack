"""Pure modem capability profile classification helpers.

The helpers in this module classify already-known identity strings. They do not
probe hardware, open serial ports, or execute AT commands.
"""

from callstack.hardware.discovery import AudioPortHint, ModemCapabilities, ModemIdentity

# Two-letter Quectel model-family prefixes are only recognised when immediately
# followed by a numeric family suffix (e.g. "EC25"), so unrelated devices that
# merely share the leading letters (e.g. "ECONET-1") stay unknown.
_QUECTEL_FAMILY_PREFIXES = (
    "BC",
    "BG",
    "EC",
    "EG",
    "EM",
    "EP",
    "FC",
    "MC",
    "RG",
    "RM",
    "UC",
    "UG",
)

# Fully-specified Quectel model names that already carry their numeric family.
_QUECTEL_FULL_MODELS = (
    "M95",
    "M66",
)


def _identity_text(identity: ModemIdentity) -> str:
    return " ".join((identity.manufacturer, identity.model, identity.revision)).upper()


def _looks_like_simcom(identity: ModemIdentity) -> bool:
    text = _identity_text(identity)
    return "SIMCOM" in text or "SIM7600" in text or "SIM868" in text


def _has_quectel_family_prefix(model: str) -> bool:
    for prefix in _QUECTEL_FAMILY_PREFIXES:
        if model.startswith(prefix) and len(model) > len(prefix) and model[len(prefix)].isdigit():
            return True
    return False


def _looks_like_quectel(identity: ModemIdentity) -> bool:
    text = _identity_text(identity)
    model = identity.model.strip().upper()
    return (
        "QUECTEL" in text
        or any(model.startswith(full) for full in _QUECTEL_FULL_MODELS)
        or _has_quectel_family_prefix(model)
    )


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


def audio_port_hint_for_identity(identity: ModemIdentity) -> AudioPortHint:
    """Return a conservative public-safe audio-port hint for an identity."""

    if _looks_like_simcom(identity):
        return AudioPortHint(
            port=None,
            confidence="profile-hint",
            reason=(
                "SIMCom-like modems commonly expose a separate PCM/audio serial interface; "
                "configure CALLSTACK_AUDIO_PORT manually after hardware validation."
            ),
        )

    return AudioPortHint()


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
