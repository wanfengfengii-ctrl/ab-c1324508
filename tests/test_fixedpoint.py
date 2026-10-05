import pytest

from app import fixedpoint as fp


@pytest.mark.parametrize(
    "text,micro",
    [
        ("1", 1_000_000),
        ("0.5", 500_000),
        ("0.000001", 1),
        ("123.456", 123_456_000),
        ("100", 100_000_000),
    ],
)
def test_parse_canonical(text, micro):
    assert fp.parse_dose(text) == micro
    assert fp.to_canonical(micro) == text


@pytest.mark.parametrize(
    "bad",
    [
        "0",          # not positive
        "00",         # leading zero
        "01.5",       # leading zero
        "1.0",        # trailing zero
        "1.10",       # trailing zero
        "1.",         # dangling dot
        ".5",         # missing integer part
        "+1",         # sign
        "-1",         # sign
        " 1",         # whitespace
        "1\n",
        "1E2",        # exponent
        "1e-3",
        "1.0000001",  # too fine
        "1_000",
        "",
        "abc",
        "1.2.3",
    ],
)
def test_reject_non_canonical(bad):
    with pytest.raises(fp.DecimalError):
        fp.parse_dose(bad)


def test_zero_allowed_when_non_positive_mode():
    assert fp.parse_dose("0", positive=False) == 0


def test_roundtrip_many_values():
    for micro in [0, 1, 999_999, 1_000_000, 42_000_001, 123_456_789]:
        assert fp.parse_dose(fp.to_canonical(micro), positive=False) == micro
