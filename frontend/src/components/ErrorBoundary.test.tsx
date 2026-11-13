import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { DISCLAIMER } from "./Disclaimer";
import { ErrorBoundary } from "./ErrorBoundary";
import { Layout } from "./Layout";

const SECRET = "SECRET-DETAIL-FROM-AN-API-RESPONSE";

function Boom(): never {
  throw new Error(SECRET);
}

function quietConsole() {
  // React reports caught render errors on console.error; keep the test output readable.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
}

describe("error boundaries", () => {
  it("a failing page keeps the shell and the disclaimer, and hides the error details", () => {
    quietConsole();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Boom />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert").textContent).toContain("could not be displayed");
    expect(screen.getByText(DISCLAIMER)).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy();
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("navigating away clears the error", async () => {
    quietConsole();
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Boom />} />
            <Route path="assistant" element={<h1>Assistant ok</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("link", { name: "KYC Assistant" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("heading", { name: "Assistant ok" })).toBeTruthy();
  });

  it("the top-level boundary shows the disclaimer itself", () => {
    quietConsole();
    render(
      <ErrorBoundary withDisclaimer>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText(DISCLAIMER)).toBeTruthy();
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("try again re-renders the children", async () => {
    quietConsole();
    const user = userEvent.setup();
    let fail = true;
    function Flaky() {
      if (fail) throw new Error(SECRET);
      return <p>Recovered</p>;
    }
    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    );
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Recovered")).toBeTruthy();
  });
});
