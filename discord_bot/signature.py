"""
discord_bot/signature.py — Ed25519 signature verification for Discord Slash Commands.
"""

import logging
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

logger = logging.getLogger(__name__)


def verify_discord_signature(signature_hex: str, timestamp: str, body: str, public_key_hex: str) -> bool:
    """
    Verifies that an incoming request originated from Discord using Ed25519 signature verification.

    Args:
        signature_hex: The X-Signature-Ed25519 header value (hex encoded).
        timestamp: The X-Signature-Timestamp header value.
        body: The raw request body string.
        public_key_hex: The Discord application's public key (hex encoded).

    Returns:
        bool: True if signature is valid, False otherwise.
    """
    if not signature_hex or not timestamp or not body or not public_key_hex:
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        message = f"{timestamp}{body}".encode("utf-8")
        verify_key.verify(message, bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, ValueError, TypeError) as e:
        logger.warning(f"Discord signature verification failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during Discord signature verification: {e}", exc_info=True)
        return False
