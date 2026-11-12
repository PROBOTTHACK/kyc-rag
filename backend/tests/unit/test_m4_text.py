"""M4 S2: segmentation keeps exact offsets into the stored text (property-style + cases)."""

import random

import pytest

from app.verification.text import segments, sentences

FRAGMENTS = [
    "35. For opening an account of a trust, certified copies of the following documents "
    "shall be obtained:",
    "(a) Registration certificate;",
    "(b) Trust deed; and",
    "(c) PAN or Form No. 60 of the trust.",
    "Rs. 50,000 is a limit, e.g. for small accounts.",
    "See para 16.1 above.",
    "Q6. Which documents are OVDs?",
    "The RE shall not open an account where it cannot verify identity.",
    "i) the proprietor; ii) the karta.",
    "Documents not older than two months may be accepted, i.e. utility bills!",
    "   ",
    "\n",
]


def test_segment_offsets_are_exact_ordered_and_disjoint_for_random_texts() -> None:
    rng = random.Random(20261112)  # noqa: S311 - seeded test inputs, not security
    for _ in range(500):
        text = " ".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 8)))
        segs = segments(text)
        for s in segs:
            assert text[s.start : s.end] == s.text
            assert s.text == s.text.strip() and s.text
            assert any(c.isalnum() for c in s.text)
        assert all(a.end <= b.start for a, b in zip(segs, segs[1:], strict=False))
        assert segments(text) == segs  # deterministic


def test_list_lead_ins_and_items_are_separate_segments() -> None:
    text = " ".join(FRAGMENTS[:4])
    segs = [s.text for s in segments(text)]
    assert segs[0].endswith("shall be obtained:")
    assert segments(text)[0].lead_in is True
    assert "(a) Registration certificate;" in segs
    assert "(b) Trust deed;" in segs
    assert "and (c) PAN or Form No. 60 of the trust." in segs  # connector joins the item


@pytest.mark.parametrize(
    "text",
    [
        "PAN or Form No. 60 of the trust.",
        "Rs. 50,000 is a limit, e.g. for small accounts.",
        "See para 16.1 above.",
        "Copies i.e. certified ones etc. are fine.",
        "The time is 10:30 today.",
    ],
)
def test_abbreviations_numbers_and_times_never_end_a_sentence(text: str) -> None:
    assert [s.text for s in segments(text)] == [text]


@pytest.mark.parametrize(
    ("text", "first"),
    [
        ("35. For a trust, X shall be obtained.", "35. For a trust, X shall be obtained."),
        ("Q6. Which documents are OVDs?", "Q6. Which documents are OVDs?"),
        ("16.1. A rule applies.", "16.1. A rule applies."),
    ],
)
def test_paragraph_labels_stay_with_their_text(text: str, first: str) -> None:
    assert segments(text)[0].text == first


def test_label_only_text_is_kept_not_lost() -> None:
    assert [s.text for s in segments("Annex 35.")] == ["Annex 35."]
    assert [s.text for s in segments("35.")] == ["35."]


def test_sentences_are_verbatim_and_keep_lists_together() -> None:
    text = (
        "The form states that Form No. 60 must be attached. It applies to LLPs; see para "
        "16.1. Documents: (a) deed; (b) PAN."
    )
    assert sentences(text) == [
        "The form states that Form No. 60 must be attached.",
        "It applies to LLPs; see para 16.1.",
        "Documents: (a) deed; (b) PAN.",
    ]
    assert sentences("") == [] and sentences("  ;  ") == []
