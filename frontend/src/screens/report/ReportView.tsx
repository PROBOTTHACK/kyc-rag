import type { ReactNode } from "react";

import type { components } from "../../api/schema";
import type { ComplianceReport } from "../../api/types";
import { OUTCOME_TEXT } from "../assistant/labels";
import { CitationNote, Citations } from "./Citations";
import {
  APPLICABILITY,
  APPLICABILITY_ORDER,
  codeText,
  CONFLICT_SCAN_FAILED,
  CONFLICT_SCAN_OFF,
  conflictScanBounded,
  DEGRADATION_COMPONENT,
  ELIGIBILITY,
  EVIDENCE_STATUS,
  EXPLANATION_STATUS,
  FORM_METHOD,
  formFlag,
  LINK_KIND,
  pages,
  PROFILE_FIELD,
  PROFILE_SOURCE,
  RESOLUTION_REASON,
  REVIEW_REASON,
  SOURCE_STATUS,
  VERDICT,
  VERIFICATION,
} from "./labels";

type S = components["schemas"];
type Item = S["ValidatedRequirement"];

/**
 * One stored report, v1 (M3) or v2 (M4), rendered from its own fields only. Nothing is
 * inferred: an absent field is shown as absent, and every status is the report's own.
 */
export function ReportView({ report }: { report: ComplianceReport }) {
  const v2 = report.report_version === 2;
  const meta = report.verification_meta ?? null;
  const results = new Map((report.claim_verification ?? []).map((r) => [r.requirement_id, r]));
  const checklist = report.checklist ?? null;
  const outcome = OUTCOME_TEXT[report.outcome];

  return (
    <article aria-labelledby="report-outcome">
      <section className="mt-4 rounded border border-slate-300 bg-white p-4">
        <h2 id="report-outcome" className="text-xl font-semibold">
          Outcome: {outcome.label}
        </h2>
        <p className="mt-1 text-sm">{outcome.detail}</p>
        <p className="mt-2 rounded bg-amber-50 p-2 text-sm font-medium text-amber-950">
          Human review required. {report.notice ?? ""}
        </p>
        <p className="mt-2 text-xs text-slate-500">
          Report format v{report.report_version ?? 1} (
          {v2 ? "with the claim-checking stage" : "before claim checking existed"}).
        </p>
      </section>

      <section aria-labelledby="message-title" className="mt-4">
        <h2 id="message-title" className="text-lg font-semibold">
          Summary
        </h2>
        {report.user_message.split(/\n{2,}/).map((p, i) => (
          <p key={i} className="mt-1 whitespace-pre-line text-sm">
            {p}
          </p>
        ))}
      </section>

      <Profile report={report} />

      <section aria-labelledby="checks-title" className="mt-4">
        <h2 id="checks-title" className="text-lg font-semibold">
          Checks
        </h2>
        <dl className="mt-1 space-y-1 text-sm">
          {report.evidence_status != null && (
            <Row term="Evidence">{EVIDENCE_STATUS[report.evidence_status]}</Row>
          )}
          <Row term="Claim checking">
            {v2 && meta?.status === "not_configured"
              ? "Automated claim checking is not enabled, so every claim is marked not verified. This is not a finding that any item is wrong."
              : VERIFICATION[report.verification ?? "not_claim_verified"]}
            {meta != null && (meta.status === "ran" || meta.status === "partial") && (
              <span className="block text-xs text-slate-500">
                Model {meta.model_id} · thresholds {meta.thresholds_version} · scores are
                uncalibrated signals, not confidence.
              </span>
            )}
          </Row>
          <Row term="Conflicting sources">
            {meta === null || meta.contradiction_scan === "not_run"
              ? CONFLICT_SCAN_OFF
              : meta.contradiction_scan === "failed"
                ? CONFLICT_SCAN_FAILED
                : conflictScanBounded(
                    meta.contradiction_scan_size ?? 0,
                    meta.contradiction_pairs_not_scanned ?? 0,
                  )}
          </Row>
        </dl>
      </section>

      <section aria-labelledby="checklist-title" className="mt-6">
        <h2 id="checklist-title" className="text-xl font-semibold">
          Checklist
        </h2>
        {checklist === null ? (
          <p className="mt-1 text-sm">
            No checklist is shown for this outcome. See the summary above for the reason.
          </p>
        ) : checklist.length === 0 ? (
          <p className="mt-1 text-sm">
            No checklist item remained after validation and checking. This does not mean that
            nothing is required.
          </p>
        ) : (
          <>
            {APPLICABILITY_ORDER.map((a) => {
              const items = checklist.filter((i) => i.applicability === a);
              if (items.length === 0) return null;
              return (
                <section key={a} aria-label={APPLICABILITY[a].label} className="mt-3">
                  <h3 className="text-lg font-medium">
                    {APPLICABILITY[a].label} ({items.length})
                  </h3>
                  <p className="text-sm text-slate-600">{APPLICABILITY[a].detail}</p>
                  {items.map((item) => (
                    <Requirement
                      key={item.requirement_id}
                      item={item}
                      result={results.get(item.requirement_id)}
                    />
                  ))}
                </section>
              );
            })}
            <CitationNote />
          </>
        )}
        {(report.dropped_requirements ?? 0) > 0 && (
          <p className="mt-2 text-sm">
            {report.dropped_requirements} drafted item(s) were dropped because they cited no valid
            source.
          </p>
        )}
      </section>

      <Unverified report={report} />
      <Conflicts report={report} />
      <OpenPoints report={report} />
      <Forms report={report} />
    </article>
  );
}

