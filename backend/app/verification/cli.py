"""M4 claim-verification CLI (S4): `check-model`, `evaluate`, `select`. DEV set only.

    python -m app.verification.cli check-model [--candidate N1]
    python -m app.verification.cli evaluate --candidate N2 --tau-e 0.9 --tau-c 0.9
    python -m app.verification.cli select --record data/eval/m4_nli_selection.json \
        --doc docs/milestones/M4_NLI_SELECTION.md

`select` applies the pre-registered rule (`app.verification.selection`) once and refuses
to overwrite its record. Nothing here reads the held-out set. Models load lazily from the
local Hugging Face cache at their pinned revisions (a first run downloads them: owner
decision D12). Exit codes: 0 ok, 1 a check failed, 2 invalid use.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT, get_settings
from app.core.logging import configure_logging
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.graph.corpus import PostgresCorpusReader
from app.graph.state import EvidenceRole
from app.retrieval.selection import bootstrap_ci
from app.verification.decide import Thresholds
from app.verification.evaluation import (
    CachedScorer,
    Case,
    Prediction,
    compute_metrics,
    fraction_json,
    latency_summary,
    load_dev,
    metrics_json,
    predict,
    premises_for,
)
from app.verification.nli import CrossEncoderNLI, NLIError
from app.verification.premises import Premise
from app.verification.selection import (
    CANDIDATES,
    DETERMINISM_TOLERANCE,
    FSR_CAP,
    GRID,
    PROTOCOL_VERSION,
    Candidate,
    Outcome,
    decide,
    outcome_json,
    threshold_grid,
    write_record,
)

HELDOUT = REPO_ROOT / "data" / "eval" / "m4_claims_heldout.jsonl"
HELDOUT_SHA256 = "eee0a575319d7df4bc3d8f5c0771a89e4c8bf9f68abe36eb56fcac3275d90203"
# Thresholds for the scorer-free ablation only (no score is ever compared with them).
UNUSED_THRESHOLDS = Thresholds(0.9, 0.9, "rules-only:no-scorer")
LIMITATIONS = (
    "Thresholds and checkpoint were chosen on 55 dev cases drafted by one annotator who "
    "also built the verifier; the choice overfits this set. The one-time held-out run (S8) "
    "is the only unbiased estimate and is reported as is. Scores are uncalibrated model "
    "signals, never a confidence. Bootstrap intervals are reported, never decisive."
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _commit() -> str:
    try:
        cmd = ["git", "rev-parse", "HEAD"]  # noqa: S607 - fixed command, no user input
        out = subprocess.run(  # noqa: S603 - fixed argument list
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=10
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _candidate(cid: str) -> Candidate:
    for c in CANDIDATES:
        if c.id == cid:
            return c
    raise SystemExit(f"unknown candidate {cid!r}; choose from {[c.id for c in CANDIDATES]}")


def _dev() -> tuple[list[Case], str, dict[str, list[Premise]], dict[str, set[EvidenceRole]]]:
    settings = get_settings()
    cases, sha = load_dev()
    corpus = PostgresCorpusReader(create_sync_sessionmaker(create_sync_engine(settings)))
    premises = {c.id: premises_for(c, corpus) for c in cases}
    roles = {cid: {p.evidence_role for p in ps} for cid, ps in premises.items()}
    return cases, sha, premises, roles


def _predict_all(
    cases: Sequence[Case],
    premises: dict[str, list[Premise]],
    scorer: CachedScorer | None,
    thresholds: Thresholds,
    mode: str,
) -> list[Prediction]:
    max_pairs = get_settings().NLI_MAX_PAIRS
    return [
        predict(c, premises[c.id], scorer=scorer, thresholds=thresholds, max_pairs=max_pairs,
                mode=mode)  # type: ignore[arg-type]
        for c in cases
    ]  # fmt: skip


def _load(candidate: Candidate) -> tuple[CrossEncoderNLI | None, dict[str, Any]]:
    model = CrossEncoderNLI(
        candidate.model_id, candidate.revision, batch_size=get_settings().NLI_BATCH_SIZE
    )
    info: dict[str, Any] = {"candidate": candidate.id, "model_id": candidate.model_id,
                            "revision": candidate.revision}  # fmt: skip
    try:
        model.load()
    except NLIError as exc:
        info |= {"loaded": False, "excluded_reason": str(exc)[:500]}
        return None, info
    info |= {"loaded": True, "label_map": model.label_map, "sanity": model.sanity,
             "load_ms": round(model.load_ms, 1)}  # fmt: skip
    return model, info


def _determinism(model: CrossEncoderNLI, cached: CachedScorer) -> float:
    """Re-score every cached pair once; the largest absolute logit difference."""
    keys = [k for k, v in cached.cache.items() if v is not None]
    from app.verification.nli import NLIPair

    again = model.score([NLIPair(a, b) for a, b in keys])
    worst = 0.0
    for key, second in zip(keys, again, strict=True):
        first = cached.cache[key]
        if first is None or second is None:
            return float("inf")
        worst = max(worst, *(abs(x - y) for x, y in zip(first.logits, second.logits, strict=True)))
    return worst


def cmd_check_model(args: argparse.Namespace) -> int:
    chosen = [_candidate(args.candidate)] if args.candidate else list(CANDIDATES)
    ok = True
    for c in chosen:
        _, info = _load(c)
        ok &= bool(info["loaded"])
        print(json.dumps(info, indent=2))
    return 0 if ok else 1


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Dev metrics for one checkpoint at given thresholds. Selects nothing."""
    cases, sha, premises, roles = _dev()
    model, info = _load(_candidate(args.candidate))
    if model is None:
        print(json.dumps(info, indent=2))
        return 1
    cached = CachedScorer(model)
    t = Thresholds(args.tau_e, args.tau_c, f"adhoc:e{args.tau_e}:c{args.tau_c}")
    preds = _predict_all(cases, premises, cached, t, "combined")
    out = {
        "dev_sha256": sha,
        "model": info,
        "thresholds": [t.entail, t.contradict],
        "metrics": metrics_json(compute_metrics(cases, preds, roles)),
        "latency": latency_summary(cached),
        "note": "evaluation only; selects nothing",
    }
    print(json.dumps(out, indent=2))
    return 0


