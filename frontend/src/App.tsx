import { Route, Routes } from "react-router";

import { Layout } from "./components/Layout";
import { AssistantPage } from "./screens/assistant/AssistantPage";
import { DashboardPage } from "./screens/dashboard/DashboardPage";
import { NotFoundPage } from "./screens/NotFoundPage";
import { ReportPage } from "./screens/report/ReportPage";

/** Routes (M5-D1 scope). Document Intelligence and Observability are M6; no upload route. */
export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="assistant" element={<AssistantPage />} />
        <Route path="reports/:reportId" element={<ReportPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
