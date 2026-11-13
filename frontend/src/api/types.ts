/**
 * Named aliases for the API contract. `schema.d.ts` is generated from the committed
 * `docs/api/openapi.json` (`npm run gen:api`; `npm run check:api` fails when stale) and is
 * never edited by hand. Keys the API may omit are optional here (`--default-non-nullable
 * false`), so the types never promise a field that can be absent.
 */
import type { components, paths } from "./schema";

type Schemas = components["schemas"];

export type SessionCreated = Schemas["SessionCreated"];
export type SessionView = Schemas["SessionView"];
export type WorkflowView = Schemas["WorkflowView"];
export type NodeEvent = Schemas["NodeEvent"];
export type PendingClarification = Schemas["PendingClarification"];
export type Question = Schemas["Question"];
export type RunAccepted = Schemas["RunAccepted"];
export type RunStatus = Schemas["RunStatus"];
export type ComplianceReport = Schemas["ComplianceReport"];
export type ReviewCasesResponse = Schemas["ReviewCasesResponse"];
export type ReviewCase = Schemas["ReviewCase"];
export type ProgressEvent = Schemas["ProgressEvent"];
export type ErrorResponse = Schemas["ErrorResponse"];
export type HealthResponse = Schemas["HealthResponse"];

// Request bodies, taken from the generated operations (not redeclared).
type JsonBody<Op> = Op extends { requestBody?: { content: { "application/json": infer B } } }
  ? B
  : never;

export type CreateSessionRequest = JsonBody<paths["/api/v1/sessions"]["post"]>;
export type MessageRequest = JsonBody<paths["/api/v1/sessions/{session_id}/messages"]["post"]>;
export type ResumeRequest = JsonBody<paths["/api/v1/sessions/{session_id}/resume"]["post"]>;
export type RetryRequest = JsonBody<paths["/api/v1/sessions/{session_id}/retry"]["post"]>;
export type DataClass = NonNullable<CreateSessionRequest["data_class"]>;
