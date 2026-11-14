import { useId, useState, type ReactNode, type RefObject } from "react";

import type { AssistantChecklist, AssistantItem, SourceCitation } from "../api/types";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { DISCLAIMER_LINES, GROUPS, ITEM_KIND_LABELS, PRODUCT_NAME, TEXT } from "./labels";
import { Button, Icon, type IconName } from "./ui";

type GroupKey = "documents" | "information" | "not_confirmed";

const GROUP_LETTER: Record<GroupKey, string> = {
  documents: "A",
  information: "B",
  not_confirmed: "C",
};

const KIND_ICON: Record<string, IconName> = {
  document: "document",
  declaration: "pen",
  information: "info",
};

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
  onBack,
}: {
  checklist: AssistantChecklist;
  notice: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
  onBack?: () => void;
}) {
  useFocusWhenLost(headingRef); // a choice card that produced the checklist has gone
  const groups = (["documents", "information", "not_confirmed"] as const).filter(
    (g) => checklist.groups[g].length > 0,
  );
  const allIds = groups.flatMap((g) => checklist.groups[g].map((i) => i.id));
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const allOpen = allIds.length > 0 && allIds.every((id) => open.has(id));
  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <section
      aria-labelledby="checklist-heading"
      className="oa-rise mx-auto max-w-3xl px-4 py-6 sm:px-6 sm:py-10"
      data-print="expand"
    >
      {/* Print-only heading: product and disclaimer on paper. */}
      <div className="mb-8 hidden" data-print="only">
        <p className="text-sm font-semibold">{PRODUCT_NAME}</p>
        <p className="mt-1 text-xs">
          {DISCLAIMER_LINES.headline} {DISCLAIMER_LINES.detail}
        </p>
      </div>

      <header className="border-b border-line pb-6">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="-ml-2 mb-4 inline-flex min-h-9 items-center gap-1.5 rounded-md px-2 text-sm text-muted hover:text-fg"
            data-print="hide"
          >
            <Icon name="chevron" className="h-4 w-4 rotate-180" />
            {TEXT.backToConversation}
          </button>
        )}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2
              id="checklist-heading"
              ref={headingRef}
              tabIndex={-1}
              className="font-display text-2xl font-semibold tracking-tight text-fg focus:outline-none"
            >
              {TEXT.checklistHeading}
            </h2>
            <p className="mt-2 text-sm tabular-nums text-muted">
              {(["documents", "information", "not_confirmed"] as const)
                .filter((g) => checklist.groups[g].length > 0)
                .map((g) => `${GROUPS[g].heading}: ${checklist.groups[g].length}`)
                .join(" · ")}
            </p>
          </div>
          <div className="-mr-2 flex gap-1" data-print="hide">
            <Button variant="ghost" onClick={() => setOpen(allOpen ? new Set() : new Set(allIds))}>
              <Icon name={allOpen ? "collapse" : "expand"} />
              {allOpen ? TEXT.collapseAll : TEXT.expandAll}
            </Button>
            <Button variant="ghost" onClick={() => window.print()}>
              <Icon name="printer" />
              {TEXT.print}
            </Button>
          </div>
        </div>
      </header>

      {checklist.coverage_note && (
        <p className="mt-6 border-l-2 border-accent/70 pl-4 text-sm leading-relaxed text-muted">
          {checklist.coverage_note}
        </p>
      )}

      <div className="mt-10 grid grid-cols-[minmax(0,1fr)] gap-12">
        {groups.map((g) => (
          <section key={g} aria-labelledby={`group-${g}`}>
            <div className="flex items-baseline gap-3">
              <span aria-hidden="true" className="w-4 shrink-0 text-sm text-subtle">
                {GROUP_LETTER[g]}
              </span>
              <div className="min-w-0">
                <h3 id={`group-${g}`} className="text-lg font-semibold tracking-tight text-fg">
                  {GROUPS[g].heading}
                </h3>
                <p className="mt-0.5 text-sm text-muted">{GROUPS[g].intro}</p>
              </div>
            </div>
            <ul className="mt-4 grid grid-cols-[minmax(0,1fr)] divide-y divide-line border-y border-line">
              {checklist.groups[g].map((item) => (
                <li key={item.id}>
                  <ChecklistItemCard
                    item={item}
                    unconfirmed={g === "not_confirmed"}
                    open={open.has(item.id)}
                    onToggle={() => toggle(item.id)}
                  />
                </li>
              ))}
            </ul>
          </section>
        ))}
        <SourcesPanel checklist={checklist} />
        <aside
          aria-label={TEXT.aboutAnswers}
          className="border-t border-line pt-6 text-sm leading-relaxed text-muted"
        >
          <p className="text-xs font-medium text-subtle">{TEXT.aboutAnswers}</p>
          <p className="mt-1.5">{notice}</p>
        </aside>
      </div>
    </section>
  );
}

