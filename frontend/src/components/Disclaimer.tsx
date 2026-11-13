/** The independence disclaimer (docs/00, docs/09, M5_PLAN §D). Rendered by the layout on
 * every page and by the top-level error fallback, so it stays visible even after a crash. */
export const DISCLAIMER =
  "Independent prototype. Not an official bank system. Not a compliance approval.";
export const DISCLAIMER_DETAIL =
  "Not affiliated with or endorsed by HDFC Bank or the RBI. Synthetic data only. Every result requires human review.";

export function Disclaimer() {
  return (
    <aside
      aria-label="Disclaimer"
      className="border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-950"
    >
      <p>
        <strong className="font-semibold">{DISCLAIMER}</strong> {DISCLAIMER_DETAIL}
      </p>
    </aside>
  );
}
