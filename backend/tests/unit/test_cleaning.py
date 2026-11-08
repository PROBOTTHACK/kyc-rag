"""Stage C text normalisation."""

import pytest

from app.ingestion.cleaning import (
    collapse_vertical_letters,
    is_noise,
    is_non_latin,
    join_lines,
    normalize_line,
)

FF_LIGATURE = chr(0xFB00)
FFL_LIGATURE = chr(0xFB04)
WORD_BULLET = chr(0xF0B7)  # Word "Symbol" bullet (private use area)
NBSP = chr(0xA0)
RUPEE = chr(0x20B9)


def test_ligatures_and_compatibility_forms() -> None:
    assert normalize_line(f"e{FF_LIGATURE}ect") == "effect"
    assert normalize_line(f"o{FFL_LIGATURE}ine") == "offline"


def test_private_use_bullets_become_bullets() -> None:
    assert (
        normalize_line(f"{WORD_BULLET} Opening a new account")
        == f"{chr(0x2022)} Opening a new account"
    )


def test_whitespace_underscores_and_currency() -> None:
    assert normalize_line(f"  KYC{NBSP}norms \t apply  ") == "KYC norms apply"
    assert normalize_line("Name __________ Date") == "Name ___ Date"
    assert normalize_line(f"{RUPEE}50,000") == f"{RUPEE}50,000"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.4 T e l e p h o n e / F a x", "2.4 Telephone / Fax"),
        ("D D M M Y Y Y Y", "DDMMYYYY"),
        ("I / We declare", "I / We declare"),  # not letter-spacing
        ("a b", "a b"),  # two letters are left alone
        ("(a) (b) (c)", "(a) (b) (c)"),  # list markers are not letter-spacing
        ("A B C", "ABC"),  # known trade-off: three bare initials are closed up too
    ],
)
def test_letter_spaced_words(raw: str, expected: str) -> None:
    assert normalize_line(raw) == expected


def test_join_lines_keeps_compound_hyphens() -> None:
    assert join_lines(["Prevention of Money-", "Laundering Act"]) == (
        "Prevention of Money-Laundering Act"
    )
    assert join_lines(["equivalent e-", "document"]) == "equivalent e-document"
    assert join_lines(["ends with dash -", "next"]) == "ends with dash - next"
    assert join_lines(["", " first ", "", "second"]) == "first second"


def test_script_and_noise_detection() -> None:
    assert is_non_latin("भारतीय रिज़र्व बैंक")
    assert not is_non_latin("RESERVE BANK OF INDIA")
    assert not is_non_latin("12.0 ---")
    for noise in ("| | |", "———", "=)", "e", "1"):
        assert is_noise(noise)
    assert not is_noise("PAN No")
    assert is_noise("ID") and not is_noise("ID", min_run=2)


def test_collapse_vertical_letters() -> None:
    lines = ["Full Name", "S", "U", "R", "N", "A", "M", "E", "Mobile", "A", "B"]
    assert collapse_vertical_letters(lines) == ["Full Name", "SURNAME", "Mobile", "A", "B"]
