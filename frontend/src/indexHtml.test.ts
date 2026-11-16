import { describe, expect, it } from "vitest";

import html from "../index.html?raw";

// A7.6: without a declared icon every browser requests /favicon.ico, gets a 404 and logs a
// console error (the browser matrix requires zero). The icon is inline: no request at all.
describe("index.html", () => {
  it("declares an inline icon, so the browser never requests /favicon.ico", () => {
    const icon = /<link\s+rel="icon"\s+href="([^"]+)"/.exec(html);
    expect(icon).not.toBeNull();
    expect(icon![1]!.startsWith("data:image/svg+xml,")).toBe(true);
  });
});
