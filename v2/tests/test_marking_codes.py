"""Compatibility of the supported scanner profiles, independent of the legacy app."""

import pytest
from instance_remote import GTIN, mark

from pack_v2.modules.marking.codes import equivalent, parse
from pack_v2.modules.marking.public import Marking

COMPACT = "01" + GTIN + '21AbC!>QwERt"9x93aB12'


@pytest.mark.parametrize("prefix", ["", "]d2", "]d8"])
@pytest.mark.parametrize("notation", ["native", "<gs>", "<GS>", r"\<gs>", "joined"])
def test_scanner_profiles_preserve_identity_and_crypto(prefix, notation):
    native = mark(crypto="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq=")
    # The joined 91/92 profile has exactly a 44-character base64 signature.
    assert len(native.split("\x1d92")[1]) == 44
    raw = native if notation == "native" else native.replace("\x1d", "" if notation == "joined" else notation)
    parsed = parse(prefix + raw + "\r\n")
    assert parsed.raw == native and parsed.gtin == GTIN and parsed.serial == "ABCDEF1234567"
    assert equivalent(native, raw)


def test_compact_profile_preserves_literal_quote_and_exact_bytes():
    code = parse(COMPACT)
    assert code.compact and code.raw == COMPACT and code.serial == 'AbC!>QwERt"9x'
    assert equivalent(COMPACT, COMPACT[:31] + "\x1d" + COMPACT[31:])
    assert not equivalent(COMPACT, COMPACT[:-1] + "z")


@pytest.mark.parametrize(
    "bad",
    [
        COMPACT[:-1],
        COMPACT + "z",
        COMPACT.replace(GTIN, GTIN[:-1] + "0"),
        COMPACT + "\x00",
        COMPACT[:31] + "\\u001d" + COMPACT[31:],
    ],
)
def test_damaged_or_ambiguous_profiles_are_never_repaired(bad):
    with pytest.raises(ValueError):
        parse(bad)


def test_only_confirmed_numeric_gtin_values_are_normalized():
    assert Marking.gtins([GTIN, "OZN" + GTIN, "MRKT" + GTIN, "0000001", "bad", GTIN[:-1] + "0"]) == (GTIN,)
