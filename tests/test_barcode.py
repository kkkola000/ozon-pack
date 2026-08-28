"""Кодирование Code128 и разбор вариантов штрихкода."""
from app.barcode import PATTERNS, encode_code128b, total_modules
from app.packing import barcode_variants


def test_code128_structure():
    widths = encode_code128b("A")
    # старт + символ + контрольная сумма + стоп = 6+6+6+7 модульных групп
    assert len(widths) == 6 + 6 + 6 + 7
    assert sum(widths) == total_modules("A")


def test_code128_checksum_known_value():
    """Контрольная сумма = (старт + сумма позиция*значение) % 103."""
    # "AB": старт B = 104, A = 33, B = 34 -> (104 + 1*33 + 2*34) % 103 = 102
    widths = encode_code128b("AB")
    checksum_pattern = "".join(str(w) for w in widths[18:24])
    assert checksum_pattern == PATTERNS[(104 + 1 * 33 + 2 * 34) % 103]
    assert checksum_pattern == PATTERNS[102]


def test_code128_all_patterns_are_11_modules():
    for pattern in PATTERNS[:-1]:
        assert sum(int(ch) for ch in pattern) == 11


def test_barcode_variants_datamatrix_gtin():
    """DataMatrix «Честного знака»: из кода достаётся GTIN и EAN-13."""
    variants = barcode_variants("010460000000001721ABCDEFG")
    assert "04600000000017" in variants
    assert "4600000000017" in variants


def test_barcode_variants_leading_zero():
    """EAN-13 с ведущим нулём и UPC-12 должны находить один и тот же товар."""
    assert "4600000000017" in barcode_variants("04600000000017")
    assert "0123456789012" in barcode_variants("123456789012")
    assert barcode_variants("") == []
    assert barcode_variants("   ") == []


def test_barcode_variants_keeps_original_first():
    assert barcode_variants("ABC-123")[0] == "ABC-123"
