import { render, screen } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it } from "vitest";

import { useFocusWhenLost } from "./useFocusWhenLost";

function Section() {
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusWhenLost(heading);
  return (
    <h2 ref={heading} tabIndex={-1}>
      New section
    </h2>
  );
}

function Page({ show }: { show: boolean }) {
  return (
    <>
      <input aria-label="Elsewhere" />
      {show && <Section />}
    </>
  );
}

describe("useFocusWhenLost", () => {
  it("moves lost focus (on the body) to the new section's heading", () => {
    const { rerender } = render(<Page show={false} />);
    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).toBe(document.body);
    rerender(<Page show />);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "New section" }));
  });

  it("never takes focus the user has placed elsewhere", () => {
    const { rerender } = render(<Page show={false} />);
    const input = screen.getByRole("textbox", { name: "Elsewhere" });
    input.focus();
    rerender(<Page show />);
    expect(document.activeElement).toBe(input);
  });
});
