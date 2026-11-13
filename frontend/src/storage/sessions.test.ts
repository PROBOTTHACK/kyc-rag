import { describe, expect, it, vi } from "vitest";

import {
  EMPTY,
  forgetAll,
  loadStored,
  MAX_SESSIONS,
  parseStored,
  remember,
  saveStored,
  setCurrent,
  STORAGE_KEY,
} from "./sessions";

const A = "5d0c1f2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f";
const B = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d";
const AT = "2026-11-14T09:00:00.000Z";

function id(n: number) {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

describe("session id storage (M5-D4)", () => {
  it("persists in sessionStorage and reads back across a 'reload'", () => {
    saveStored(remember(EMPTY, A, new Date(AT)));
    // a reload: nothing but sessionStorage survives
    expect(loadStored()).toEqual({ sessions: [{ id: A, addedAt: AT }], current: A });
  });

  it("never uses localStorage", () => {
    const local = vi.spyOn(Storage.prototype, "setItem");
    saveStored(remember(EMPTY, A));
    for (const call of local.mock.calls) expect(call[0]).toBe(STORAGE_KEY);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.getItem(STORAGE_KEY)).not.toBeNull();
  });

  it("discards a malformed value and clears it", () => {
    for (const raw of ["{not json", "null", "42", '"text"', "[]", '{"sessions": "x"}']) {
      window.sessionStorage.setItem(STORAGE_KEY, raw);
      expect(loadStored()).toEqual(EMPTY);
      expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    }
  });

  it("keeps only valid entries: real session ids, valid dates, no duplicates, a known current", () => {
    const raw = JSON.stringify({
      sessions: [
        { id: A, addedAt: AT },
        { id: "not-a-uuid", addedAt: AT },
        { id: "<script>alert(1)</script>", addedAt: AT },
        { id: B, addedAt: "yesterday-ish" },
        { id: A, addedAt: AT },
        null,
        "x",
        { id: B.toUpperCase(), addedAt: AT },
        { id: B, addedAt: AT, extra: "ignored" },
      ],
      current: "00000000-0000-4000-8000-00000000ffff",
    });
    expect(parseStored(raw)).toEqual({
      sessions: [
        { id: A, addedAt: AT },
        { id: B, addedAt: AT },
      ],
      current: null, // not in the list: never followed
    });
  });

  it("is bounded to the most recent sessions", () => {
    let state = EMPTY;
    for (let n = 1; n <= MAX_SESSIONS + 5; n++) state = remember(state, id(n));
    expect(state.sessions).toHaveLength(MAX_SESSIONS);
    expect(state.sessions[0]!.id).toBe(id(6));
    expect(state.current).toBe(id(MAX_SESSIONS + 5));
  });

  it("remembers each session once, switches current, and ignores invalid ids", () => {
    let state = remember(EMPTY, A, new Date(AT));
    state = remember(state, B);
    state = setCurrent(state, A);
    expect(state.sessions.map((s) => s.id)).toEqual([A, B]);
    expect(state.sessions[0]!.addedAt).toBe(AT);
    expect(state.current).toBe(A);
    expect(remember(state, "nope")).toBe(state);
    expect(setCurrent(state, null)).toEqual({ ...state, current: null });
  });

  it("forgetting removes only the stored key", () => {
    window.sessionStorage.setItem("unrelated", "kept");
    saveStored(remember(EMPTY, A));
    expect(forgetAll()).toEqual(EMPTY);
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(window.sessionStorage.getItem("unrelated")).toBe("kept");
  });

  it("never throws when storage is unavailable", () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(loadStored()).toEqual(EMPTY);
    expect(() => saveStored(remember(EMPTY, A))).not.toThrow();
    get.mockRestore();
    set.mockRestore();
  });
});
