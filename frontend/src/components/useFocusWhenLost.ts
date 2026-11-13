import { useEffect, type RefObject } from "react";

/**
 * Accessibility (M5 S7): when a step of the workflow replaces the control the user just
 * used (e.g. "Ask" or "Send answers" disappears while the run continues), keyboard focus
 * falls back to the page body and a keyboard or screen-reader user has to start again from
 * the top. When this component mounts and focus has been lost in that way, move it to the
 * new section's heading (give the heading `tabIndex={-1}`). Focus the user has placed
 * elsewhere is never taken away.
 */
export function useFocusWhenLost(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const active = document.activeElement;
    if (active === null || active === document.body) ref.current?.focus();
  }, [ref]);
}
