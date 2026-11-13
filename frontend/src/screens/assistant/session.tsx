import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  forgetAll,
  loadStored,
  saveStored,
  setCurrent,
  type StoredSession,
  type StoredState,
} from "../../storage/sessions";

/**
 * The assistant's current session and the sessions known to this browser tab, above the
 * routes. Persisted in `sessionStorage` only (M5-D4): it survives reloads and navigation in
 * the same tab and is gone when the tab closes. Session ids are never put in the URL, the
 * page text or logs (a session id is the only access credential, L6).
 */
interface AssistantSession {
  sessionId: string | null;
  setSessionId: (id: string | null) => void;
  /** Sessions started or opened in this tab, oldest first. */
  known: readonly StoredSession[];
  /** Clear the locally stored ids. The backend is not contacted. */
  forget: () => void;
}

const Context = createContext<AssistantSession | null>(null);

export function AssistantSessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<StoredState>(loadStored);

  // Persist after each change (state updaters stay pure; StrictMode may run them twice).
  useEffect(() => saveStored(state), [state]);

  const setSessionId = useCallback((id: string | null) => setState((s) => setCurrent(s, id)), []);
  const forget = useCallback(() => setState(forgetAll()), []);

  const { current, sessions } = state;
  const value = useMemo(
    () => ({ sessionId: current, setSessionId, known: sessions, forget }),
    [current, sessions, setSessionId, forget],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useAssistantSession(): AssistantSession {
  const value = useContext(Context);
  if (value === null) throw new Error("AssistantSessionProvider is missing");
  return value;
}
