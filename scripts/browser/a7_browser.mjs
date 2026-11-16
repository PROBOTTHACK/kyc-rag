// A7 E10 and E12: the browser matrix and the recorded demo (A7_PLAN.md §7).
//
//   node scripts/browser/a7_browser.mjs matrix   -> docs/assistant/a7/browser.md
//   node scripts/browser/a7_browser.mjs demo     -> the demo video, outside git
//
// Needs the stack, the API (`make api`, deterministic: GROQ_LIVE_ENABLED=false) and the dev
// server (`make web`). Synthetic inputs only. Output that is not a report goes under
// data/processed/a7/ (gitignored).
//
// Gates per width (1440, 1024, 768, 390): no horizontal page overflow and no page scroll at
// every step; no internal horizontal scroll in the conversation; zero console errors and
// warnings; a keyboard-only completion (Tab / Enter only) to a checklist; reduced motion
// switches the animations off; the print stylesheet shows the checklist and hides the chrome.

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import { dirname, join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { launch } from "./cdp.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BASE = process.env.A7_BASE_URL ?? "http://127.0.0.1:5173";
const URL = `${BASE}/open-account`;
const OUT = join(ROOT, "data/processed/a7/browser");
const ALL_WIDTHS = [
  [1440, 900],
  [1024, 768],
  [768, 1024],
  [390, 844],
];
// A7_WIDTHS=390,1440 runs a subset (debugging); the report is written for full runs only.
const WIDTHS = process.env.A7_WIDTHS
  ? ALL_WIDTHS.filter(([w]) => process.env.A7_WIDTHS.split(",").includes(String(w)))
  : ALL_WIDTHS;
const SYNTHETIC_PAN = "ABCPE1234F"; // synthetic (A7 PII set pan-01), never a real identifier

// --- in-page helpers (re-installed after every navigation) ----------------------------------

const HELPERS = `window.__a7 = window.__a7 || (() => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
  const visible = (e) => {
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && !e.closest("[hidden]");
  };
  const label = (e) => norm(e.getAttribute("aria-label") || e.textContent);
  const buttons = () => [...document.querySelectorAll("button")].filter(visible);
  const find = (t) => {
    t = norm(t);
    const bs = buttons();
    return bs.find((b) => label(b) === t) || bs.find((b) => norm(b.textContent) === t)
      || bs.find((b) => label(b).startsWith(t)) || bs.find((b) => label(b).includes(t));
  };
  const turns = () =>
    document.querySelectorAll('ol[aria-label="Conversation"] > li:not([aria-hidden])').length;
  const busy = () =>
    document.querySelectorAll('ol[aria-label="Conversation"] > li[aria-hidden]').length > 0;
  const layout = () => {
    const d = document.documentElement;
    const scrollers = [...document.querySelectorAll(".oa-scroll")].filter(visible);
    return {
      width: innerWidth, height: innerHeight,
      hOverflow: d.scrollWidth - d.clientWidth,
      vScroll: d.scrollHeight - d.clientHeight,
      innerHOverflow: Math.max(0, ...scrollers.map((s) => s.scrollWidth - s.clientWidth)),
      // The conversation is full-screen (h-dvh, no page scroll); the welcome screen is a
      // normal scrolling page by design (AssistantApp: min-h-dvh).
      conversation: !!document.getElementById("composer")
        || !!document.getElementById("checklist-heading"),
    };
  };
  return { norm, visible, label, buttons, find, turns, busy, layout };
})(); true`;

class Flow {
  constructor(page, { width, pauseMs = 0, shots = null, charDelayMs = 0, hold = {} }) {
    this.hold = hold; // extra pause after named steps (demo: time to read)
    this.page = page;
    this.width = width;
    this.pauseMs = pauseMs;
    this.shots = shots;
    this.charDelayMs = charDelayMs;
    this.steps = [];
    this.layoutFailures = [];
    this.pageScroll = []; // welcome screen only: reported, by design not gated
  }

  async h() {
    await this.page.eval(HELPERS);
  }

  async pause(ms = this.pauseMs) {
    if (ms) await sleep(ms);
  }

  async step(name, fn, { gateLayout = true } = {}) {
    const started = Date.now();
    try {
      await this.h();
      await fn();
      await this.h();
      const layout = await this.page.eval("__a7.layout()");
      const bad =
        layout.hOverflow > 0 ||
        layout.innerHOverflow > 0 ||
        (layout.conversation && layout.vScroll > 0);
      if (bad && gateLayout) this.layoutFailures.push({ step: name, ...layout });
      if (!layout.conversation && layout.vScroll > 0) this.pageScroll.push({ step: name, ...layout });
      this.steps.push({ name, ok: true, ms: Date.now() - started, layout });
      if (this.shots) await this.page.screenshot(join(this.shots, `${slug(name)}.png`), fs);
    } catch (err) {
      this.steps.push({ name, ok: false, ms: Date.now() - started, error: String(err.message) });
      if (this.shots) await this.page.screenshot(join(this.shots, `FAIL-${slug(name)}.png`), fs);
      throw err;
    }
    await this.pause(this.pauseMs + (this.hold[name] ?? 0));
  }

  async click(text, { optionsFallback = true } = {}) {
    await this.h();
    const js = (t) => `(() => { const b = __a7.find(${JSON.stringify(t)});
      if (!b) return null; if (b.disabled) return "disabled";
      b.scrollIntoView({ block: "center" }); const r = b.getBoundingClientRect();
      return [r.x + r.width / 2, r.y + r.height / 2]; })()`;
    let at = await this.page.eval(js(text));
    if (at === null && optionsFallback) {
      const more = await this.page.eval(js("show options"));
      if (Array.isArray(more)) {
        await this.page.mouseClick(...more);
        await sleep(200);
        at = await this.page.eval(js(text));
      }
    }
    if (!Array.isArray(at)) throw new Error(`button not clickable: ${text} (${at})`);
    await this.page.mouseClick(...at);
  }

  async afterTurn(before) {
    await this.page.waitFor(`__a7.turns() >= ${before + 2} && !__a7.busy()`, {
      timeoutMs: 30_000,
      what: "the assistant's answer",
    });
    await sleep(150);
  }

  async say(text) {
    await this.h();
    const before = await this.page.eval("__a7.turns()");
    const at = await this.page.eval(`(() => { const t = document.getElementById("composer");
      t.scrollIntoView({ block: "center" }); const r = t.getBoundingClientRect();
      return [r.x + 20, r.y + r.height / 2]; })()`);
    await this.page.mouseClick(...at);
    await this.page.typeText(text, { charDelayMs: this.charDelayMs });
    await this.pause(Math.min(this.pauseMs, 600));
    await this.page.enter();
    await this.afterTurn(before);
  }

  async choose(text) {
    await this.h();
    const before = await this.page.eval("__a7.turns()");
    await this.click(text);
    await this.afterTurn(before);
    // The customer's bubble names the choice (regression: an empty or "0" bubble, A7.6).
    const bubble = await this.page.eval(`(() => {
      const mine = [...document.querySelectorAll('ol[aria-label="Conversation"] > li.justify-end')];
      const last = mine.at(-1); return last ? last.innerText.replace(/^You:\s*/, "").trim() : null; })()`);
    if (!bubble || /^d+$/.test(bubble)) throw new Error(`customer bubble for "${text}": "${bubble}"`);
  }

  text() {
    return this.page.eval("document.body.innerText");
  }

  async expectText(pattern, what) {
    const body = await this.text();
    if (!new RegExp(pattern, "i").test(body)) throw new Error(`missing on screen: ${what}`);
  }

  checklistCount() {
    return this.page.eval(`(() => { const b = __a7.buttons().find((x) =>
      __a7.norm(x.textContent).startsWith("checklist")); return b ? b.textContent : null; })()`);
  }
}

/** A new conversation: the tab's stored session (sessionStorage) is cleared first. */
async function freshStart(page) {
  await page.goto(URL);
  await page.eval("sessionStorage.clear(); true");
  await page.goto(URL);
}

const slug = (s) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");

// --- the scripted demo flow (A7_PLAN.md §7), also the main flow of the matrix ----------------

async function demoFlow(f, { mobileReshoots = null } = {}) {
  const { page } = f;
  await f.step("01 welcome", async () => {
    await freshStart(page);
    await page.waitFor(`!!document.body.innerText.match(/I understand/)`, { what: "welcome" });
    await f.expectText("Independent prototype", "the disclaimer");
    await f.expectText("Don't enter real personal", "the real-data warning");
    const box = await page.eval(`!!document.getElementById("composer")`);
    if (box) throw new Error("a text box is shown before Start");
  });
  await f.step("02 start", async () => {
    await f.click("I understand — start");
    await page.waitFor(`!!document.getElementById("composer")`, { what: "the composer" });
  });
  await f.step("03 greeting", async () => {
    await f.say("Hi");
    await f.expectText("current account", "the account question");
  });
  await f.step("04 current account", async () => f.choose("Current account"));
  await f.step("05 llp checklist", async () => {
    await f.choose("limited liability partnership");
    if (!(await f.checklistCount())) throw new Error("no checklist button");
  });
  await f.step("06 checklist form 60", async () => {
    await f.click("Checklist");
    await page.waitFor(`!!document.getElementById("checklist-heading")`, { what: "checklist" });
    await f.click("Details: Form 60");
    await sleep(200);
    await f.expectText("Approximate reading|Scanned form", "the scanned-text warning");
    await f.h();
    const src = await page.eval(`(() => { const b = __a7.buttons().find((x) =>
      __a7.norm(x.textContent).startsWith("source")); if (!b) return false;
      b.click(); return true; })()`);
    if (!src) throw new Error("no Source toggle");
    await sleep(300);
    await f.expectText("page \\d", "the source page");
  });
  await f.step("07 back to conversation", async () => {
    await f.click("Checklist");
    await page.waitFor(`!!document.getElementById("composer")`, { what: "conversation" });
  });
  await f.step("08 pan card question", async () => {
    await f.say("Do I need a copy of the PAN card?");
  });
  const count = await f.checklistCount();
  await f.step("09 daughter offer", async () => {
    await f.say("How can I open an account for my daughter who is six?");
    await f.h();
    if (!(await page.eval(`!!__a7.find("Ask about a separate account")`))) {
      throw new Error("no separate-account offer");
    }
  });
  await f.step("10 separate account", async () => f.choose("Ask about a separate account"));
  await f.step("11 india fallback", async () => {
    await f.say("No, we are in India.");
    await f.expectText("HDFC Bank", "the fallback");
  });
  await f.step("12 back to llp", async () => {
    await f.say("what about the LLP current account?");
    await f.choose("Yes, go back to it");
    const again = await f.checklistCount();
    if (again !== count) throw new Error(`checklist changed: ${count} -> ${again}`);
  });
  await f.step("13 your answers", async () => {
    await f.click("Your answers");
    await page.waitFor(`!!document.querySelector('[role="dialog"]')`, { what: "drawer" });
    await f.expectText("Other accounts", "the other accounts");
  });
  if (mobileReshoots) await mobileReshoots("13 your answers");
  await f.step("14 close answers", async () => {
    await f.click("Close");
    await page.waitFor(`!document.querySelector('[role="dialog"]')`, { what: "drawer closed" });
  });
  await f.step("15 synthetic pan blocked", async () => {
    await f.say(`my PAN is ${SYNTHETIC_PAN}`);
    await f.expectText("Message not sent", "the blocked notice");
    if ((await f.text()).includes(SYNTHETIC_PAN)) throw new Error("the PAN is shown");
  });
  const turns = await page.eval("__a7.turns()");
  await f.step("16 reload restores", async () => {
    await page.reload();
    await f.h();
    await page.waitFor(`__a7.turns() === ${turns}`, { what: `${turns} restored turns` });
    if ((await f.text()).includes(SYNTHETIC_PAN)) throw new Error("the PAN is shown");
    const inUrl = await page.eval("location.href");
    if (/[0-9a-f]{8}-[0-9a-f]{4}-/.test(inUrl)) throw new Error("session id in the URL");
  });
  if (mobileReshoots) await mobileReshoots("16 reload restores");
  await f.step("17 print preview", async () => {
    await f.click("Checklist");
    await page.waitFor(`!!document.getElementById("checklist-heading")`, { what: "checklist" });
    await page.media("print");
    await sleep(300);
    const p = await page.eval(`(() => {
      const hidden = [...document.querySelectorAll('[data-print="hide"]')]
        .every((e) => getComputedStyle(e).display === "none");
      const heading = document.getElementById("checklist-heading");
      return { hidden, checklist: !!heading && __a7.visible(heading) }; })()`);
    if (!p.hidden || !p.checklist) throw new Error(`print layout ${JSON.stringify(p)}`);
  }, { gateLayout: false }); // print media lays the checklist out for paper
  await page.media("");
  await f.step("18 start again", async () => {
    await f.click("Start again");
    await page.waitFor(`!!document.body.innerText.match(/I understand/) ||
      !!document.getElementById("composer")`, { what: "a new conversation" });
    await f.h();
    if (await page.eval(`!!__a7.find("I understand — start")`)) await f.click("I understand — start");
    await page.waitFor(`!!document.getElementById("composer")`, { what: "the composer" });
  });
  await f.step("19 nri from free text", async () => {
    await f.say("I live in Singapore and want a savings account");
  });
  await f.step("20 nri confirm", async () => {
    for (let i = 0; i < 3 && (await f.page.eval(`!!__a7.find("Yes")`)); i++) await f.choose("Yes");
  });
  await f.step("21 nre joint minor", async () => {
    await f.choose("NRE");
    await f.choose("Joint");
    await f.choose("No");
    if (!(await f.checklistCount())) throw new Error("no NRI checklist");
  });
  await f.step("22 nri checklist", async () => {
    await f.click("Checklist");
    await page.waitFor(`!!document.getElementById("checklist-heading")`, { what: "checklist" });
  });
}

// --- keyboard only ---------------------------------------------------------------------------

async function tabTo(f, text, maxTabs = 150) {
  for (let i = 0; i < maxTabs; i++) {
    await f.page.tab();
    await f.h();
    const hit = await f.page.eval(`(() => { const a = document.activeElement;
      if (!a || a.tagName !== "BUTTON") return false;
      const l = __a7.label(a); const t = __a7.norm(${JSON.stringify(text)});
      return l === t || l.startsWith(t) || l.includes(t); })()`);
    if (hit) {
      const ring = await f.page.eval(`(() => { const s = getComputedStyle(document.activeElement);
        return (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0)
          || (s.boxShadow && s.boxShadow !== "none"); })()`);
      f.focusRings.push({ target: text, visible: !!ring });
      return;
    }
  }
  throw new Error(`keyboard: ${text} not reachable in ${maxTabs} Tab presses`);
}

async function keyboardFlow(f) {
  f.focusRings = [];
  const { page } = f;
  await f.step("k1 welcome", async () => {
    await freshStart(page);
    await page.waitFor(`!!document.body.innerText.match(/I understand/)`, { what: "welcome" });
  });
  await f.step("k2 start by keyboard", async () => {
    await tabTo(f, "I understand — start");
    await page.enter();
    await page.waitFor(`!!document.getElementById("composer")`, { what: "the composer" });
  });
  for (const [name, label] of [
    ["k3 current by keyboard", "Current account"],
    ["k4 company by keyboard", "A company"],
  ]) {
    await f.step(name, async () => {
      const before = await page.eval("__a7.turns()");
      await tabTo(f, label);
      await page.enter();
      await f.afterTurn(before);
    });
  }
  await f.step("k5 open checklist by keyboard", async () => {
    await tabTo(f, "Checklist");
    await page.enter();
    await page.waitFor(`!!document.getElementById("checklist-heading")`, { what: "checklist" });
    await tabTo(f, "Details:");
    await page.enter();
    await sleep(200);
    const expanded = await page.eval(`document.activeElement.getAttribute("aria-label")
      .startsWith("Hide details")`);
    if (!expanded) throw new Error("details did not open by keyboard");
  });
}

// --- reduced motion --------------------------------------------------------------------------

async function reducedMotion(f) {
  const { page } = f;
  await f.step("m1 reduced motion", async () => {
    await page.media("", [{ name: "prefers-reduced-motion", value: "reduce" }]);
    await freshStart(page);
    await page.waitFor(`!!document.body.innerText.match(/I understand/)`, { what: "welcome" });
    await f.click("I understand — start");
    await page.waitFor(`!!document.getElementById("composer")`, { what: "the composer" });
    await f.say("Hi");
    const r = await page.eval(`(() => {
      const els = [...document.querySelectorAll(".oa-rise, .oa-fade")];
      const moving = els.filter((e) => getComputedStyle(e).animationName !== "none").length;
      const trans = [...document.querySelectorAll(".oa *")].filter((e) =>
        getComputedStyle(e).transitionDuration.split(",").some((d) => parseFloat(d) > 0)).length;
      return { animated: els.length, moving, withTransitions: trans }; })()`);
    f.motion = r;
    if (r.animated === 0 || r.moving > 0 || r.withTransitions > 0) {
      throw new Error(`reduced motion not honoured ${JSON.stringify(r)}`);
    }
  });
  await page.media("", []);
}

// --- runners ---------------------------------------------------------------------------------

async function runMatrix() {
  fs.mkdirSync(OUT, { recursive: true });
  const results = [];
  let browser = null;
  for (const [width, height] of WIDTHS) {
    const shots = join(OUT, String(width));
    fs.mkdirSync(shots, { recursive: true });
    const { page, close } = await launch();
    browser = page.browser;
    await page.viewport(width, height);
    const row = { width, height, flows: {} };
    for (const [name, fn] of [
      ["main", demoFlow],
      ["keyboard", keyboardFlow],
      ["reduced-motion", reducedMotion],
    ]) {
      const f = new Flow(page, { width, shots: name === "main" ? shots : null });
      let error = null;
      try {
        await fn(f);
      } catch (err) {
        error = String(err.message);
      }
      row.flows[name] = {
        ok: error === null && f.layoutFailures.length === 0,
        error,
        steps: f.steps.length,
        failedSteps: f.steps.filter((s) => !s.ok).map((s) => s.name),
        layoutFailures: f.layoutFailures,
        welcomeScroll: f.pageScroll,
        focusRings: f.focusRings,
        motion: f.motion,
      };
      console.log(`${width}px ${name}: ${row.flows[name].ok ? "PASS" : "FAIL"} ${error ?? ""}`);
    }
    row.console = page.console;
    results.push(row);
    await close();
  }
  const record = { date: new Date().toISOString(), base: BASE, browser, commit: git(), results };
  fs.writeFileSync(join(OUT, "matrix.json"), JSON.stringify(record, null, 2) + "\n");
  if (WIDTHS.length === ALL_WIDTHS.length) {
    fs.writeFileSync(join(ROOT, "docs/assistant/a7/browser.md"), matrixReport(record));
  }
  const ok = results.every((r) => r.console.length === 0 && Object.values(r.flows).every((x) => x.ok));
  console.log(ok ? "browser matrix: PASS" : "browser matrix: FAIL");
  return ok ? 0 : 1;
}

function git() {
  try {
    const commit = execFileSync("git", ["rev-parse", "--short", "HEAD"], { cwd: ROOT })
      .toString()
      .trim();
    const dirty = execFileSync("git", ["status", "--porcelain"], { cwd: ROOT }).toString().trim();
    return dirty ? `${commit} (with uncommitted changes)` : commit;
  } catch {
    return "unknown";
  }
}

function matrixReport(r) {
  const yes = (b) => (b ? "pass" : "**FAIL**");
  let md =
    "# A7: browser matrix (E10)\n\n" +
    `Generated by \`node scripts/browser/a7_browser.mjs matrix\` on ${r.date} at commit ` +
    `\`${r.commit}\`. Headless ${r.browser} over the DevTools protocol (Node's built-in ` +
    "WebSocket, no added dependency), against the Vite dev server and the real API on the " +
    "development corpus, **deterministic (no live LLM)**. Synthetic inputs only.\n\n" +
    "Flows per width:\n" +
    "- **main**: the §7 demo script (welcome, greeting, LLP checklist with the scanned Form " +
    "60/61 source, PAN-card question, daughter offer and the India fallback, back to the LLP " +
    "checklist unchanged, your answers, a synthetic PAN blocked and never shown, reload, " +
    "print layout, start again, NRI savings to a checklist);\n" +
    "- **keyboard**: Tab and Enter only, from the welcome screen to an opened checklist item;\n" +
    "- **reduced-motion**: `prefers-reduced-motion: reduce` switches every fade and transition off.\n\n" +
    "Layout is checked after every step: no horizontal page overflow, no horizontal scroll " +
    "inside the conversation, and on the conversation screens no page scroll (the chat is " +
    "full-screen and scrolls inside). The welcome screen is a normal scrolling page by design " +
    "(`min-h-dvh`): it is checked for horizontal overflow only. The print step is checked " +
    "for its print layout (chrome hidden, checklist shown) rather than screen layout. After " +
    "every chip or offer choice, the customer's bubble must name the choice (not empty, not " +
    "a raw value).\n\n" +
    "| Width | Main flow | Layout (all steps) | Keyboard only | Visible focus | Reduced motion | Console errors / warnings |\n" +
    "|---|---|---|---|---|---|---|\n";
  for (const row of r.results) {
    const f = row.flows;
    const layoutBad = Object.values(f).flatMap((x) => x.layoutFailures);
    const rings = f.keyboard.focusRings ?? [];
    md +=
      `| ${row.width}×${row.height} | ${yes(!f.main.error)} (${f.main.steps} steps) | ` +
      `${yes(layoutBad.length === 0)} | ${yes(!f.keyboard.error)} | ` +
      `${rings.filter((x) => x.visible).length}/${rings.length} | ` +
      `${yes(!f["reduced-motion"].error)} | ${row.console.length} |\n`;
  }
  const problems = r.results.flatMap((row) =>
    Object.entries(row.flows)
      .filter(([, x]) => !x.ok)
      .map(([n, x]) => `- ${row.width}px ${n}: ${x.error ?? ""} ${JSON.stringify(x.layoutFailures)}`),
  );
  const consoleLines = r.results.flatMap((row) =>
    row.console.map((c) => `- ${row.width}px ${c.type}: ${c.text}`),
  );
  md += `\n${problems.length ? "### Failures\n\n" + problems.join("\n") + "\n" : ""}`;
  md += consoleLines.length ? "\n### Console\n\n" + consoleLines.join("\n") + "\n" : "";
  const ok = r.results.every(
    (row) => row.console.length === 0 && Object.values(row.flows).every((x) => x.ok),
  );
  md +=
    "\nScreenshots of every main-flow step: `data/processed/a7/browser/<width>/` (local, not " +
    "committed); the record is `data/processed/a7/browser/matrix.json`.\n\n" +
    `- Result: **${ok ? "PASS" : "FAIL"}**.\n`;
  return md;
}

// --- the recorded demo (E12) ----------------------------------------------------------------

async function runDemo() {
  const dir = join(ROOT, "data/processed/a7/demo");
  const frames = join(dir, "frames");
  fs.rmSync(frames, { recursive: true, force: true });
  fs.mkdirSync(frames, { recursive: true });
  const keyframes = join(dir, "keyframes");
  fs.mkdirSync(keyframes, { recursive: true });
  const { page, close } = await launch({ port: 9334 });
  await page.viewport(1440, 900, false);
  const list = [];
  page.s.on("Page.screencastFrame", async (p) => {
    const file = join(frames, `${String(list.length).padStart(5, "0")}.jpg`);
    fs.writeFileSync(file, Buffer.from(p.data, "base64"));
    list.push({ file, t: p.metadata.timestamp });
    try {
      await page.s.send("Page.screencastFrameAck", { sessionId: p.sessionId });
    } catch {
      /* the recording is stopping */
    }
  });
  await page.s.send("Page.startScreencast", {
    format: "jpeg",
    quality: 85,
    maxWidth: 1440,
    maxHeight: 900,
  });
  const f = new Flow(page, {
    width: 1440,
    pauseMs: 3500,
    shots: keyframes,
    charDelayMs: 45,
    hold: {
      "01 welcome": 3000,
      "05 llp checklist": 4000,
      "06 checklist form 60": 6000,
      "08 pan card question": 4000,
      "09 daughter offer": 2500,
      "11 india fallback": 5000,
      "12 back to llp": 3000,
      "13 your answers": 3000,
      "15 synthetic pan blocked": 4000,
      "17 print preview": 3000,
      "22 nri checklist": 6000,
    },
  });
  const reshoot = async (name) => {
    await page.viewport(390, 844, true);
    await sleep(2500);
    await page.screenshot(join(keyframes, `${slug(name)}-390.png`), fs);
    await sleep(500);
    await page.viewport(1440, 900, false);
    await sleep(1200);
  };
  let error = null;
  try {
    await demoFlow(f, { mobileReshoots: reshoot });
    await sleep(3000);
  } catch (err) {
    error = String(err.message);
  }
  await page.s.send("Page.stopScreencast");
  await sleep(500);
  const consoleErrors = page.console;
  await close();
  if (error) {
    console.error(`demo FAILED: ${error}`);
    return 1;
  }
  // Variable frame timing -> constant 25 fps H.264, every frame letterboxed to 1440x900.
  const concat = join(dir, "frames.txt");
  const lines = list.map((fr, i) => {
    const next = list[i + 1]?.t ?? fr.t + 2;
    return `file '${fr.file.replaceAll("\\", "/")}'\nduration ${Math.max(0.04, next - fr.t).toFixed(3)}`;
  });
  lines.push(`file '${list.at(-1).file.replaceAll("\\", "/")}'`);
  fs.writeFileSync(concat, lines.join("\n") + "\n");
  const video = join(dir, "account-opening-assistant-demo.mp4");
  execFileSync(
    ffmpegPath(),
    [
      "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat,
      "-vf",
      "scale=1440:900:force_original_aspect_ratio=decrease,pad=1440:900:(ow-iw)/2:(oh-ih)/2:color=0x0b0d10,fps=25,format=yuv420p",
      "-c:v", "libx264", "-preset", "medium", "-crf", "26", "-movflags", "+faststart", video,
    ],
    { stdio: "inherit" },
  );
  const sha = createHash("sha256").update(fs.readFileSync(video)).digest("hex");
  const seconds = list.length ? list.at(-1).t - list[0].t : 0;
  const info = {
    video,
    bytes: fs.statSync(video).size,
    sha256: sha,
    seconds: Math.round(seconds),
    frames: list.length,
    steps: f.steps.map((s) => s.name),
    console: consoleErrors,
    commit: git(),
    date: new Date().toISOString(),
  };
  fs.writeFileSync(join(dir, "demo.json"), JSON.stringify(info, null, 2) + "\n");
  console.log(JSON.stringify(info, null, 2));
  return 0;
}

/** ffmpeg (a local tool, not a project dependency): FFMPEG may name the binary or its folder. */
function ffmpegPath() {
  const env = process.env.FFMPEG;
  if (!env) return "ffmpeg";
  if (fs.existsSync(env) && fs.statSync(env).isDirectory()) {
    return join(env, process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg");
  }
  return env;
}

const mode = process.argv[2];
const code = mode === "matrix" ? await runMatrix() : mode === "demo" ? await runDemo() : 2;
if (code === 2) console.error("usage: node scripts/browser/a7_browser.mjs matrix|demo");
process.exit(code);
