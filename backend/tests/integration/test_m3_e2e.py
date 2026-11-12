"""M3 S8: the LLP demo end to end through the API, on the real ingested corpus.

The real app, Postgres checkpointer, workflow tables and insert-only report store; the
real retriever (BM25 over the real chunks; the fixture's embedder is fake) and corpus
reader with the stored form schemas; a scripted LLM (no live call; Gemini is spied and
must never be called). The synthesis draft cites evidence ids that retrieval actually
supplied at run time, plus one bogus id that the validator must strip. Nothing here is
hardcoded evidence.

The report is judged by `app.workflow.acceptance.check_report`, the same rules the live
demo uses: every item cites an existing chunk (document key and page checked against
Postgres), no MANDATORY without a primary citation, unresolved items listed, an
honestly labelled outcome. The SSE stream is opened before the first message.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.graph.synthesis import ChecklistDraft, DraftRequirement
from app.llm.common import parse_structured, text_sha256
from app.llm.provider import LLMRequest, LLMResult
from app.workflow.acceptance import check_report, db_chunk_lookup
from app.workflow.demo import ANSWERS, demo_query
from tests.integration.test_graph_s4_corpus import no_gemini, world  # noqa: F401 (fixtures)
from tests.integration.workflow_harness import api, new_session, wait_for

pytestmark = pytest.mark.integration
BOGUS = "00000000-0000-4000-8000-000000000000"


class CitingLLM:
    """Intake: LLP only (so account_type is asked). Synthesis: one requirement per supplied
    evidence id (the first proposed MANDATORY, the rest CONDITIONAL), plus a bogus id."""

    name = "fake"
    model_id = "fake-llm-1"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_structured(self, request: LLMRequest, schema: Any) -> LLMResult[Any]:
        self.calls.append(schema.__name__)
        if schema is ChecklistDraft:
            ids = re.findall(r'<evidence id="([^"]+)"', request.user)[:3]
            items = [
                DraftRequirement(
                    name=f"Requirement {n + 1}",
                    explanation="As cited.",
                    applicability="MANDATORY" if n == 0 else "CONDITIONAL",
                    evidence_ids=[x] if n else [x, BOGUS],
                )
                for n, x in enumerate(ids)
            ]
            raw = ChecklistDraft(requirements=items).model_dump_json()
        else:
            raw = IntakeExtraction(
                entity_type=ExtractedValue(value="LLP", evidence_span="an LLP"),
                residency=ExtractedValue(),
                account_type=ExtractedValue(),
            ).model_dump_json()
        parsed, errors = parse_structured(raw, schema)
        return LLMResult[schema](  # type: ignore[valid-type]
            parsed=parsed,
            raw_text_sha256=text_sha256(raw),
            validation_errors=errors,
            provider=self.name,
            model_id=self.model_id,
            latency_ms=0,
        )


def post(client: TestClient, path: str, body: Any, key: str) -> Any:
    return client.post(f"/api/v1{path}", content=json.dumps(body), headers={"Idempotency-Key": key})


def test_llp_demo_pauses_resumes_and_meets_the_m3_acceptance(
    world: tuple[Any, Any], corpus_db: sessionmaker[Session]  # noqa: F811 (pytest fixture)
) -> None:
    retriever, corpus = world
    llm = CitingLLM()
    query = demo_query(get_settings())
    with api(llm, retriever=retriever, corpus=corpus, retrieval_mode="bm25") as client:
        sid = new_session(client)
        with ThreadPoolExecutor(1) as pool:
            stream = pool.submit(lambda: client.get(f"/api/v1/sessions/{sid}/events").text)
            time.sleep(0.3)  # the stream is open before the first message
            assert (
                post(client, f"/sessions/{sid}/messages", {"message": query}, "m").status_code
                == 202
            )
            clar = wait_for(client, sid, "awaiting_clarification", 120)["workflow"]["clarification"]
            assert [q["field"] for q in clar["questions"]] == ["account_type"]
            body = {
                "checkpoint_id": clar["checkpoint_id"],
                "answers": {"account_type": ANSWERS["account_type"]},
            }
            assert post(client, f"/sessions/{sid}/resume", body, "r").status_code == 202
            wf = wait_for(client, sid, "completed", 180)["workflow"]
            events = stream.result(timeout=60)
        report = client.get(f"/api/v1/reports/{wf['report_id']}").json()

    with corpus_db() as db:
        result = check_report(report, db_chunk_lookup(db))
    assert result.ok, result.problems
    # M4 S5: with a checklist, claim verification runs with NLI disabled -> DEGRADED
    assert report["outcome"] in ("DEGRADED", "INSUFFICIENT_EVIDENCE")
    if report["outcome"] == "DEGRADED":
        assert result.items >= 1 and result.citations >= result.items
        assert [d["component"] for d in report["degradations"]] == ["verification"]
        assert report["report_version"] == 2
    # the bogus id never survives into a citation
    assert BOGUS not in json.dumps(report["checklist"] or [])
    # the LLP group scope is never MANDATORY (S4 carry-forward)
    assert all(i["applicability"] != "MANDATORY" for i in report["checklist"] or [])
    assert report["profile"]["account_type"] == {
        "value": "CURRENT_ACCOUNT",
        "source": "user_answered",
    }
    assert llm.calls.count("ChecklistDraft") >= 1
    # the stream saw every node, ending in `completed`, and never carried report content
    last = events.strip().split("\n\n")[-1]
    assert '"node":"finalize"' in events
    assert '"kind":"run"' in last and '"status":"completed"' in last
    assert query not in events and wf["report_id"] not in events
