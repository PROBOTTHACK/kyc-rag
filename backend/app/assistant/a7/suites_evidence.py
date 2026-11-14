"""A7.2 suites: covered combinations, not-established combinations, resident-to-NRO
conversion, the evidence and citation audit, and the human citation-review sheet.

Every expectation is an invariant of the spec or of the stored data (A7_PLAN §2.2); no
checklist item is written by hand. Item ids per profile are compared with a golden file
recorded at the first A7 run, so later drift is detected and shown to the owner.
"""

from __future__ import annotations

import json
import random
import re
import uuid
from collections import Counter
from typing import Any

import sqlalchemy as sa

from app.assistant.a7.core import Context, header, suite, write_report
from app.assistant.a7.datasets import EVAL_DIR, REPO_ROOT, CoverageProfile, load_profiles
from app.assistant.checklist import ChecklistAnswer
from app.assistant.coverage import coverage
from app.assistant.service import AssistantService
from app.assistant.taxonomy import AccountType, CustomerCategory
from app.assistant.wording import wording
from app.db.session import create_sync_engine

GOLDEN_FILE = EVAL_DIR / "a7_golden_items.json"
RELIABLE = {"native", "ocr_reliable"}
ORGANISATIONS = {
    "SOLE_PROPRIETORSHIP", "PARTNERSHIP_FIRM", "LLP", "COMPANY", "TRUST", "HUF",
    "OTHER_ORGANISATION",
}  # fmt: skip
CONVERSION_DOC = "hdfc_resident_to_nro_conversion"


async def _send(service: AssistantService, sid: str, body: dict[str, Any]) -> dict[str, Any]:
    out = await service.submit_turn(sid, f"a7-{uuid.uuid4().hex}", json.dumps(body).encode())
    return out["answer"]  # type: ignore[no-any-return]


async def drive_profile(service: AssistantService, p: CoverageProfile) -> dict[str, Any]:
    """Answer exactly the questions the assistant asks, with the profile's values (chips)."""
    sid = (await service.create_session())["session_id"]
    if p.message is not None:
        answer = await _send(service, sid, {"message": p.message})
        if answer.get("kind") == "confirm" and p.kind == "conversion":
            # "NRO" is also an NRI hint (R7): it is confirmed first. Alternate yes / no so
            # both answers are shown to lead to the conversion checklist.
            yes = int(p.id.rsplit("-", 1)[1]) % 2 == 1
            answer = await _send(service, sid, {"action": "confirm_yes" if yes else "confirm_no"})
        return answer
    answer = await _send(
        service, sid, {"action": "choose", "slot": "account_type", "value": p.account_type}
    )
    answer = await _send(
        service,
        sid,
        {"action": "choose", "slot": "customer_category", "value": p.customer_category},
    )
    for _ in range(6):  # bounded: at most three facets are ever asked
        slot = (answer.get("question") or {}).get("slot")
        if answer.get("kind") != "question" or slot not in p.facets:
            break
        value = p.facets[slot]
        body = (
            {"action": "not_sure", "slot": slot}
            if value == "NOT_SURE"
            else {"action": "choose", "slot": slot, "value": value}
        )
        answer = await _send(service, sid, body)
    return answer


