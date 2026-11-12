"""The M3 "done when" rules for a finished report (M3 S8), shared by the CI end-to-end
test and `make demo` / `make demo-live`, so the live run is judged by the same rules.

A report passes when:
- its outcome is one of the five defined outcomes (no approve/reject value exists) and
  `human_review_required` is true, with the notice and the independence disclaimer;
- every checklist item cites at least one stored chunk, and every citation's chunk id
  EXISTS in PostgreSQL with the same document key and a page inside the chunk's pages
  (looked up, never trusted from the report);
- every MANDATORY item has at least one primary citation whose stored chunk can support
  MANDATORY;
- `unresolved_items` is present (listed, possibly empty), and an INSUFFICIENT_EVIDENCE
  outcome carries the "no evidence, no answer" fallback text.
- (M4 S5) it validates as a `ComplianceReport` (v1 or v2, with every model invariant);
  otherwise it fails with `report_schema_invalid` (field locations only, never values) and
  no other check runs. A v2 report whose claim check was `not_configured` (NLI disabled)
  must show every item `not_verified`, none MANDATORY, and agree with its metadata; and
  (M4 S6) its conflict scan must be `not_run`, with nothing scanned and no conflicts.
An honestly labelled weak outcome (INSUFFICIENT_EVIDENCE, DEGRADED, ...) passes; the
checker never asks for a particular outcome. It returns problems as codes with ids only.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.graph.report import FALLBACK
from app.graph.state import DISCLAIMER, NOTICE, NOTICE_V2, ComplianceReport, Outcome


@dataclass(frozen=True)
class StoredChunk:
    document_key: str
    page_start: int
    page_end: int
    can_support_mandatory: bool


ChunkLookup = Callable[[Sequence[str]], dict[str, StoredChunk]]


def db_chunk_lookup(db: Session) -> ChunkLookup:
    def lookup(ids: Sequence[str]) -> dict[str, StoredChunk]:
        valid = []
        for i in ids:
            try:
                valid.append(uuid.UUID(i))
            except ValueError:
                continue  # not a chunk id: reported as missing
        if not valid:
            return {}
        rows = db.execute(
            sa.text(
                "SELECT id, document_key, page_number, coalesce(page_end, page_number) AS pe, "
                "can_support_mandatory FROM chunks WHERE id = ANY(:ids)"
            ),
            {"ids": valid},
        )
        return {
            str(r.id): StoredChunk(r.document_key, r.page_number, r.pe, r.can_support_mandatory)
            for r in rows
        }

    return lookup


@dataclass
class Acceptance:
    problems: list[str]
    items: int
    mandatory: int
    citations: int
    unresolved: int

    @property
    def ok(self) -> bool:
        return not self.problems


SCHEMA_INVALID = "report_schema_invalid"
_MAX_SCHEMA_LOCATIONS = 10


def _schema_problems(report: dict[str, Any]) -> tuple[ComplianceReport | None, list[str]]:
    """Validate the whole report first; a malformed report never reaches the other checks.
    Problems name field locations only (never the offending values)."""
    try:
        return ComplianceReport.model_validate(report), []
    except ValidationError as exc:
        locs = sorted({".".join(str(p) for p in e["loc"]) or "report" for e in exc.errors()})
        return None, [SCHEMA_INVALID] + [
            f"{SCHEMA_INVALID}:{loc}" for loc in locs[:_MAX_SCHEMA_LOCATIONS]
        ]


def _not_configured_problems(model: ComplianceReport) -> list[str]:
    """M4 S5: with NLI disabled, nothing may look verified and nothing may be MANDATORY."""
    meta = model.verification_meta
    if model.report_version != 2 or meta is None or meta.status != "not_configured":
        return []
    problems: list[str] = []
    if model.verification != "not_claim_verified":
        problems.append("verification_status_inconsistent")
    if any(v is not None for v in (meta.model_id, meta.model_revision, meta.label_map,
                                   meta.thresholds_version)) or meta.pairs_scored:  # fmt: skip
        problems.append("verification_meta_inconsistent")
    items = model.checklist or []
    for n, item in enumerate(items):
        if item.verification_status != "not_verified":
            problems.append(f"item_{n}:verification_status_not_not_verified")
        if item.applicability == "MANDATORY":
            problems.append(f"item_{n}:mandatory_without_claim_verification")
    results = model.claim_verification
    if items and results is None:
        problems.append("claim_verification_missing")
    for r in results or []:
        if (
            r.requirement_verdict != "not_verified"
            or r.judgements
            or any(v.verdict != "not_verified" for v in r.verdicts)
        ):
            problems.append(f"result_{r.requirement_id}:verified_without_a_model")
    if model.unverified_items:
        problems.append("unverified_items_without_a_model")
    # M4 S6: no conflict scan without a model (never "no conflict found")
    if meta.contradiction_scan != "not_run":
        problems.append("conflict_scan_without_a_model")
    if meta.contradiction_scan_size or meta.contradiction_pairs_not_scanned:
        problems.append("conflict_scan_size_without_a_model")
    if model.source_conflicts:
        problems.append("source_conflicts_without_a_model")
    if any("conflicting_sources" in r.review_flags for r in results or []):
        problems.append("conflict_flag_without_a_model")
    if items and "verification" not in {d.component for d in model.degradations}:
        problems.append("verification_degradation_missing")
    if items and model.outcome != Outcome.DEGRADED:
        problems.append("outcome_not_degraded_without_verification")
    return problems


def check_report(report: dict[str, Any], lookup: ChunkLookup) -> Acceptance:
    model, problems = _schema_problems(report)
    if model is None:
        return Acceptance(problems=problems, items=0, mandatory=0, citations=0, unresolved=0)
    problems += _not_configured_problems(model)
    outcome = report.get("outcome")
    if outcome not in {o.value for o in Outcome}:
        problems.append("unknown_outcome")
    if report.get("human_review_required") is not True:
        problems.append("human_review_not_required")
    # M4: a v2 report (claim verification ran) carries the M4 notice; v1 keeps the M3 one.
    notice = NOTICE_V2 if report.get("report_version") == 2 else NOTICE
    if report.get("notice") != notice or report.get("disclaimer") != DISCLAIMER:
        problems.append("notice_or_disclaimer_missing")
    if not isinstance(report.get("unresolved_items"), list):
        problems.append("unresolved_items_not_listed")
    if outcome == Outcome.INSUFFICIENT_EVIDENCE and not str(
        report.get("user_message", "")
    ).startswith(FALLBACK):
        problems.append("fallback_text_missing")

    items = report.get("checklist") or []
    cited_ids = [c.get("evidence_id", "") for i in items for c in i.get("citations", [])]
    stored = lookup(cited_ids)
    mandatory = 0
    for n, item in enumerate(items):
        cites = item.get("citations") or []
        if not cites:
            problems.append(f"item_{n}:no_citation")
        for c in cites:
            chunk = stored.get(str(c.get("evidence_id")))
            if chunk is None:
                problems.append(f"item_{n}:chunk_not_found")
            elif c.get("document_key") != chunk.document_key or not (
                chunk.page_start <= int(c.get("page", 0)) <= chunk.page_end
            ):
                problems.append(f"item_{n}:citation_mismatch")
        if item.get("applicability") == "MANDATORY":
            mandatory += 1
            primary = [
                c
                for c in cites
                if c.get("evidence_eligibility") == "primary"
                and (s := stored.get(str(c.get("evidence_id")))) is not None
                and s.can_support_mandatory
            ]
            if not primary:
                problems.append(f"item_{n}:mandatory_without_primary_citation")
    return Acceptance(
        problems=problems,
        items=len(items),
        mandatory=mandatory,
        citations=len(cited_ids),
        unresolved=len(report.get("unresolved_items") or []),
    )
