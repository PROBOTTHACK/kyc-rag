import type { components } from "../../api/schema";
import { ELIGIBILITY, pages, RELIABILITY, SOURCE_STATUS } from "./labels";

type Citation = components["schemas"]["Citation"];

/**
 * Citations exactly as stored: document key and page range, with the source's own metadata.
 * The page range is copied from the stored chunk record; it is not re-checked here
 * (known issue K5, see `CitationNote`).
 */
export function Citations({ citations }: { citations: readonly Citation[] }) {
  return (
    <ul aria-label="Citations" className="mt-1 space-y-1 text-sm">
      {citations.map((c) => (
        <li key={c.evidence_id} className="rounded bg-slate-50 px-2 py-1">
          <span className="font-mono">{c.document_key}</span>, {pages(c.page, c.page_end)}
          {c.paragraph != null && <>, para {c.paragraph}</>}
          <span className="text-slate-600">
            {" "}
            · {ELIGIBILITY[c.evidence_eligibility]} · {RELIABILITY[c.text_reliability]} ·{" "}
            {SOURCE_STATUS[c.source_status]}
            {c.match_scope === "entity_group" && " · covers a broader customer group"}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** The K5 caveat, shown once wherever citations are listed. */
export function CitationNote() {
  return (
    <p className="mt-2 text-xs text-slate-600">
      Page references are copied from the stored source records and have not been independently
      re-checked. A citation of part of a paragraph can show the page range of the whole paragraph
      (known issue K5). Check the source document before relying on a page number.
    </p>
  );
}
