import { useId, useState, type ReactNode, type RefObject } from "react";

import type { AssistantChecklist, AssistantItem, SourceCitation } from "../api/types";
import { useFocusWhenLost } from "../components/useFocusWhenLost";
import { DISCLAIMER_LINES, GROUPS, ITEM_KIND_LABELS, PRODUCT_NAME, TEXT } from "./labels";
import { Button, Icon, type IconName } from "./ui";

type GroupKey = "documents" | "information" | "not_confirmed";

const GROUP_STYLE: Record<GroupKey, { letter: string; tone: string; badge: string }> = {
  documents: { letter: "A", tone: "text-cyan", badge: "bg-cyan/10 text-cyan ring-cyan/30" },
  information: {
    letter: "B",
    tone: "text-violet",
    badge: "bg-violet/10 text-violet ring-violet/30",
  },
  not_confirmed: { letter: "C", tone: "text-warn", badge: "bg-warn/10 text-warn ring-warn/30" },
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
      className="oa-rise overflow-hidden rounded-2xl bg-surface/70 ring-1 ring-line/70"
      data-print="expand"
    >
      {/* Print-only heading: product and disclaimer on paper. */}
      <div className="hidden" data-print="only">
        <p className="text-sm font-semibold">{PRODUCT_NAME}</p>
        <p className="mt-1 text-xs">
          {DISCLAIMER_LINES.headline} {DISCLAIMER_LINES.detail}
        </p>
      </div>

      <header className="border-b border-line/70 p-5 sm:p-6">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="-ml-2 mb-3 inline-flex min-h-9 items-center gap-1.5 rounded-lg px-2 text-sm text-muted hover:text-fg"
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
              className="font-display text-xl font-semibold tracking-tight text-fg focus:outline-none"
            >
              {TEXT.checklistHeading}
            </h2>
            <p className="mt-1 text-sm text-muted">
              {(["documents", "information", "not_confirmed"] as const)
                .filter((g) => checklist.groups[g].length > 0)
                .map((g) => `${GROUPS[g].heading}: ${checklist.groups[g].length}`)
                .join(" · ")}
            </p>
          </div>
          <div className="flex gap-1" data-print="hide">
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
        <p className="mx-5 mt-5 flex gap-2.5 rounded-xl border border-cyan/20 bg-cyan/[0.06] p-3.5 text-sm leading-relaxed text-muted sm:mx-6">
          <Icon name="info" className="mt-0.5 h-4 w-4 text-cyan" />
          <span>{checklist.coverage_note}</span>
        </p>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)] gap-9 p-5 sm:p-6">
        {groups.map((g) => (
          <section key={g} aria-labelledby={`group-${g}`}>
            <div className="flex items-start gap-3">
              <span
                aria-hidden="true"
                className={`grid h-7 w-7 shrink-0 place-items-center rounded-lg font-code text-sm font-medium ring-1 ${GROUP_STYLE[g].badge}`}
              >
                {GROUP_STYLE[g].letter}
              </span>
              <div className="min-w-0">
                <h3
                  id={`group-${g}`}
                  className="font-display text-base font-semibold leading-7 text-fg"
                >
                  {GROUPS[g].heading}
                </h3>
                <p className="text-sm text-muted">{GROUPS[g].intro}</p>
              </div>
            </div>
            <ul className="mt-4 grid grid-cols-[minmax(0,1fr)] gap-2.5">
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
          className="rounded-xl border border-line bg-raised/40 p-4 text-sm leading-relaxed text-muted"
        >
          <p className="font-code text-[0.65rem] uppercase tracking-[0.18em] text-subtle">
            {TEXT.aboutAnswers}
          </p>
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
  tone?: "neutral" | "cyan" | "warn" | "violet";
}) {
  const tones = {
    neutral: "bg-raised text-muted ring-line",
    cyan: "bg-cyan/10 text-cyan ring-cyan/25",
    warn: "bg-warn/10 text-warn ring-warn/25",
    violet: "bg-violet/10 text-violet ring-violet/25",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[0.72rem] font-medium ring-1 ${tones[tone]}`}
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
    <article
      aria-labelledby={`item-${item.id}`}
      className={`rounded-xl border transition-colors duration-150 ${
        open
          ? "border-line-strong bg-raised/60"
          : "border-line bg-raised/30 hover:border-line-strong"
      }`}
    >
      <div className="flex gap-3 p-3.5 sm:p-4">
        <span
          aria-hidden="true"
          className={`mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg ring-1 ${
            unconfirmed ? "bg-warn/10 text-warn ring-warn/25" : "bg-surface text-muted ring-line"
          }`}
        >
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
            <Chip tone={unconfirmed ? "warn" : "cyan"}>{item.classification_label}</Chip>
            {ITEM_KIND_LABELS[item.kind] && <Chip>{ITEM_KIND_LABELS[item.kind]}</Chip>}
            {(item.applies_if ?? []).map((text) => (
              <Chip key={text} tone="violet">
                {text}
              </Chip>
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
            <p className="mt-2 truncate font-code text-[0.7rem] text-subtle">
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
          className="grid h-10 w-10 shrink-0 place-items-center self-start rounded-lg text-muted hover:bg-hover hover:text-fg"
          data-print="hide"
        >
          <Icon
            name="chevron"
            className={`h-4 w-4 transition-transform duration-200 ${open ? "rotate-90" : ""}`}
          />
        </button>
      </div>

      <div
        id={detailsId}
        hidden={!open}
        className="border-t border-line px-3.5 pb-4 pt-3.5 sm:px-4 sm:pl-[3.75rem]"
      >
        {item.source_wording && (
          <figure className="rounded-lg border-l-2 border-cyan/60 bg-void/40 py-2 pl-3 pr-2">
            <figcaption className="font-code text-[0.65rem] uppercase tracking-[0.16em] text-subtle">
              {TEXT.formSays}
            </figcaption>
            <blockquote className="mt-1 text-sm text-fg">“{item.source_wording}”</blockquote>
          </figure>
        )}
        {item.approximate_reading && (
          <div className="rounded-lg border border-warn/25 bg-warn/[0.06] p-3">
            <p className="font-code text-[0.65rem] uppercase tracking-[0.16em] text-warn">
              {TEXT.approximate}
            </p>
            <p className="mt-1 text-sm text-fg/90">{item.approximate_reading}</p>
            {item.uncertainty_warning && (
              <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-warn">
                <Icon name="alert" className="h-3.5 w-3.5" /> {item.uncertainty_warning}
              </p>
            )}
          </div>
        )}

        <div className="mt-3 grid gap-1.5 text-sm leading-relaxed">
          <p className="font-code text-[0.65rem] uppercase tracking-[0.16em] text-subtle">
            {TEXT.whyHere}
          </p>
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
    <div className="overflow-hidden rounded-lg border border-line bg-surface/70">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
        className="flex min-h-11 w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:bg-raised/60"
      >
        <span className="flex min-w-0 items-start gap-2">
          <Icon name="book" className="mt-0.5 h-3.5 w-3.5 text-cyan" />
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
      <div id={panelId} hidden={!open} className="border-t border-line px-3 py-3 text-sm">
        <dl className="grid gap-x-4 gap-y-1 font-code text-[0.7rem] text-subtle sm:grid-cols-[auto_1fr]">
          <dt className="uppercase tracking-[0.12em]">Document</dt>
          <dd className="font-ui text-muted">{citation.document_title}</dd>
          <dt className="uppercase tracking-[0.12em]">Page</dt>
          <dd className="font-ui text-muted">{pages}</dd>
          {citation.section && (
            <>
              <dt className="uppercase tracking-[0.12em]">Section</dt>
              <dd className="font-ui text-muted">{citation.section}</dd>
            </>
          )}
          <dt className="uppercase tracking-[0.12em]">Document date</dt>
          <dd className="font-ui text-muted">{citation.date_basis}</dd>
          <dt className="uppercase tracking-[0.12em]">Text</dt>
          <dd className="font-ui text-muted">
            {citation.quotable ? "Text from the document" : "Automatic reading of a scanned page"}
          </dd>
        </dl>
        {!citation.basis && <p className="mt-2 text-xs text-subtle">{TEXT.sameWording}.</p>}
        {!citation.quotable && (
          <p className="mt-2 flex items-start gap-1.5 text-xs font-medium text-warn">
            <Icon name="alert" className="mt-px h-3.5 w-3.5" /> {TEXT.scannedPassage}
          </p>
        )}
        <p className="oa-scroll mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-void/50 p-3 text-[0.8rem] leading-relaxed text-muted">
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
      <h3
        id="sources-heading"
        className="flex items-center gap-2 font-display text-lg font-semibold text-fg"
      >
        <Icon name="book" className="h-4 w-4 text-cyan" />
        {TEXT.sourcesHeading}
      </h3>
      <ul className="mt-3 grid gap-2">
        {checklist.sources.map((s) => (
          <li key={s.title} className="rounded-xl border border-line bg-raised/30 p-3.5">
            <p className="font-medium text-fg">{s.title}</p>
            <p className="mt-0.5 font-code text-[0.72rem] text-subtle">{s.date_basis}</p>
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
