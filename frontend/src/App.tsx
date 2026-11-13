import { Route, Routes } from "react-router";

import { Layout } from "./components/Layout";
import { AssistantPage } from "./screens/assistant/AssistantPage";
import { AssistantSessionProvider } from "./screens/assistant/session";
import { DashboardPage } from "./screens/dashboard/DashboardPage";
import { NotFoundPage } from "./screens/NotFoundPage";
import { ReportPage } from "./screens/report/ReportPage";

/** Routes (M5-D1 scope). Document Intelligence and Observability are M6; no upload route.
 * The assistant's session id lives in memory above the routes (no browser storage, M5-D4). */
export function App() {
  return (
    <AssistantSessionProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="assistant" element={<AssistantPage />} />
          <Route path="reports/:reportId" element={<ReportPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </AssistantSessionProvider>
  );
}
