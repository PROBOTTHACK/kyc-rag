import { Link } from "react-router";

export function NotFoundPage() {
  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        Page not found
      </h1>
      <p className="mt-2 text-slate-600">
        This address does not match any page.{" "}
        <Link to="/" className="underline">
          Go to the dashboard
        </Link>
        .
      </p>
    </section>
  );
}
