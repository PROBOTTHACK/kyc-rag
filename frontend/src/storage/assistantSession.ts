/**
 * The account opening assistant's current conversation id (A6), following M5-D4:
 *
 * - `sessionStorage` only, never `localStorage`: a reload or a return to `/open-account` in
 *   the same tab recovers the conversation; closing the tab forgets it.
 * - The id is the only handle to a conversation, so it is kept here and nowhere else: not in
 *   the URL, the page text, logs or error messages.
 * - What is read back is validated (a UUID); anything else is discarded and the key cleared.
 * - Storage that is unavailable or throws degrades to memory-only behaviour; nothing throws
 *   to the UI.
 * - Forgetting only removes this key. The server keeps its records (A5 retention).
 */
export const ASSISTANT_STORAGE_KEY = "open-account.session.v1";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function isSessionId(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

export function loadAssistantSession(): string | null {
  const store = storage();
  if (store === null) return null;
  try {
    const raw = store.getItem(ASSISTANT_STORAGE_KEY);
    if (raw === null) return null;
    if (isSessionId(raw)) return raw;
    store.removeItem(ASSISTANT_STORAGE_KEY); // never let a malformed value linger
  } catch {
    // disabled storage: memory only
  }
  return null;
}

export function saveAssistantSession(id: string): void {
  if (!isSessionId(id)) return;
  try {
    storage()?.setItem(ASSISTANT_STORAGE_KEY, id);
  } catch {
    // quota or disabled storage: keep working in memory
  }
}

export function forgetAssistantSession(): void {
  try {
    storage()?.removeItem(ASSISTANT_STORAGE_KEY);
  } catch {
    // nothing to do
  }
}
