import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";

import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./index.css";

// Conservative defaults: one retry, no refetch on window focus (the backend is a local
// prototype; state-changing calls are never retried implicitly).
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
    mutations: { retry: 0 },
  },
});

const root = document.getElementById("root");
if (root === null) {
  throw new Error("missing #root element");
}

createRoot(root).render(
  <StrictMode>
    {/* Last-resort boundary: if the shell itself fails, the disclaimer is still shown. */}
    <ErrorBoundary withDisclaimer>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
