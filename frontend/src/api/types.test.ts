/**
 * Type-level checks on the generated contract (verified by `npm run typecheck`; at run
 * time these assertions are no-ops). They pin the safety-relevant shapes: optional keys stay
 * optional, review cases are open-only, and no outcome approves or rejects.
 */
import { describe, expectTypeOf, it } from "vitest";

import type {
  ComplianceReport,
  ErrorResponse,
  ReviewCase,
  SessionView,
  WorkflowView,
} from "./types";

describe("generated API types", () => {
  it("keys the API may omit are optional", () => {
    expectTypeOf<WorkflowView>().toHaveProperty("clarification");
    expectTypeOf<undefined>().toExtend<WorkflowView["clarification"]>();
    expectTypeOf<undefined>().toExtend<WorkflowView["report_id"]>();
    expectTypeOf<undefined>().toExtend<WorkflowView["outcome"]>();
    expectTypeOf<null>().toExtend<SessionView["workflow"]>();
  });

  it("review cases are open-only", () => {
    expectTypeOf<ReviewCase["status"]>().toEqualTypeOf<"open">();
  });

  it("no outcome approves or rejects", () => {
    expectTypeOf<ComplianceReport["outcome"]>().toEqualTypeOf<
      | "BLOCKED"
      | "INSUFFICIENT_INFORMATION"
      | "INSUFFICIENT_EVIDENCE"
      | "DEGRADED"
      | "CHECKLIST_REVIEW_REQUIRED"
    >();
    expectTypeOf<ComplianceReport["human_review_required"]>().toEqualTypeOf<true | undefined>();
  });

  it("errors carry a code and a message", () => {
    expectTypeOf<ErrorResponse["error"]["code"]>().toEqualTypeOf<string>();
    expectTypeOf<ErrorResponse["error"]["message"]>().toEqualTypeOf<string>();
  });
});
