import { useParams } from "react-router";

import { Placeholder } from "../Placeholder";

export function ReportPage() {
  const { reportId } = useParams();
  return (
    <Placeholder title="Compliance Report" stage="S5">
      <p className="mt-2 text-sm text-slate-600">
        Report id: <code>{reportId}</code>
      </p>
    </Placeholder>
  );
}