function Row({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="sm:flex sm:gap-2">
      <dt className="font-medium sm:w-44 sm:shrink-0">{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Requirement({
  item,
  result,
}: {
  item: Item;
  result: S["RequirementVerification"] | undefined;
}) {
  const verdict = VERDICT[item.verification_status ?? "not_claim_verified"];
  const lowered = item.applicability !== item.proposed_applicability;
  return (
    <div
      role="group"
      aria-label={`${item.name} (${item.requirement_id})`}
      className="mt-2 rounded border border-slate-200 bg-white p-3"
    >
      <h4 className="font-semibold">{item.name}</h4>
      <p className="text-sm">
        <span className="font-medium">Applicability:</span>{" "}
        {APPLICABILITY[item.applicability].label}
        {lowered && (
          <span className="text-slate-600">
            {" "}
            (proposed as {APPLICABILITY[item.proposed_applicability].label}; lowered by validation
            or checking)
          </span>
        )}
      </p>
      <p className="text-sm">
        <span className="font-medium">Claim check:</span> {verdict.label}.{" "}
        <span className="text-slate-600">{verdict.detail}</span>
      </p>
      <p className="text-sm">
        <span className="font-medium">Validator note:</span> {item.uncertainty_label}
      </p>
      {item.explanation !== "" && (
        <div className="mt-1 text-sm">
          <p>{item.explanation}</p>
          <p className="text-xs text-slate-600">
            {EXPLANATION_STATUS[item.explanation_status ?? "model_drafted_not_verified"]}
          </p>
        </div>
      )}
      <Citations citations={item.citations} />
      {(item.related_form_fields ?? []).length > 0 && (
        <div className="mt-1 text-sm">
          <p className="font-medium">
            Related form fields (the form asks for this; not an obligation):
          </p>
          <ul className="list-disc pl-5">
            {(item.related_form_fields ?? []).map((f) => (
              <li key={`${f.form_key}:${f.field_label}`}>
                <span className="font-mono">{f.form_key}</span>: {f.field_label} (
                {LINK_KIND[f.link_kind]})
              </li>
            ))}
          </ul>
        </div>
      )}
      {result && (result.review_flags ?? []).length > 0 && (
        <p className="mt-1 text-sm">
          <span className="font-medium">Flagged for review:</span>{" "}
          {(result.review_flags ?? []).map((f) => REVIEW_REASON[f]).join("; ")}
        </p>
      )}
    </div>
  );
}

function Profile({ report }: { report: ComplianceReport }) {
  const entries = Object.entries(report.profile ?? {});
  const missing = report.missing_fields ?? [];
  if (entries.length === 0 && missing.length === 0) return null;
  return (
    <section aria-labelledby="profile-title" className="mt-4">
      <h2 id="profile-title" className="text-lg font-semibold">
        Customer profile used
      </h2>
      <dl className="mt-1 space-y-1 text-sm">
        {entries.map(([field, p]) => (
          <Row key={field} term={PROFILE_FIELD[field] ?? field}>
            {p.value ?? "not known"}{" "}
            <span className="text-slate-600">({PROFILE_SOURCE[p.source]})</span>
          </Row>
        ))}
      </dl>
      {missing.length > 0 && (
        <p className="mt-1 text-sm">
          Missing details: {missing.map((f) => PROFILE_FIELD[f] ?? f).join(", ")}.
        </p>
      )}
    </section>
  );
}

function Unverified({ report }: { report: ComplianceReport }) {
  const items = report.unverified_items ?? [];
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="unverified-title" className="mt-6">
      <h2 id="unverified-title" className="text-xl font-semibold">
        Set aside for review
      </h2>
      <p className="text-sm text-slate-600">
        The cited text did not support these items in the automated check. They are listed here for
        human review. This does not mean they are not required.
      </p>
      {items.map((u) => (
        <div
          key={u.requirement_id}
          role="group"
          aria-label={`${u.name} (${u.requirement_id})`}
          className="mt-2 rounded border border-slate-200 bg-white p-3"
        >
          <h3 className="font-semibold">{u.name}</h3>
          <p className="text-sm">
            Applicability before checking: {APPLICABILITY[u.applicability_before].label}
          </p>
          {(u.reason_codes ?? []).length > 0 && (
            <p className="text-sm">
              Reason codes: <span className="font-mono">{(u.reason_codes ?? []).join(", ")}</span>
            </p>
          )}
          <Citations citations={u.citations} />
        </div>
      ))}
      <CitationNote />
    </section>
  );
}

function Conflicts({ report }: { report: ComplianceReport }) {
  const conflicts = report.source_conflicts ?? [];
  if (conflicts.length === 0) return null;
  const names = new Map((report.checklist ?? []).map((i) => [i.requirement_id, i.name]));
  const side = (s: S["ConflictSide"]) => (
    <>
      <span className="font-mono">{s.document_key}</span>, {pages(s.page, s.page_end)} ·{" "}
      {ELIGIBILITY[s.evidence_eligibility]} · {SOURCE_STATUS[s.source_status]}
      {s.cited ? " · cited" : " · retrieved, not cited"}
    </>
  );
  return (
    <section aria-labelledby="conflicts-title" className="mt-6">
      <h2 id="conflicts-title" className="text-xl font-semibold">
        Possible conflicts between sources
      </h2>
      <p className="text-sm text-slate-600">
        An automated check read these passages as contradicting each other. Both sources are shown;
        a newer date never settles a conflict.
      </p>
      <ul className="mt-2 space-y-2 text-sm">
        {conflicts.map((c, i) => (
          <li key={i} className="rounded border border-slate-200 bg-white p-3">
            <p className="font-medium">{names.get(c.requirement_id) ?? c.requirement_id}</p>
            <p>Source A: {side(c.side_a)}</p>
            <p>Source B: {side(c.side_b)}</p>
            <p>
              {c.resolution === "unresolved" ? "Unresolved" : "Resolved"}:{" "}
              {RESOLUTION_REASON[c.resolution_reason]}.
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

function OpenPoints({ report }: { report: ComplianceReport }) {
  const unresolved = report.unresolved_items ?? [];
  const gaps = (report.gaps ?? []).filter((g) => !unresolved.includes(g));
  const degradations = report.degradations ?? [];
  const findings = report.findings ?? [];
  const security = report.security_reasons ?? [];
  if (
    !unresolved.length &&
    !gaps.length &&
    !degradations.length &&
    !findings.length &&
    !security.length
  ) {
    return null;
  }
  return (
    <section aria-labelledby="open-title" className="mt-6">
      <h2 id="open-title" className="text-xl font-semibold">
        Open points
      </h2>
      {unresolved.length > 0 && <CodeList title="Unresolved items" codes={unresolved} />}
      {gaps.length > 0 && <CodeList title="Evidence gaps" codes={gaps} />}
      {degradations.length > 0 && (
        <div className="mt-2">
          <h3 className="font-medium">Parts that did not run normally</h3>
          <ul className="list-disc pl-5 text-sm">
            {degradations.map((d, i) => (
              <li key={i}>
                {DEGRADATION_COMPONENT[d.component]}{" "}
                <span className="font-mono text-xs">({d.reason})</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {findings.length > 0 && (
        <div className="mt-2">
          <h3 className="font-medium">Validation findings</h3>
          <ul className="list-disc pl-5 text-sm">
            {findings.map((f, i) => (
              <li key={i}>
                <span className="font-mono text-xs">{f.kind}</span>
                {f.requirement_id != null && <> ({f.requirement_id})</>}: {f.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
      {security.length > 0 && (
        <p className="mt-2 text-sm">
          Input screening codes: <span className="font-mono">{security.join(", ")}</span>
        </p>
      )}
    </section>
  );
}

function CodeList({ title, codes }: { title: string; codes: readonly string[] }) {
  return (
    <div className="mt-2">
      <h3 className="font-medium">{title}</h3>
      <ul className="list-disc pl-5 text-sm">
        {codes.map((c) => (
          <li key={c}>
            {codeText(c)} <span className="font-mono text-xs text-slate-500">({c})</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Forms({ report }: { report: ComplianceReport }) {
  const forms = report.forms ?? [];
  if (forms.length === 0) return null;
  return (
    <section aria-labelledby="forms-title" className="mt-6">
      <h2 id="forms-title" className="text-xl font-semibold">
        Forms analysed
      </h2>
      <p className="text-sm text-slate-600">
        What a blank form asks for. Form fields are never policy obligations.
      </p>
      <ul className="mt-1 list-disc pl-5 text-sm">
        {forms.map((f) => (
          <li key={`${f.form_key}:${f.method}:${f.version}`}>
            <span className="font-mono">{f.form_key}</span> (version {f.version},{" "}
            {FORM_METHOD[f.method]}
            ): {f.fields_analysed} field(s) analysed
            {f.subset_only && " (only a subset of the form)"}
            {(f.flags ?? []).length > 0 && <> · {(f.flags ?? []).map(formFlag).join(", ")}</>}
          </li>
        ))}
      </ul>
    </section>
  );
}
