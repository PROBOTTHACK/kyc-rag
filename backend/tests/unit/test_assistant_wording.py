"""A3: the fixed dialogue wording is complete, uses the A1 chip wording, and fails closed."""

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.assistant.profile import SLOT_VALUES, Slot
from app.assistant.wording import WORDING_FILE, Wording, wording


def raw() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load(WORDING_FILE.read_text(encoding="utf-8")))


def test_every_slot_and_value_has_wording() -> None:
    w = wording()
    for slot in Slot:
        sw = w.slots[slot]
        assert sw.question.endswith("?") and sw.explanation and sw.not_sure
        assert set(sw.chips) == {v.value for v in SLOT_VALUES[slot]}


def test_chip_labels_are_the_a1_wording() -> None:
    chips = wording().slots[Slot.CUSTOMER_CATEGORY].chips
    assert chips["NRI_INDIVIDUAL"] == "An individual living outside India (NRI / PIO / OCI)"
    assert chips["OTHER_ORGANISATION"] == "A society, association, club or other organisation"
    assert wording().slots[Slot.ACCOUNT_TYPE].chips["FIXED_DEPOSIT"] == "Fixed / term deposit"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["slots"].pop("minor"),
        lambda d: d["slots"]["holding"]["chips"].pop("JOINT"),
        lambda d: d["slots"]["account_type"]["chips"].update({"GOLD": "Gold account"}),
        lambda d: d["slots"]["nri_scheme"].pop("explanation"),
        lambda d: d["cannot_build"].pop("account_type"),
        lambda d: d.update({"corrected": "Updated."}),
        lambda d: d.update({"unexpected": "x"}),
    ],
)
def test_incomplete_wording_fails_to_load(mutate: Any) -> None:
    d = raw()
    mutate(d)
    with pytest.raises(ValidationError):
        Wording.model_validate(d)