def _correct(cases: Sequence[Case], preds: Sequence[Prediction]) -> tuple[Fraction, ...]:
    by = {p.case_id: p for p in preds}
    return tuple(Fraction(int(by[c.id].label == c.gold)) for c in cases)


def cmd_select(args: argparse.Namespace) -> int:
    record_path, doc_path = Path(args.record), Path(args.doc)
    if record_path.exists():
        print(f"refused: {record_path} exists; the selection runs once (no retuning)")
        return 2
    if _sha(HELDOUT) != HELDOUT_SHA256:
        print("refused: the frozen held-out file changed")
        return 2
    cases, sha, premises, roles = _dev()
    rules_only = _predict_all(cases, premises, None, UNUSED_THRESHOLDS, "rules_only")
    models: list[dict[str, Any]] = []
    outcomes: list[Outcome] = []
    nli_only: dict[str, dict[str, Any]] = {}
    preds_by: dict[tuple[str, str], list[Prediction]] = {}
    for cand in CANDIDATES:
        model, info = _load(cand)
        models.append(info)
        if model is None:
            continue
        cached = CachedScorer(model)
        grid = threshold_grid(f"{PROTOCOL_VERSION}:{cand.id}")
        first = _predict_all(cases, premises, cached, grid[0], "combined")  # fills the cache
        worst = _determinism(model, cached)
        info |= {"determinism_max_abs_logit_diff": worst,
                 "deterministic": worst <= DETERMINISM_TOLERANCE,
                 "latency": latency_summary(cached)}  # fmt: skip
        p50 = float(info["latency"].get("per_pair_p50_ms", 0.0))
        only = _predict_all(cases, premises, cached, grid[0], "nli_only")
        nli_only[cand.id] = metrics_json(compute_metrics(cases, only, roles))
        for t in grid:
            preds = first if t == grid[0] else _predict_all(cases, premises, cached, t, "combined")
            preds_by[(cand.id, t.version)] = preds
            outcomes.append(
                Outcome(cand, t, compute_metrics(cases, preds, roles), sanity_ok=True,
                        deterministic=bool(info["deterministic"]), p50_ms=p50)  # fmt: skip
            )
    decision = decide(outcomes)
    selected = decision.selected
    bootstrap: dict[str, Any] = {}
    if selected is not None:
        chosen = preds_by[(selected.candidate.id, selected.thresholds.version)]
        lo, hi = bootstrap_ci(_correct(cases, chosen), _correct(cases, rules_only))
        bootstrap["accuracy_selected_minus_rules_only"] = [round(lo, 4), round(hi, 4)]
    record: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code_commit": _commit(),
        "dev_path": "data/eval/m4_claims_dev.jsonl",
        "dev_sha256": sha,
        "heldout_sha256_checked": HELDOUT_SHA256,
        "rule": {"grid": list(GRID), "fsr_cap": fraction_json(FSR_CAP),
                 "objective": "max entailed recall",
                 "tie_breaks": ["false_contradiction_rate", "-macro_f1", "size_rank", "-tau_e",
                                "-tau_c", "p50_ms", "candidate_id"]},  # fmt: skip
        "models": models,
        "ablations": {"rules_only": metrics_json(compute_metrics(cases, rules_only, roles)),
                      "nli_only": nli_only},  # fmt: skip
        "outcomes": [outcome_json(o) for o in outcomes],
        "qualifiers": len(decision.qualifiers),
        "selected": outcome_json(selected) if selected else None,
        "bootstrap_95ci": bootstrap,
        "limitations": LIMITATIONS,
    }
    write_record(record_path, record)
    doc_path.write_text(render(record), encoding="utf-8")
    print(
        f"record: {record_path}\nselected: {record['selected'] and record['selected']['candidate']}"
    )
    return 0


