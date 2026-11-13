import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";

import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./index.css";

// Retries are bounded in the API client itself (transient failures only, same
// Idempotency-Key), so React Query adds none; no refetch on window focus.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
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
