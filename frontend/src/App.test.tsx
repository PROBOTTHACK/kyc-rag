import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { DISCLAIMER } from "./components/Disclaimer";

function renderAt(path: string) {
  // The assistant (S4) reads server state, so the shell needs the React Query provider.
  // No request is made: no screen fetches anything until the user acts.
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("application shell", () => {
  it.each([
    ["/", "Dashboard"],
    ["/assistant", "KYC Assistant"],
    ["/reports/5d0c1f2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f", "Compliance Report"],
    ["/no/such/page", "Page not found"],
  ])("shows the disclaimer and the right page at %s", (path, heading) => {
    renderAt(path);
    const disclaimer = screen.getByRole("complementary", { name: "Disclaimer" });
    expect(within(disclaimer).getByText(DISCLAIMER)).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: heading })).toBeTruthy();
  });

  it("navigates between pages and keeps the disclaimer", async () => {
    const user = userEvent.setup();
    renderAt("/");
    await user.click(screen.getByRole("link", { name: "KYC Assistant" }));
    expect(screen.getByRole("heading", { level: 1, name: "KYC Assistant" })).toBeTruthy();
    expect(screen.getByText(DISCLAIMER)).toBeTruthy();
    await user.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeTruthy();
  });

  it("shows only the screens in the M5 scope", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(labels).toEqual(["Dashboard", "KYC Assistant"]);
  });

  it("never presents an approval or rejection", () => {
    for (const path of ["/", "/assistant", "/reports/x", "/nope"]) {
      const { container, unmount } = renderAt(path);
      expect(container.textContent ?? "").not.toMatch(/\b(approved|rejected|compliant)\b/i);
      unmount();
    }
  });
});
