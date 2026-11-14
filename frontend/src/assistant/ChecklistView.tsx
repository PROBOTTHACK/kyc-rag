import { useId, useState, type RefObject } from "react";

import type { AssistantChecklist, AssistantItem, SourceCitation } from "../api/types";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { GROUPS, ITEM_KIND_LABELS, TEXT } from "./labels";
import { WarningIcon } from "./WelcomeScreen";

/**
 * The personalised checklist, exactly as the API built it (A4 via A5). Groups, items,
 * classifications, conditions, quotations, pages and sources are rendered from the answer;
 * nothing is added, removed, merged or re-ordered here. Group C appears only when the API
 * sends entries for it (the customer asked about one of its topics).
 */
export function ChecklistView({
  checklist,
  notice,
  headingRef,
}: {
  checklist: AssistantChecklist;
  notice: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
}) {
  useFocusWhenLost(headingRef); // a choice card that produced the checklist has gone
  const groups = (["documents", "information", "not_confirmed"] as const).filter(
    (g) => checklist.groups[g].length > 0,
  );
  return (
    <section
      aria-labelledby="checklist-heading"
      className="rounded-2xl bg-white shadow-sm ring-1 ring-slate-200 print:shadow-none print:ring-0"
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 p-4 sm:p-6">
        <div>
          <h2
            id="checklist-heading"
            ref={headingRef}
            tabIndex={-1}
            className="text-xl font-semibold tracking-tight text-slate-900 focus:outline-none"
          >
            {TEXT.checklistHeading}
          </h2>
          <ul className="mt-2 flex flex-wrap gap-2 text-xs text-slate-600">
            {groups.map((g) => (
              <li key={g} className="rounded-full bg-slate-100 px-2.5 py-1">
                {GROUPS[g].heading}: {checklist.groups[g].length}
              </li>
            ))}
          </ul>
        </div>
        <button
          type="button"
          onClick={() => window.print()}
          className="min-h-11 rounded-lg px-3 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-sky-600 print:hidden"
        >
          Print checklist
        </button>
      </header>
      {checklist.coverage_note && (
        <p className="mx-4 mt-4 rounded-xl bg-sky-50 p-3 text-sm leading-relaxed text-sky-950 ring-1 ring-sky-100 sm:mx-6">
          {checklist.coverage_note}
        </p>
      )}
      <div className="grid gap-8 p-4 sm:p-6">
        {groups.map((g, i) => (
          <section key={g} aria-labelledby={`group-${g}`}>
            <h3
              id={`group-${g}`}
              className="flex items-baseline gap-2 text-lg font-semibold text-slate-900"
            >
              <span aria-hidden="true" className="text-sm font-semibold text-sky-700">
                {String.fromCharCode(65 + i)}
              </span>
              {GROUPS[g].heading}
            </h3>
            <p className="mt-0.5 text-sm text-slate-600">{GROUPS[g].intro}</p>
            <ul className="mt-3 grid gap-3">
              {checklist.groups[g].map((item) => (
                <li key={item.id}>
                  <ChecklistItemCard item={item} unconfirmed={g === "not_confirmed"} />
                </li>
              ))}
            </ul>
          </section>
        ))}
        <SourcesPanel checklist={checklist} />
        <aside
          aria-label={TEXT.aboutAnswers}
          className="rounded-xl bg-slate-50 p-4 text-sm text-slate-700 ring-1 ring-slate-200"
        >
          <p>{notice}</p>
        </aside>
      </div>
    </section>
  );
}

