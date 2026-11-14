/**
 * Small interface primitives for the assistant: one inline SVG icon set (1.6px strokes, 24px
 * grid), buttons and a spinner. Decorative only: nothing here carries product content.
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

const PATHS = {
  document: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M9 13h6M9 17h4",
  info: "M4 6h16M4 10h16M4 14h10M4 18h7",
  pen: "M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4ZM13.5 6.5l4 4",
  question:
    "M9.1 9a3 3 0 1 1 4.2 2.7c-.8.4-1.3 1.1-1.3 2v.3M12 17.5v.01M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z",
  check: "m5 12.5 4.2 4.2L19 7",
  alert:
    "M12 9v4M12 16.5v.01M10.3 4 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0Z",
  shield: "M12 3 5 6v5c0 4.4 3 8.3 7 10 4-1.7 7-5.6 7-10V6l-7-3Zm-3 9 2 2 4-4",
  chevron: "m9 6 6 6-6 6",
  arrow: "M5 12h14M13 6l6 6-6 6",
  refresh:
    "M20 11a8 8 0 0 0-14.7-4.3L4 8.5M4 4v4.5h4.5M4 13a8 8 0 0 0 14.7 4.3l1.3-1.8M20 20v-4.5h-4.5",
  printer:
    "M7 9V3h10v6M7 17H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-2M7 14h10v7H7v-7Z",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4 21a8 8 0 0 1 16 0",
  users:
    "M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM2.5 20a6.5 6.5 0 0 1 13 0M16 4.3a3.5 3.5 0 0 1 0 6.4M18 14a6.5 6.5 0 0 1 3.5 6",
  building:
    "M4 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16M16 9h2a2 2 0 0 1 2 2v10M8 7h4M8 11h4M8 15h4M3 21h18",
  globe:
    "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM3.5 9h17M3.5 15h17M12 3c2.3 2.5 3.5 5.5 3.5 9s-1.2 6.5-3.5 9c-2.3-2.5-3.5-5.5-3.5-9S9.7 5.5 12 3Z",
  home: "M4 11 12 4l8 7M6 9.5V20h12V9.5M10 20v-5h4v5",
  wallet: "M4 7a2 2 0 0 1 2-2h11v4M4 7v10a2 2 0 0 0 2 2h13V9H6a2 2 0 0 1-2-2Zm12 7h.01",
  card: "M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Zm0 3h18M7 15h3",
  vault: "M4 4h16v14H4V4Zm2 14v2m12-2v2M12 14a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm0-3h.01M15 11h2",
  calendar:
    "M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm-1 5h16M8 3v4M16 3v4",
  coins:
    "M8 8a5 2 0 1 0 0 .01M3 8v4c0 1.1 2.2 2 5 2s5-.9 5-2V8M11 13.6V16c0 1.1 2.2 2 5 2s5-.9 5-2v-4c0-1.1-2.2-2-5-2-1 0-1.9.1-2.6.3",
  briefcase: "M4 8h16v11H4V8Zm5 0V5h6v3M4 13h16",
  handshake: "m11 7-2-2-6 6 2 2m6-6 2 2 4-4 6 6-6 6-3-3m-3-7-3 3 2 2 3-3m-4 4 2 2m1-5 2 2",
  seal: "M12 15a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm-3 4.5L12 18l3 1.5V14M9 14v5.5",
  family:
    "M7 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Zm10 0a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM3 21v-6a4 4 0 0 1 8 0M13 21v-6a4 4 0 0 1 8 0",
  child: "M12 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm-5 5 5-2 5 2M12 11v5m-3 5 3-5 3 5",
  layers: "m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
  x: "M6 6l12 12M18 6 6 18",
  scan: "M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3M4 12h16",
  quote: "M7 7h4v4c0 3-1.5 5-4 6M14 7h4v4c0 3-1.5 5-4 6",
  send: "M4 12 20 4l-6 16-3-7-7-1Z",
  book: "M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2V5Zm0 14a2 2 0 0 1 2-2h13M8 7h7",
  expand: "m7 10 5-5 5 5M7 14l5 5 5-5",
  collapse: "m7 5 5 5 5-5M7 19l5-5 5 5",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({
  name,
  className = "h-4 w-4",
  strokeWidth = 1.6,
}: {
  name: IconName;
  className?: string;
  strokeWidth?: number;
}) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 ${className}`}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

/** Icons for the API's chip values (decoration only; an unknown value gets a neutral icon). */
const VALUE_ICONS: Record<string, IconName> = {
  SAVINGS: "wallet",
  CURRENT: "card",
  SALARY: "briefcase",
  FIXED_DEPOSIT: "vault",
  RECURRING_DEPOSIT: "calendar",
  FCNR_RFC: "coins",
  RESIDENT_INDIVIDUAL: "home",
  NRI_INDIVIDUAL: "globe",
  SOLE_PROPRIETORSHIP: "user",
  PARTNERSHIP_FIRM: "handshake",
  LLP: "layers",
  COMPANY: "building",
  TRUST: "seal",
  HUF: "family",
  OTHER_ORGANISATION: "users",
  NRE: "globe",
  NRO: "home",
  SINGLE: "user",
  JOINT: "users",
  YES: "child",
  NO: "user",
};

export function valueIcon(value: string): IconName {
  return VALUE_ICONS[value] ?? "document";
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block animate-spin rounded-full border-2 border-line-strong border-t-accent motion-reduce:animate-none ${className}`}
    />
  );
}

/** The assistant is working: a quiet text line (no animation). */
export function Thinking() {
  return <span className="text-sm text-subtle">Checking the documents…</span>;
}

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-fg text-void font-medium hover:bg-white active:bg-fg/90 disabled:bg-line disabled:text-subtle",
  secondary:
    "border border-line-strong text-fg hover:bg-raised active:bg-surface disabled:text-subtle disabled:border-line",
  ghost: "text-muted hover:text-fg hover:bg-raised disabled:text-subtle/70",
  danger: "border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-60",
};

export function Button({
  variant = "secondary",
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; children: ReactNode }) {
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-md px-4 text-sm font-medium transition-colors duration-150 disabled:cursor-not-allowed ${VARIANTS[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

/** The product mark: a plain monogram in a hairline square (no third-party branding). */
export function Mark({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`grid place-items-center rounded-md border border-line-strong font-display text-[0.8rem] font-semibold tracking-tight text-fg ${className}`}
    >
      KR
    </span>
  );
}
