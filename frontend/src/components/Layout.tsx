import { NavLink, Outlet, useLocation } from "react-router";

import { Disclaimer } from "./Disclaimer";
import { ErrorBoundary } from "./ErrorBoundary";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/assistant", label: "KYC Assistant", end: false },
] as const;

/** The application shell: the disclaimer and navigation stay outside the page error
 * boundary, so they remain visible when a page fails. */
export function Layout() {
  const location = useLocation();
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="sticky top-0 z-10 bg-white shadow-sm">
        <Disclaimer />
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <span className="font-semibold">KYC Compliance Engine (prototype)</span>
          <nav aria-label="Main">
            <ul className="flex gap-4 text-sm">
              {NAV.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      isActive ? "font-semibold underline" : "hover:underline"
                    }
                  >
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <ErrorBoundary resetKey={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
