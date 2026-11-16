import { Navigate, Route, Routes } from "react-router";

import { AssistantApp } from "./assistant/AssistantApp";
import { NotFound } from "./assistant/NotFound";
import { Layout } from "./components/Layout";
import { AssistantPage } from "./screens/assistant/AssistantPage";
import { AssistantSessionProvider } from "./screens/assistant/session";
import { DashboardPage } from "./screens/dashboard/DashboardPage";
import { NotFoundPage } from "./screens/NotFoundPage";
import { ReportPage } from "./screens/report/ReportPage";

/**
 * Routes. The Account Opening Assistant is the application (`/`); `/open-account` (A6)
 * redirects to it. The preserved M5 compliance-engine screens live under `/legacy`, are not
 * linked from the assistant, and keep their own in-memory session provider (M5-D4).
 */
export function App() {
  return (
    <Routes>
      <Route index element={<AssistantApp />} />
      <Route path="open-account" element={<Navigate to="/" replace />} />
      <Route
        path="legacy"
        element={
          <AssistantSessionProvider>
            <Layout />
          </AssistantSessionProvider>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="assistant" element={<AssistantPage />} />
        <Route path="reports/:reportId" element={<ReportPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
