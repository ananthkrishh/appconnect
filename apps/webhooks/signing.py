"""HMAC-SHA256 webhook signatures.

Header format (Stripe-style, so the timestamp is covered by the MAC and old
requests cannot be replayed)::

    X-AppConnect-Signature: t=1731150000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd

The signed message is ``f"{t}.{raw_body}"``. Receivers should verify with
:func:`verify_signature` (or an equivalent) using the installation's
``webhook_secret`` and reject requests outside the tolerance window.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-AppConnect-Signature"
DEFAULT_TOLERANCE = 300  # seconds


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_signature_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={compute_signature(secret, ts, body)}"


def parse_signature_header(header: str) -> tuple[int, list[str]]:
    timestamp, signatures = None, []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        raise ValueError("Malformed signature header.")
    return timestamp, signatures


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> bool:
    try:
        timestamp, signatures = parse_signature_header(header)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        return False
    expected = compute_signature(secret, timestamp, body)
    # Several v1 values are allowed so secrets can be rotated without downtime.
    return any(hmac.compare_digest(expected, sig) for sig in signatures)
