import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

/**
 * The assistant's current session id, held in memory above the routes so that opening a
 * report and coming back keeps it. It is never written to browser storage, the URL or logs:
 * a session id is the only access credential (L6), and where the browser may keep it is the
 * open decision M5-D4. Reloading the page forgets it.
 */
interface AssistantSession {
  sessionId: string | null;
  setSessionId: (id: string | null) => void;
}

const Context = createContext<AssistantSession | null>(null);

export function AssistantSessionProvider({ children }: { children: ReactNode }) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const value = useMemo(() => ({ sessionId, setSessionId }), [sessionId]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useAssistantSession(): AssistantSession {
  const value = useContext(Context);
  if (value === null) throw new Error("AssistantSessionProvider is missing");
  return value;
}
