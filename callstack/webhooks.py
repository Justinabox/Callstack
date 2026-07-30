import hashlib
import hmac


def webhook_signature(secret: str, timestamp: str, payload: bytes) -> str:
    message = timestamp.encode("ascii") + b"." + payload
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