export function ChecklistItemCard({
  item,
  unconfirmed,
}: {
  item: AssistantItem;
  unconfirmed: boolean;
}) {
  const conditional = (item.caveats ?? []).filter((c) => c.code === "conditional");
  const otherCaveats = (item.caveats ?? []).filter((c) => c.code !== "conditional");
  return (
    <article
      aria-labelledby={`item-${item.id}`}
      className={`rounded-xl p-4 ring-1 break-inside-avoid ${
        unconfirmed ? "bg-slate-50 ring-slate-200" : "bg-white ring-slate-200"
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h4 id={`item-${item.id}`} className="font-semibold leading-snug text-slate-900">
          {item.title}
        </h4>
        <div className="flex flex-wrap gap-1.5 text-xs font-medium">
          {ITEM_KIND_LABELS[item.kind] && (
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-700">
              {ITEM_KIND_LABELS[item.kind]}
            </span>
          )}
          <span
            className={`rounded-full px-2 py-0.5 ${
              unconfirmed ? "bg-amber-100 text-amber-900" : "bg-sky-100 text-sky-900"
            }`}
          >
            {item.classification_label}
          </span>
        </div>
      </div>

      {(item.applies_if ?? []).length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {item.applies_if!.map((text) => (
            <li
              key={text}
              className="rounded-md bg-violet-50 px-2 py-0.5 text-xs font-medium text-violet-900 ring-1 ring-violet-100"
            >
              {text}
            </li>
          ))}
        </ul>
      )}
      {conditional.map((c, i) => (
        <p key={`${c.code}${i}`} className="mt-2 text-sm font-medium text-slate-800">
          {c.text}
        </p>
      ))}

      {item.source_wording && (
        <figure className="mt-3 border-l-4 border-sky-200 pl-3">
          <figcaption className="text-xs font-medium uppercase tracking-wider text-slate-500">
            {TEXT.formSays}
          </figcaption>
          <blockquote className="mt-0.5 text-sm text-slate-800">“{item.source_wording}”</blockquote>
        </figure>
      )}
      {item.approximate_reading && (
        <div className="mt-3 rounded-lg bg-amber-50 p-3 ring-1 ring-amber-200">
          <p className="text-xs font-medium uppercase tracking-wider text-amber-900">
            {TEXT.approximate}
          </p>
          <p className="mt-0.5 text-sm text-slate-800">{item.approximate_reading}</p>
          {item.uncertainty_warning && (
            <p className="mt-1.5 flex items-center gap-1.5 text-xs font-medium text-amber-900">
              <WarningIcon /> {item.uncertainty_warning}
            </p>
          )}
        </div>
      )}

      <div className="mt-3 grid gap-1 text-sm leading-relaxed">
        {item.establishes && <p className="text-slate-700">{item.establishes}</p>}
        <p className={unconfirmed ? "text-slate-800" : "text-slate-600"}>
          {item.does_not_establish}
        </p>
        {item.note && <p className="text-slate-600">{item.note}</p>}
        {item.fallback && <p className="font-medium text-slate-800">{item.fallback}</p>}
      </div>

      {otherCaveats.length > 0 && (
        <ul className="mt-3 grid gap-1 text-xs text-slate-600">
          {otherCaveats.map((c, i) => (
            <li key={`${c.code}${i}`} className="flex items-start gap-1.5">
              {c.code === "scanned_text" ? <WarningIcon /> : <span aria-hidden="true">•</span>}
              <span>{c.text}</span>
            </li>
          ))}
        </ul>
      )}

      {((item.citations ?? []).length > 0 || (item.related_citations ?? []).length > 0) && (
        <div className="mt-3 grid gap-1.5">
          {(item.citations ?? []).map((c, i, all) => (
            <CitationDisclosure
              key={`c${i}`}
              citation={c}
              label={all.length > 1 ? `${TEXT.sourceToggle} ${i + 1}` : TEXT.sourceToggle}
            />
          ))}
          {(item.related_citations ?? []).map((c, i, all) => (
            <CitationDisclosure
              key={`r${i}`}
              citation={c}
              label={all.length > 1 ? `${TEXT.relatedToggle} ${i + 1}` : TEXT.relatedToggle}
            />
          ))}
        </div>
      )}
    </article>
  );
}

/** One cited passage: document, page(s) and date always visible; the stored text on expand.
 * Scanned-form text is labelled as an automatic reading, never as a quotation. */
export function CitationDisclosure({
  citation,
  label,
}: {
  citation: SourceCitation;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const pages = TEXT.page(citation.page, citation.page_end);
  return (
    <div className="rounded-lg bg-slate-50 ring-1 ring-slate-200">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
        className="flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm focus-visible:outline-2 focus-visible:outline-sky-600"
      >
        <span>
          <span className="font-medium text-slate-800">{label}:</span>{" "}
          <span className="text-slate-700">
            {citation.document_title}, {pages}
          </span>
        </span>
        <span
          aria-hidden="true"
          className={`text-slate-500 transition-transform motion-reduce:transition-none ${open ? "rotate-90" : ""}`}
        >
          ›
        </span>
      </button>
      <div id={panelId} hidden={!open} className="border-t border-slate-200 px-3 py-3 text-sm">
        <dl className="grid gap-1 text-xs text-slate-600 sm:grid-cols-[auto_1fr] sm:gap-x-3">
          <dt className="font-medium">Document</dt>
          <dd>{citation.document_title}</dd>
          <dt className="font-medium">Page</dt>
          <dd>{pages}</dd>
          {citation.section && (
            <>
              <dt className="font-medium">Section</dt>
              <dd>{citation.section}</dd>
            </>
          )}
          <dt className="font-medium">Document date</dt>
          <dd>{citation.date_basis}</dd>
        </dl>
        {!citation.basis && <p className="mt-2 text-xs text-slate-600">{TEXT.sameWording}.</p>}
        {!citation.quotable && (
          <p className="mt-2 flex items-start gap-1.5 text-xs font-medium text-amber-900">
            <WarningIcon /> {TEXT.scannedPassage}
          </p>
        )}
        <p className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md bg-white p-3 text-[0.8rem] leading-relaxed text-slate-700 ring-1 ring-slate-200">
          {citation.passage}
        </p>
      </div>
    </div>
  );
}

function SourcesPanel({ checklist }: { checklist: AssistantChecklist }) {
  if (checklist.sources.length === 0) return null;
  return (
    <section aria-labelledby="sources-heading" className="rounded-xl p-4 ring-1 ring-slate-200">
      <h3 id="sources-heading" className="font-semibold text-slate-900">
        {TEXT.sourcesHeading}
      </h3>
      <ul className="mt-2 grid gap-2 text-sm">
        {checklist.sources.map((s) => (
          <li key={s.title}>
            <p className="font-medium text-slate-800">{s.title}</p>
            <p className="text-slate-600">{s.date_basis}</p>
            {s.scanned && (
              <p className="mt-0.5 flex items-center gap-1.5 text-xs font-medium text-amber-900">
                <WarningIcon /> {TEXT.scannedSource}
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
