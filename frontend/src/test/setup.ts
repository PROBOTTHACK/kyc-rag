import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest globals are off, so Testing Library's automatic cleanup is registered here.
// Browser storage persists within a test file's window (M5-D4 stores session ids in
// sessionStorage), so each test starts with an empty tab.
afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  window.localStorage.clear();
});