def render(record: dict[str, Any]) -> str:
    """A short human-readable summary of the committed record."""
    sel = record["selected"]
    lines = [
        "# M4 S4 NLI selection (dev set only)",
        "",
        f"- **Protocol:** `{record['protocol']}`, run once at commit `{record['code_commit']}`.",
        f"- **Dev file SHA-256:** `{record['dev_sha256']}`.",
        "- **Held-out set:** not read for scoring (S8 only).",
        f"- **Qualifying configurations:** {record['qualifiers']} of {len(record['outcomes'])}.",
        "",
    ]
    if sel is None:
        lines += ["**No configuration qualified.** NLI stays off by default (decision D5).", ""]
    else:
        m = sel["metrics"]
        lines += [
            f"**Selected:** {sel['candidate']} at tau_e={sel['tau_e']}, tau_c={sel['tau_c']}.",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Entailed recall | {m['recall']['entailed']['value']} |",
            f"| False-support rate | {m['false_support_rate']['value']} |",
            f"| False-contradiction rate | {m['false_contradiction_rate']['value']} |",
            f"| Macro-F1 | {m['macro_f1']['value']} |",
            f"| Abstention rate | {m['abstention_rate']['value']} |",
            f"| MANDATORY false positives | {m['mandatory_false_positives']} |",
            "",
        ]
    lines += ["## Models", ""]
    for info in record["models"]:
        state = "loaded" if info.get("loaded") else f"excluded: {info.get('excluded_reason')}"
        lines.append(f"- {info['candidate']} `{info['model_id']}@{info['revision'][:8]}`: {state}")
    lines += ["", "## Limitations", "", record["limitations"], ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging(get_settings().APP_ENV)
    parser = argparse.ArgumentParser(prog="app.verification.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check-model", help="load a pinned candidate and run its checks")
    p.add_argument("--candidate")
    p.set_defaults(func=cmd_check_model)
    p = sub.add_parser("evaluate", help="dev metrics at fixed thresholds (selects nothing)")
    p.add_argument("--candidate", required=True)
    p.add_argument("--tau-e", type=float, required=True)
    p.add_argument("--tau-c", type=float, required=True)
    p.set_defaults(func=cmd_evaluate)
    p = sub.add_parser("select", help="apply the registered rule once and write the record")
    p.add_argument("--record", required=True)
    p.add_argument("--doc", required=True)
    p.set_defaults(func=cmd_select)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
