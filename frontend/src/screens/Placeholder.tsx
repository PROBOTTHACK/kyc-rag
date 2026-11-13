import type { ReactNode } from "react";

/** A screen that is not built yet. It says so plainly and shows no data (M5 S2). */
export function Placeholder({
  title,
  stage,
  children,
}: {
  title: string;
  stage: string;
  children?: ReactNode;
}) {
  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        {title}
      </h1>
      <p className="mt-2 text-slate-600">Not built yet (planned for M5 {stage}).</p>
      {children}
    </section>
  );
}