function Chip({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "warn";
}) {
  const tones = { neutral: "text-muted", accent: "text-accent", warn: "text-warn" };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-sm border border-line px-1.5 py-px text-[0.72rem] ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function ChecklistItemCard({
  item,
  unconfirmed,
  open = false,
  onToggle = () => {},
}: {
  item: AssistantItem;
  unconfirmed: boolean;
  open?: boolean;
  onToggle?: () => void;
}) {
  const detailsId = useId();
  const conditional = (item.caveats ?? []).filter((c) => c.code === "conditional");
  const otherCaveats = (item.caveats ?? []).filter((c) => c.code !== "conditional");
  const firstCitation = item.citations?.[0];
  const scanned =
    Boolean(item.uncertainty_warning) || (item.citations ?? []).some((c) => !c.quotable);
  return (
    <article aria-labelledby={`item-${item.id}`}>
      <div className="flex gap-3 py-4">
        <span aria-hidden="true" className={`mt-1 ${unconfirmed ? "text-warn" : "text-subtle"}`}>
          <Icon
            name={unconfirmed ? "question" : (KIND_ICON[item.kind] ?? "document")}
            className="h-4 w-4"
          />
        </span>
        <div className="min-w-0 flex-1">
          <h4 id={`item-${item.id}`} className="font-medium leading-snug text-fg">
            {item.title}
          </h4>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <Chip tone={unconfirmed ? "warn" : "accent"}>{item.classification_label}</Chip>
            {ITEM_KIND_LABELS[item.kind] && <Chip>{ITEM_KIND_LABELS[item.kind]}</Chip>}
            {(item.applies_if ?? []).map((text) => (
              <Chip key={text}>{text}</Chip>
            ))}
            {scanned && (
              <Chip tone="warn">
                <Icon name="scan" className="h-3 w-3" /> Scanned form
              </Chip>
            )}
          </div>
          {conditional.map((c, i) => (
            <p key={`${c.code}${i}`} className="mt-2 text-sm text-fg/90">
              {c.text}
            </p>
          ))}
          {unconfirmed && (
            <p className="mt-2 text-sm leading-relaxed text-muted">{item.does_not_establish}</p>
          )}
          {firstCitation && (
            <p className="mt-2 truncate text-xs text-subtle">
              {firstCitation.document_title} ·{" "}
              {TEXT.page(firstCitation.page, firstCitation.page_end)}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={detailsId}
          aria-label={`${open ? TEXT.hideDetails : TEXT.details}: ${item.title}`}
          className="-mr-2 grid h-10 w-10 shrink-0 place-items-center self-start rounded-md text-subtle hover:bg-raised hover:text-fg"
          data-print="hide"
        >
          <Icon
            name="chevron"
            className={`h-4 w-4 transition-transform duration-200 ${open ? "rotate-90" : ""}`}
          />
        </button>
      </div>

      <div id={detailsId} hidden={!open} className="pb-5 pl-7">
        {item.source_wording && (
          <figure className="border-l-2 border-line-strong py-1 pl-3">
            <figcaption className="text-xs text-subtle">{TEXT.formSays}</figcaption>
            <blockquote className="mt-1 text-sm text-fg">“{item.source_wording}”</blockquote>
          </figure>
        )}
        {item.approximate_reading && (
          <div className="border-l-2 border-warn/70 py-1 pl-3">
            <p className="text-xs font-medium text-warn">{TEXT.approximate}</p>
            <p className="mt-1 text-sm text-fg/90">{item.approximate_reading}</p>
            {item.uncertainty_warning && (
              <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-warn">
                <Icon name="alert" className="h-3.5 w-3.5" /> {item.uncertainty_warning}
              </p>
            )}
          </div>
        )}

        <div className="mt-3 grid gap-1.5 text-sm leading-relaxed">
          <p className="text-xs text-subtle">{TEXT.whyHere}</p>
          {item.establishes && <p className="text-fg/90">{item.establishes}</p>}
          {!unconfirmed && <p className="text-muted">{item.does_not_establish}</p>}
          {item.note && <p className="text-muted">{item.note}</p>}
          {item.fallback && <p className="font-medium text-fg">{item.fallback}</p>}
        </div>

        {otherCaveats.length > 0 && (
          <ul className="mt-3 grid gap-1 text-xs text-subtle">
            {otherCaveats.map((c, i) => (
              <li key={`${c.code}${i}`} className="flex items-start gap-1.5">
                <Icon
                  name={c.code === "scanned_text" ? "alert" : "calendar"}
                  className={`mt-px h-3.5 w-3.5 ${c.code === "scanned_text" ? "text-warn" : ""}`}
                />
                <span>{c.text}</span>
              </li>
            ))}
          </ul>
        )}

        {((item.citations ?? []).length > 0 || (item.related_citations ?? []).length > 0) && (
          <div className="mt-4 grid border-y border-line">
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
      </div>
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
    <div className="border-t border-line first:border-t-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
        className="flex min-h-11 w-full items-center justify-between gap-3 rounded-md py-2 text-left text-sm hover:text-fg"
      >
        <span className="flex min-w-0 items-start gap-2">
          <Icon name="book" className="mt-0.5 h-3.5 w-3.5 text-subtle" />
          <span className="min-w-0">
            <span className="font-medium text-fg">{label}:</span>{" "}
            <span className="text-muted">
              {citation.document_title}, {pages}
            </span>
          </span>
        </span>
        <Icon
          name="chevron"
          className={`h-4 w-4 text-subtle transition-transform duration-200 ${open ? "rotate-90" : ""}`}
        />
      </button>
      <div id={panelId} hidden={!open} className="pb-3 pt-1 text-sm">
        <dl className="grid gap-x-4 gap-y-1 text-xs text-subtle sm:grid-cols-[auto_1fr]">
          <dt>Document</dt>
          <dd className="text-muted">{citation.document_title}</dd>
          <dt>Page</dt>
          <dd className="text-muted">{pages}</dd>
          {citation.section && (
            <>
              <dt>Section</dt>
              <dd className="text-muted">{citation.section}</dd>
            </>
          )}
          <dt>Document date</dt>
          <dd className="text-muted">{citation.date_basis}</dd>
          <dt>Text</dt>
          <dd className="text-muted">
            {citation.quotable ? "Text from the document" : "Automatic reading of a scanned page"}
          </dd>
        </dl>
        {!citation.basis && <p className="mt-2 text-xs text-subtle">{TEXT.sameWording}.</p>}
        {!citation.quotable && (
          <p className="mt-2 flex items-start gap-1.5 text-xs font-medium text-warn">
            <Icon name="alert" className="mt-px h-3.5 w-3.5" /> {TEXT.scannedPassage}
          </p>
        )}
        <p className="oa-scroll mt-3 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md bg-surface p-3 text-[0.8rem] leading-relaxed text-muted">
          {citation.passage}
        </p>
      </div>
    </div>
  );
}

function SourcesPanel({ checklist }: { checklist: AssistantChecklist }) {
  if (checklist.sources.length === 0) return null;
  return (
    <section aria-labelledby="sources-heading">
      <h3 id="sources-heading" className="text-lg font-semibold tracking-tight text-fg">
        {TEXT.sourcesHeading}
      </h3>
      <ul className="mt-4 divide-y divide-line border-y border-line">
        {checklist.sources.map((s) => (
          <li key={s.title} className="py-3">
            <p className="text-sm font-medium text-fg">{s.title}</p>
            <p className="mt-0.5 text-xs text-subtle">{s.date_basis}</p>
            {s.scanned && (
              <p className="mt-1.5 flex items-center gap-1.5 text-xs font-medium text-warn">
                <Icon name="scan" className="h-3.5 w-3.5" /> {TEXT.scannedSource}
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
