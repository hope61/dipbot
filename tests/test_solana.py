"""base58 and program-derived addresses.

The PDA cases are real mint/curve pairs reported by PumpPortal, so they are
ground truth rather than self-consistent fixtures. Derivation matters because
brand-new launches are not in DexScreener for some minutes, so the curve
address cannot be looked up.
"""
from __future__ import annotations

import pytest

from dipbot.solana import (
    PUMP_PROGRAM,
    b58decode,
    b58encode,
    bonding_curve_address,
    find_program_address,
)

# (mint, bondingCurveKey) captured live from pump.fun creation events, where the
# curve address is reported by the chain rather than derived. Ground truth.
KNOWN_CURVES = [
    ("FmsQNEebvJLMcfiKuVuvGRgAQJzpJfyjJ8PHUGnzpump", "FMbFRbx61wJitzK4HhSFXd9ijT2kwvsEqjQkekZ4ej75"),
    ("DvFzcExFCWj9GnVS1ByXrsPezniP7Be6QienbXJ8pump", "75GnwHQbBfU1Q1n4ooWB32Zsy6zRv3sUy1YuME5gNN5W"),
    ("Bgw29GVLpT4cmAcJRxERg8nhVZQKBo4SGCrLXt66pump", "3uyv4edC67U5K4ToW2P99HAJrJLWAzNumBz9VhGiyXB1"),
    ("7Ykr5ftEu2S5jD9SjoDtt4E5AeSMgtVoF8wZLTz5pump", "7haqbCexhTukSV6wfb6MbuhF4yGvk1gQ7ra51gEntxZa"),
]


@pytest.mark.parametrize(
    "text",
    [
        "So11111111111111111111111111111111111111112",
        "11111111111111111111111111111111",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    ],
)
def test_b58_round_trip(text):
    assert b58encode(b58decode(text)) == text


def test_b58decode_rejects_invalid_characters():
    with pytest.raises(ValueError):
        b58decode("0OIl")  # the four characters base58 deliberately omits


def test_b58decode_preserves_leading_zeros():
    assert b58decode("11111111111111111111111111111111") == b"\x00" * 32


def test_b58encode_empty():
    assert b58encode(b"") == ""


def test_find_program_address_is_deterministic():
    a, bump_a = find_program_address([b"bonding-curve", b58decode(KNOWN_CURVES[0][0])], PUMP_PROGRAM)
    b, bump_b = find_program_address([b"bonding-curve", b58decode(KNOWN_CURVES[0][0])], PUMP_PROGRAM)
    assert a == b
    assert bump_a == bump_b


def test_find_program_address_returns_valid_length():
    addr, bump = find_program_address([b"seed"], PUMP_PROGRAM)
    assert 32 <= len(addr) <= 44
    assert 0 <= bump <= 255


@pytest.mark.parametrize("mint, expected_curve", KNOWN_CURVES)
def test_bonding_curve_matches_chain(mint, expected_curve):
    """Verified against 10/10 live launches when this was written."""
    assert bonding_curve_address(mint) == expected_curve


def test_different_mints_give_different_curves():
    a = bonding_curve_address(KNOWN_CURVES[0][0])
    b = bonding_curve_address(KNOWN_CURVES[1][0])
    assert a != b
