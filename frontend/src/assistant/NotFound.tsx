import { Link } from "react-router";

import { PRODUCT_NAME } from "./labels";
import { Icon, Mark } from "./ui";

/** An unknown address, in the assistant's own shell style. */
export function NotFound() {
  return (
    <main className="oa grid min-h-dvh place-items-center px-4">
      <section aria-labelledby="not-found" className="oa-rise w-full max-w-md text-center">
        <Mark className="mx-auto h-12 w-12" />
        <p className="mt-6 text-sm tabular-nums text-subtle">404</p>
        <h1 id="not-found" className="mt-2 font-display text-2xl font-semibold text-fg">
          Page not found
        </h1>
        <p className="mt-2 text-sm text-muted">This address does not match any page.</p>
        <Link
          to="/"
          className="mt-6 inline-flex min-h-11 items-center gap-2 rounded-md border border-line-strong px-4 text-sm font-medium text-fg hover:bg-hover"
        >
          Go to the {PRODUCT_NAME}
          <Icon name="arrow" />
        </Link>
      </section>
    </main>
  );
}
