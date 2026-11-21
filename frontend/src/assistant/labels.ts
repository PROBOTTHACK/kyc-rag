/**
 * Interface wording for the account opening assistant (A6).
 *
 * Only the interface's own words live here: headings, button labels, slot names and error
 * texts. Every requirement, condition, quotation, page, source and classification label the
 * customer sees comes from the API answer (A5), never from this file.
 */
import type { ApiError } from "../api/errors";

export const PRODUCT_NAME = "Account Opening Assistant";

/** The persistent disclaimer (A1 §7), reworded for customers without dropping any claim. */
export const DISCLAIMER_LINES = {
  headline: "Independent prototype — not an official HDFC Bank service.",
  detail:
    "Not affiliated with or endorsed by HDFC Bank or the RBI. Not a compliance or KYC approval. " +
    "Synthetic data only. Every result needs to be checked with HDFC Bank.",
} as const;

export const REAL_DATA_WARNING = {
  title: "Don't enter real personal or banking information",
  body:
    "This prototype is for demonstration only. Don't type your name, contact details, or " +
    "PAN, Aadhaar, passport, card or account numbers. Conversations are kept on the " +
    "prototype's server.",
  short: "Don't include real personal or banking details.",
} as const;

export const WELCOME = {
  eyebrow: "Account opening, explained",
  title: "Know exactly what to bring before you open an account.",
  lead:
    "Answer a few short questions. The assistant reads the HDFC Bank account opening forms " +
    "held in this prototype and builds your checklist: every item linked to the form and " +
    "page it comes from.",
  points: [
    {
      title: "Evidence, not guesses",
      body: "Every item cites a form and page. When the documents don't cover your case, it says so.",
    },
    {
      title: "Dated sources, clearly marked",
      body: "The forms are dated copies. Always confirm the requirements with HDFC Bank before you apply.",
    },
    {
      title: "No approvals, no scoring",
      body: "It doesn't approve, reject or assess applications. It only shows what the forms ask for.",
    },
  ],
  start: "I understand — start",
  resuming: "Reopening your conversation…",
  steps: ["Tell it about the account", "Answer a few questions", "Get your cited checklist"],
} as const;

/** Names of the profile slots (API keys) as the customer reads them. */
export const SLOT_LABELS: Record<string, string> = {
  account_type: "Account type",
  customer_category: "Account holder",
  nri_scheme: "NRE or NRO",
  holding: "Holders",
  minor: "Opened for a minor",
};

export function slotLabel(slot: string): string {
  return SLOT_LABELS[slot] ?? slot.replaceAll("_", " ");
}

export const STATUS_LABELS: Record<string, string> = {
  not_sure: "Not sure",
  skipped: "Skipped",
};

export const ORIGIN_LABELS: Record<string, string> = {
  chosen: "You chose this",
  stated: "From your message",
  corrected: "You changed this",
};

/** How a choice turn appears in the customer's own bubble. */
export const ACTION_LABELS = {
  not_sure: "I'm not sure",
  skip: "Skip this question",
  explain: "What does this mean?",
  confirm_yes: "Yes",
  confirm_no: "No",
} as const;

export const PROGRESS_STEPS = [
  "Account type",
  "Who it's for",
  "A few details",
  "Your checklist",
] as const;

export const GROUPS = {
  documents: {
    heading: "Documents and declarations",
    intro: "What the form asks you to provide, sign or complete.",
  },
  information: {
    heading: "Information to provide",
    intro: "Details the form has fields for.",
  },
  not_confirmed: {
    heading: "Could not be confirmed",
    intro: "Things you asked about that the available documents don't confirm.",
  },
} as const;

export const ITEM_KIND_LABELS: Record<string, string> = {
  document: "Document",
  declaration: "Declaration",
  information: "Information",
};

export const TEXT = {
  send: "Send",
  sending: "Checking…",
  composerLabel: "Type your question or answer",
  composerHint: "Press Enter to send, Shift + Enter for a new line.",
  startAgain: "Start again",
  startNew: "Start a new conversation",
  tryAgain: "Try again",
  dismiss: "Dismiss",
  change: "Change",
  cancel: "Cancel",
  yourAnswers: "Your answers",
  noAnswersYet: "Your answers will appear here.",
  progress: "Progress",
  checklistReady: "Your checklist is ready.",
  checklistUpdated: "Your checklist has been updated.",
  viewChecklist: "Open checklist",
  otherAccounts: "Other accounts in this conversation",
  showOptions: "Show options",
  newConversation: "New conversation",
  hideOptions: "Hide options",
  otherAccountsHint: "Each account keeps its own answers and checklist.",
  nowDiscussing: "Now discussing",
  goBackTo: "Go back to",
  backToConversation: "Back to the conversation",
  checklistHeading: "Your checklist",
  sourcesHeading: "Documents this checklist uses",
  aboutAnswers: "About these answers",
  scannedSource: "Scanned form: the text may be inaccurate.",
  messageNotSent: "Message not sent.",
  sourceToggle: "Source",
  relatedToggle: "Related form field (context only, not a source for this item)",
  sameWording: "The same wording also appears here",
  formSays: "The form says",
  approximate: "Approximate reading of the scanned form",
  scannedPassage:
    "Text read automatically from the scanned page; it may contain errors and is shown " +
    "for reference only.",
  page: (page: number, end: number | null | undefined) =>
    end && end !== page ? `pages ${page}–${end}` : `page ${page}`,
  chooseOne: "Choose one",
  orType: "Or type your answer below.",
  coveredHeading: "Covered by the documents",
  coveredHint: "Choose one to change your answer.",
  conversationTab: "Conversation",
  checklistTab: "Checklist",
  expandAll: "Expand all",
  collapseAll: "Collapse all",
  details: "Details",
  hideDetails: "Hide details",
  whyHere: "Why this is here",
  print: "Print checklist",
  answersButton: "Your answers",
  close: "Close",
  updated: "Updated",
  changeTo: (slot: string) => `Change: ${slot}`,
} as const;

export const ENDED = {
  SESSION_EXPIRED:
    "This conversation has expired after 24 hours without activity. You can start a new one.",
  SESSION_NOT_FOUND: "This conversation could not be found. You can start a new one.",
} as const;

/** Customer wording for an API error (codes only; the server's text is never shown). */
export function customerError(error: ApiError): string {
  switch (error.code) {
    case "NETWORK_ERROR":
    case "TIMEOUT":
      return "We couldn't reach the assistant. Check your connection and try again.";
    case "ASSISTANT_UNAVAILABLE":
    case "INTERNAL_ERROR":
    case "BAD_RESPONSE":
      return "The assistant isn't available right now. Please try again in a moment.";
    case "VALIDATION_ERROR":
    case "IDEMPOTENCY_KEY_REQUIRED":
    case "IDEMPOTENCY_MISMATCH":
      return "That couldn't be sent. Please try again.";
    default:
      return error.status >= 500
        ? "The assistant isn't available right now. Please try again in a moment."
        : "Something went wrong. Please try again.";
  }
}