class ChunkTable:
    """Raw SQL over the development corpus: independent of the checklist's own lookup."""

    def __init__(self, settings: Any) -> None:
        self.engine = create_sync_engine(settings)

    def get(self, chunk_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT document_key, page_number, content, text_reliability, bank "
                        "FROM chunks WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": chunk_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def close(self) -> None:
        self.engine.dispose()


def _items(answer: dict[str, Any], *groups: str) -> list[dict[str, Any]]:
    g = (answer.get("checklist") or {}).get("groups") or {}
    return [i for name in groups for i in g.get(name, [])]


def audit_covered(
    p: CoverageProfile, answer: dict[str, Any], internal: ChecklistAnswer | None, chunks: ChunkTable
) -> tuple[list[str], dict[str, int]]:
    """The §2.2 invariants for one covered profile. Returns failures and citation counts."""
    f: list[str] = []
    counts = Counter[str]()
    w = wording()
    if answer.get("kind") != "checklist":
        return [f"kind {answer.get('kind')!r}, expected a checklist"], dict(counts)
    if answer.get("coverage_state") != p.state:
        f.append(f"coverage_state {answer.get('coverage_state')} != {p.state}")
    if answer.get("notice") != w.notice:
        f.append("notice is not the verbatim A1 §7 text")
    ab = _items(answer, "documents", "information")
    for item in _items(answer, "documents", "information", "not_confirmed"):
        if item["classification"] == "policy_supported":
            f.append(f"{item['id']}: policy_supported emitted")
        for c in item.get("citations", []) + item.get("related_citations", []):
            if c["quotable"] != (c["text_reliability"] in RELIABLE):
                f.append(f"{item['id']}: quotable flag disagrees with text reliability")
    for item in ab:
        title = item["title"].lower()
        if "pan card" in title:
            f.append(f"{item['id']}: a PAN-card item in groups A/B")
        if p.customer_category in ORGANISATIONS and "photo" in title:
            f.append(f"{item['id']}: an organisation photograph item")
        cites = item.get("citations", [])
        if not cites:
            f.append(f"{item['id']}: no citation")
        for c in cites:
            if c["document_key"] not in p.sources:
                f.append(f"{item['id']}: cites {c['document_key']} outside the cell's sources")
        if item.get("source_wording"):
            ok = [c for c in cites if c["quotable"] and item["source_wording"] in c["passage"]]
            if not ok:
                f.append(f"{item['id']}: quotation not in a quotable cited passage")
        if item.get("approximate_reading") and not any(
            item["approximate_reading"] in c["passage"] for c in cites
        ):
            f.append(f"{item['id']}: approximate reading not in a cited passage")
        if p.state == "COVERED_OCR" and not any(
            k["code"] == "scanned_text" for k in item.get("caveats", [])
        ):
            f.append(f"{item['id']}: scanned form item without the scanned_text caveat")
    # Internal citations: every chunk re-resolves in PostgreSQL to the same document, page,
    # text and reliability.
    if internal is not None and internal.groups is not None:
        g = internal.groups
        for ci in (*g.documents, *g.information, *g.not_confirmed):
            for c in (*ci.citations, *ci.related_citations):
                counts["citations"] += 1
                row = chunks.get(c.chunk_id)
                if row is None:
                    f.append(f"{ci.id}: chunk {c.chunk_id} not found")
                    continue
                problems = []
                if row["document_key"] != c.document_key:
                    problems.append("document")
                if row["page_number"] != c.page:
                    problems.append("page")
                if row["content"] != c.passage:
                    problems.append("passage text")
                if row["text_reliability"] != c.text_reliability:
                    problems.append("reliability")
                if row["bank"] != "HDFC":
                    problems.append("non-HDFC source")
                if problems:
                    f.append(f"{ci.id}: chunk {c.chunk_id} mismatch: {problems}")
                else:
                    counts["valid"] += 1
    else:
        f.append("no internal checklist captured")
    return f, dict(counts)


def audit_not_established(p: CoverageProfile, answer: dict[str, Any]) -> list[str]:
    f: list[str] = []
    w = wording()
    if answer.get("kind") != "fallback":
        f.append(f"kind {answer.get('kind')!r}, expected fallback")
    if answer.get("fallback") != w.fallback:
        f.append("fallback is not the verbatim A1 §7 text")
    if answer.get("checklist") is not None:
        f.append("a checklist was attached")
    if answer.get("coverage_state") != "NOT_ESTABLISHED":
        f.append(f"coverage_state {answer.get('coverage_state')}")
    if not answer.get("explanation"):
        f.append("no explanation")
    cov = coverage()
    org = p.customer_category in ORGANISATIONS
    for s in answer.get("suggestions") or []:
        if s["slot"] == "customer_category":
            if (s["value"] in ORGANISATIONS) != org:
                f.append(f"suggested {s['value']}: a different kind of holder")
            cell = cov.cell(CustomerCategory(s["value"]), AccountType(p.account_type or ""))
        else:
            cell = cov.cell(CustomerCategory(p.customer_category or ""), AccountType(s["value"]))
        if not cell.covered:
            f.append(f"suggested {s['value']}, which is not covered")
    return f


def audit_conversion(p: CoverageProfile, answer: dict[str, Any]) -> list[str]:
    f: list[str] = []
    if p.kind == "conversion":
        if answer.get("kind") != "checklist" or answer.get("coverage_state") != "CONVERSION_ONLY":
            f.append(
                f"not a conversion checklist ({answer.get('kind')}, {answer.get('coverage_state')})"
            )
        for item in _items(answer, "documents", "information"):
            for c in item.get("citations", []):
                if c["document_key"] != CONVERSION_DOC:
                    f.append(f"{item['id']}: cites {c['document_key']}")
    else:
        if answer.get("coverage_state") == "CONVERSION_ONLY":
            f.append("treated as a conversion request")
        for item in _items(answer, "documents", "information"):
            if any(c["document_key"] == CONVERSION_DOC for c in item.get("citations", [])):
                f.append(f"{item['id']}: the conversion form used as a basis")
    return f


@suite("coverage")
async def coverage_suite(ctx: Context) -> dict[str, Any]:
    service = ctx.stack.service()
    chunks = ChunkTable(ctx.dev_settings)
    profiles = load_profiles()
    golden: dict[str, list[str]] = (
        json.loads(GOLDEN_FILE.read_text("utf-8")) if GOLDEN_FILE.exists() else {}
    )
    recording = not golden
    rows: list[dict[str, Any]] = []
    citations = Counter[str]()
    item_ids: dict[str, list[str]] = {}
    try:
        for p in profiles:
            before = len(ctx.checklist.built)
            answer = await drive_profile(service, p)
            internal = ctx.checklist.built[-1][1] if len(ctx.checklist.built) > before else None
            if p.kind == "covered":
                failures, counts = audit_covered(p, answer, internal, chunks)
                citations.update(counts)
                ids = sorted(
                    i["id"] for i in _items(answer, "documents", "information", "not_confirmed")
                )
                item_ids[p.id] = ids
                if not recording and golden.get(p.id) != ids:
                    failures.append(
                        f"item ids differ from the golden file: {golden.get(p.id)} -> {ids}"
                    )
            elif p.kind == "not_established":
                failures = audit_not_established(p, answer)
            else:
                failures = audit_conversion(p, answer)
                if p.kind == "conversion" and internal is not None:
                    _, counts = audit_covered(
                        p.model_copy(
                            update={"state": "CONVERSION_ONLY", "customer_category": None}
                        ),
                        answer,
                        internal,
                        chunks,
                    )
                    citations.update(counts)
            rows.append({"id": p.id, "kind": p.kind, "failures": failures})
    finally:
        chunks.close()
    if recording:
        GOLDEN_FILE.write_text(json.dumps(item_ids, indent=1, sort_keys=True) + "\n", "utf-8")
    by_kind: dict[str, Counter[str]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], Counter())["pass" if not r["failures"] else "fail"] += 1
    complete = [r for r in rows if r["kind"] == "covered" and "notsure" not in r["id"]]
    passed = all(not r["failures"] for r in rows) and citations["citations"] == citations["valid"]
    md = header("coverage, conversion and evidence (E1, E2, E7)", ctx)
    md += "| Set | Pass | Fail |\n|---|---|---|\n"
    for kind, c in by_kind.items():
        md += f"| {kind} | {c['pass']} | {c['fail']} |\n"
    md += (
        f"\n- Complete covered profiles: {sum(not r['failures'] for r in complete)}/{len(complete)}"
        f" (26 cells).\n"
        f"- Citations re-resolved in PostgreSQL (document, page, passage text, reliability, "
        f"HDFC): **{citations['valid']}/{citations['citations']}**.\n"
        f"- Golden item ids: {'recorded now (first run)' if recording else 'compared'} "
        f"(`data/eval/a7_golden_items.json`).\n"
        f"- Result: **{'PASS' if passed else 'FAIL'}**.\n"
    )
    failing = [r for r in rows if r["failures"]]
    if failing:
        md += "\n## Failures\n\n" + "".join(
            f"- `{r['id']}`: {'; '.join(r['failures'][:5])}\n" for r in failing
        )
    write_report(
        "coverage",
        md,
        {
            "provenance": ctx.provenance,
            "rows": rows,
            "citations": dict(citations),
            "passed": passed,
        },
    )
    return {"passed": passed}


