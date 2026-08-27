"""Minimal Solana primitives: base58, and program-derived addresses.

No SDK dependency - this is the only on-chain maths the bot needs, and pulling
in solana-py for two functions is not worth the install weight.

PDA derivation matters because brand-new pump.fun launches are not indexed by
DexScreener for some minutes after creation, so their bonding curve address
cannot be looked up. It can be derived from the mint instead.
"""
from __future__ import annotations

import hashlib

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {c: i for i, c in enumerate(B58_ALPHABET)}

PDA_MARKER = b"ProgramDerivedAddress"

# ed25519 field parameters, for the on-curve check.
_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58_ALPHABET[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in B58_INDEX:
            raise ValueError(f"invalid base58 character {ch!r}")
        n = n * 58 + B58_INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def _is_on_curve(raw: bytes) -> bool:
    """True if `raw` decodes to a valid ed25519 point.

    A PDA must NOT be on the curve - that is what guarantees no private key
    exists for it.
    """
    if len(raw) != 32:
        return False
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return False

    # Solve x^2 = (y^2 - 1) / (d*y^2 + 1)
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    if v == 0:
        return False
    xx = u * pow(v, _P - 2, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if x * x % _P != xx:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
        if x * x % _P != xx:
            return False
    if x == 0 and sign:
        return False
    return True


def create_program_address(seeds: list[bytes], program_id: str) -> bytes | None:
    """Return the address for these seeds, or None if it lands on the curve."""
    data = b"".join(seeds) + b58decode(program_id) + PDA_MARKER
    digest = hashlib.sha256(data).digest()
    return None if _is_on_curve(digest) else digest


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Find the canonical PDA and its bump seed.

    Mirrors Solana's algorithm: count down from bump 255 and take the first
    seed that produces an off-curve address.
    """
    for bump in range(255, -1, -1):
        addr = create_program_address(seeds + [bytes([bump])], program_id)
        if addr is not None:
            return b58encode(addr), bump
    raise ValueError("no valid program address found")


# --- pump.fun ---------------------------------------------------------------

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def bonding_curve_address(mint: str) -> str:
    """Derive a pump.fun token's bonding curve account from its mint."""
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)[0]
