import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ASSISTANT_STORAGE_KEY,
  forgetAssistantSession,
  loadAssistantSession,
  saveAssistantSession,
} from "./assistantSession";

const ID = "0a5e5510-0000-4000-8000-000000000001";

afterEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("assistant session storage", () => {
  it("keeps one valid id in sessionStorage only", () => {
    saveAssistantSession(ID);
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBe(ID);
    expect(window.localStorage.length).toBe(0);
    expect(loadAssistantSession()).toBe(ID);
    forgetAssistantSession();
    expect(loadAssistantSession()).toBeNull();
  });

  it("ignores and clears anything that is not a session id", () => {
    saveAssistantSession("not-an-id");
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBeNull();
    window.sessionStorage.setItem(ASSISTANT_STORAGE_KEY, "<script>");
    expect(loadAssistantSession()).toBeNull();
    expect(window.sessionStorage.getItem(ASSISTANT_STORAGE_KEY)).toBeNull();
  });

  it("degrades to memory when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("disabled");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("disabled");
    });
    expect(() => saveAssistantSession(ID)).not.toThrow();
    expect(loadAssistantSession()).toBeNull();
    expect(() => forgetAssistantSession()).not.toThrow();
  });
});
