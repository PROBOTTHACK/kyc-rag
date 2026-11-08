import pytest

from app.schemas.taxonomy import (
    ENTITY_GROUPS,
    Applicability,
    EntityGroup,
    EntityType,
    ResidencyStatus,
    SourceStatus,
    expand_entity_terms,
)


def test_source_status_keeps_spec_values_plus_unknown() -> None:
    assert {s.value for s in SourceStatus} == {"active", "dated", "superseded", "unknown"}


def test_requirement_status_is_a_separate_vocabulary() -> None:
    assert {a.value for a in Applicability} == {"MANDATORY", "CONDITIONAL", "OPTIONAL", "UNCLEAR"}
    assert not {s.value for s in SourceStatus} & {a.value for a in Applicability}


def test_legal_entity_group_membership() -> None:
    legal = ENTITY_GROUPS[EntityGroup.LEGAL_ENTITY]
    assert EntityType.LLP in legal
    assert EntityType.COMPANY in legal
    assert EntityType.PARTNERSHIP in legal
    for excluded in (EntityType.INDIVIDUAL, EntityType.SOLE_PROPRIETORSHIP, EntityType.HUF):
        assert excluded not in legal


def test_non_individual_is_everything_but_individual() -> None:
    assert ENTITY_GROUPS[EntityGroup.NON_INDIVIDUAL] == frozenset(EntityType) - {
        EntityType.INDIVIDUAL
    }
    assert ENTITY_GROUPS[EntityGroup.LEGAL_ENTITY] < ENTITY_GROUPS[EntityGroup.NON_INDIVIDUAL]


def test_group_names_are_not_entity_types() -> None:
    for group in EntityGroup:
        with pytest.raises(ValueError):
            EntityType(group.value)


def test_residency_is_not_an_entity_type() -> None:
    for status in ResidencyStatus:
        with pytest.raises(ValueError):
            EntityType(status.value)


def test_expand_entity_terms() -> None:
    assert expand_entity_terms(["LLP"]) == {EntityType.LLP}
    assert expand_entity_terms(["LEGAL_ENTITY", "INDIVIDUAL"]) == (
        ENTITY_GROUPS[EntityGroup.LEGAL_ENTITY] | {EntityType.INDIVIDUAL}
    )
    with pytest.raises(ValueError):
        expand_entity_terms(["NRI"])
