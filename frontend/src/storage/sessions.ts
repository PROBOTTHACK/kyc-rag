/**
 * Session ids known to THIS browser tab (decision M5-D4, owner 2026-11-14).
 *
 * - `sessionStorage` only, never `localStorage`: ids survive reloads and navigation in the
 *   same tab and are gone when the tab or browser session closes.
 * - A session id is the only access credential (L6), so it is stored here and nowhere else:
 *   not in the URL, the page text, logs or error messages.
 * - Everything read back is validated: a malformed value is discarded (and the key cleared),
 *   entries that are not session ids are dropped, duplicates removed, and the list is capped.
 * - Storage that is unavailable or throws (private mode, disabled storage) degrades to
 *   memory-only behaviour; nothing ever throws to the UI.
 * - Forgetting only removes this key. Backend sessions and reports are never touched; they
 *   expire on the server's own schedule.
 */
export const STORAGE_KEY = "kyc-assistant.sessions.v1";
export const MAX_SESSIONS = 20;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export interface StoredSession {
  id: string;
  /** When this tab first saw the session (local metadata, not from the API). */
  addedAt: string;
}

export interface StoredState {
  sessions: StoredSession[];
  current: string | null;
}

export const EMPTY: StoredState = { sessions: [], current: null };

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null; // access itself can throw when storage is disabled
  }
}

function validSession(value: unknown): StoredSession | null {
  if (value === null || typeof value !== "object") return null;
  const { id, addedAt } = value as Record<string, unknown>;
  if (typeof id !== "string" || !UUID.test(id)) return null;
  if (typeof addedAt !== "string" || Number.isNaN(Date.parse(addedAt))) return null;
  return { id, addedAt };
}

/** Parse and validate a stored value; anything unusable becomes an empty state. */
export function parseStored(raw: string | null): StoredState {
  if (raw === null) return EMPTY;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return EMPTY;
  }
  if (value === null || typeof value !== "object") return EMPTY;
  const { sessions, current } = value as Record<string, unknown>;
  const seen = new Set<string>();
  const valid: StoredSession[] = [];
  for (const entry of Array.isArray(sessions) ? sessions : []) {
    const s = validSession(entry);
    if (s !== null && !seen.has(s.id)) {
      seen.add(s.id);
      valid.push(s);
    }
  }
  const kept = valid.slice(-MAX_SESSIONS);
  const cur = typeof current === "string" && kept.some((s) => s.id === current) ? current : null;
  return { sessions: kept, current: cur };
}

export function loadStored(): StoredState {
  const store = storage();
  if (store === null) return EMPTY;
  let raw: string | null;
  try {
    raw = store.getItem(STORAGE_KEY);
  } catch {
    return EMPTY;
  }
  const state = parseStored(raw);
  // Rewrite a value that needed cleaning, so a malformed entry never lingers.
  if (raw !== null && raw !== serialise(state)) saveStored(state);
  return state;
}

function serialise(state: StoredState): string {
  return JSON.stringify({ sessions: state.sessions, current: state.current });
}

export function saveStored(state: StoredState): void {
  const store = storage();
  if (store === null) return;
  try {
    if (state.sessions.length === 0 && state.current === null) store.removeItem(STORAGE_KEY);
    else store.setItem(STORAGE_KEY, serialise(state));
  } catch {
    // quota or disabled storage: keep working in memory
  }
}

/** Add (if new) and make current. Invalid ids are ignored. */
export function remember(state: StoredState, id: string, now: Date = new Date()): StoredState {
  if (!UUID.test(id)) return state;
  const sessions = state.sessions.some((s) => s.id === id)
    ? state.sessions
    : [...state.sessions, { id, addedAt: now.toISOString() }].slice(-MAX_SESSIONS);
  return { sessions, current: id };
}

export function setCurrent(state: StoredState, id: string | null): StoredState {
  if (id === null) return { ...state, current: null };
  return remember(state, id);
}

/** Forget every locally stored id (this tab only; the server is not contacted). */
export function forgetAll(): StoredState {
  saveStored(EMPTY);
  return EMPTY;
}