# --- the human citation-review sheet -------------------------------------------------------------


def _pdf_text(document_key: str, page: int) -> str | None:
    import pymupdf  # already a project dependency
    import yaml

    manifest = yaml.safe_load(
        (REPO_ROOT / "data" / "raw_pdfs" / "manifest.yaml").read_text("utf-8")
    )
    entry = next((d for d in manifest if d["document_key"] == document_key), None)
    if entry is None:
        return None
    path = REPO_ROOT / "data" / "raw_pdfs" / entry["file"]
    if not path.exists():
        return None
    with pymupdf.open(path) as doc:  # type: ignore[no-untyped-call]
        if not 1 <= page <= doc.page_count:
            return None
        return str(doc[page - 1].get_text())


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@suite("citation_review")
async def citation_review(ctx: Context) -> dict[str, Any]:
    """30 seeded items (15 NRI, 15 organisation) for the owner to check against the PDFs.
    The automated pre-check only looks for the wording in the PDF's text layer; the human
    outcome stays "pending" until the owner has reviewed the page."""
    if not ctx.checklist.built:
        service = ctx.stack.service()
        for p in load_profiles():
            if p.kind == "covered" and "notsure" not in p.id:
                await drive_profile(service, p)
    pool: dict[str, dict[tuple[str, str], tuple[Any, Any]]] = {"nri": {}, "org": {}}
    for ready, ans in ctx.checklist.built:
        if ans.groups is None:
            continue
        cat = ready.customer_category
        if cat is None:  # the conversion checklist: not part of the 15 + 15 sample
            continue
        side = "nri" if cat == CustomerCategory.NRI_INDIVIDUAL else "org"
        for item in (*ans.groups.documents, *ans.groups.information):
            for c in item.citations:
                if c.basis:
                    pool[side].setdefault((item.id, c.chunk_id), (item, c))
    rng = random.Random(20261115)  # noqa: S311 - a reproducible sample, not security
    # 15 per side where available; the organisation cells share one scanned form, so any
    # shortfall there is filled from NRI items to reach 30 distinct (item, passage) pairs.
    org_keys = sorted(pool["org"])
    org = rng.sample(org_keys, min(15, len(org_keys)))
    nri_keys = sorted(pool["nri"])
    nri = rng.sample(nri_keys, min(30 - len(org), len(nri_keys)))
    sample = [("nri", *pool["nri"][k]) for k in nri] + [("org", *pool["org"][k]) for k in org]
    lines = [
        header("citation review sheet (E7, human check)", ctx),
        "Each row: the checklist claim, the cited source and page, and the supporting passage. "
        "**Review outcome** is filled in only by a person who opened the PDF page; nothing here "
        "is marked human-verified by the tooling. The automated pre-check only searches the PDF's "
        "text layer (scanned pages have none and need a visual check).\n",
        "| # | Checklist claim | Classification | Source document (key) | Page "
        "| Supporting passage | Automated pre-check | Review outcome |",
        "|---|---|---|---|---|---|---|---|",
    ]
    record = []
    for n, (side, item, c) in enumerate(sample, 1):
        quote = item.source_wording or item.approximate_reading or ""
        text = _pdf_text(c.document_key, c.page)
        if text is None:
            pre = "PDF not available locally"
        elif not text.strip():
            pre = "scanned page (no text layer): visual check needed"
        elif quote and _norm(quote) in _norm(text):
            pre = "wording found in the PDF text layer on this page"
        else:
            pre = "wording NOT found in the text layer: check carefully"
        kind = "approximate reading (OCR)" if item.approximate_reading else "quotation"
        passage = f"{kind}: “{quote}”" if quote else "(no quoted wording)"
        claim = item.title + (f" — condition: {item.condition}" if item.condition else "")
        lines.append(
            f"| {n} | {claim} | {item.classification_label} | {c.document_title} "
            f"(`{c.document_key}`) | {c.page} | {passage.replace('|', '/')} | {pre} | "
            "Pending human review |"
        )
        record.append(
            {"n": n, "side": side, "item": item.id, "chunk": c.chunk_id, "document": c.document_key,
             "page": c.page, "quote": quote, "precheck": pre, "outcome": "pending"}
        )  # fmt: skip
    lines.append(
        "\nTo record a review, replace *Pending human review* with *Match* or *Mismatch: <note>* "
        "and add the reviewer and date below.\n\nReviewer: ______  Date: ______\n"
    )
    write_report("citation_review", "\n".join(lines) + "\n", {"rows": record})
    return {"passed": len(sample) == 30}
