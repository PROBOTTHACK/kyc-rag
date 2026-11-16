// A minimal Chrome DevTools Protocol client for the A7 browser checks (A7_PLAN.md §8, owner
// decision: Node's built-in WebSocket and fetch, no new dependency). Node >= 22.
//
// Launches a local headless Chrome with a throwaway profile, opens one page, and offers the
// few page helpers the checks need. Every wait has a timeout; every loop is bounded.

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);

export function findChrome() {
  const found = CHROME_CANDIDATES.find((p) => existsSync(p));
  if (!found) throw new Error("Chrome not found; set CHROME_PATH");
  return found;
}

export class Session {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (msg.id !== undefined) {
        const p = this.pending.get(msg.id);
        if (!p) return;
        this.pending.delete(msg.id);
        if (msg.error) p.reject(new Error(`${p.method}: ${msg.error.message}`));
        else p.resolve(msg.result);
      } else {
        for (const fn of this.listeners.get(msg.method) ?? []) fn(msg.params);
      }
    });
  }

  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    return new Session(ws);
  }

  send(method, params = {}, timeoutMs = 30_000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method}: no reply in ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, {
        method,
        resolve: (v) => (clearTimeout(timer), resolve(v)),
        reject: (e) => (clearTimeout(timer), reject(e)),
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(fn);
  }

  close() {
    this.ws.close();
  }
}

/** Launch headless Chrome and return {page, close}. */
export async function launch({ port = 9333 } = {}) {
  const profile = mkdtempSync(join(tmpdir(), "a7-chrome-"));
  const proc = spawn(
    findChrome(),
    [
      "--headless=new",
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--hide-scrollbars",
      "--force-color-profile=srgb",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  let version = null;
  for (let i = 0; i < 100 && version === null; i++) {
    try {
      version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    } catch {
      await sleep(100);
    }
  }
  if (version === null) throw new Error("Chrome did not start");
  const target = await (
    await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })
  ).json();
  const session = await Session.connect(target.webSocketDebuggerUrl);
  const page = new Page(session, version.Browser);
  await page.init();
  return {
    page,
    async close() {
      session.close();
      proc.kill();
      await sleep(500);
      try {
        rmSync(profile, { recursive: true, force: true });
      } catch {
        /* Chrome may still hold a file briefly; the OS temp cleaner removes it */
      }
    },
  };
}

export class Page {
  constructor(session, browser) {
    this.s = session;
    this.browser = browser;
    this.console = []; // {type, text}
  }

  async init() {
    await this.s.send("Page.enable");
    await this.s.send("Runtime.enable");
    await this.s.send("Log.enable");
    this.s.on("Runtime.consoleAPICalled", (p) => {
      if (p.type === "error" || p.type === "warning" || p.type === "assert") {
        const text = p.args.map((a) => a.value ?? a.description ?? "").join(" ");
        this.console.push({ type: p.type, text: text.slice(0, 300) });
      }
    });
    this.s.on("Runtime.exceptionThrown", (p) =>
      this.console.push({
        type: "exception",
        text: (p.exceptionDetails.exception?.description ?? p.exceptionDetails.text).slice(0, 300),
      }),
    );
    this.s.on("Log.entryAdded", (p) => {
      if (p.entry.level === "error" || p.entry.level === "warning") {
        this.console.push({
          type: `log-${p.entry.level}`,
          text: p.entry.text.slice(0, 300),
          url: p.entry.url,
        });
      }
    });
  }

  async viewport(width, height, mobile = width < 600) {
    await this.s.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile,
    });
  }

  async media(media = "", features = []) {
    await this.s.send("Emulation.setEmulatedMedia", { media, features });
  }

  async goto(url) {
    const loaded = new Promise((resolve) => this.s.on("Page.loadEventFired", resolve));
    await this.s.send("Page.navigate", { url });
    await Promise.race([loaded, sleep(15_000)]);
  }

  async reload() {
    const loaded = new Promise((resolve) => this.s.on("Page.loadEventFired", resolve));
    await this.s.send("Page.reload", {});
    await Promise.race([loaded, sleep(15_000)]);
  }

  async eval(expression) {
    const r = await this.s.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(`eval failed: ${r.exceptionDetails.exception?.description ?? expression}`);
    }
    return r.result.value;
  }

  /** Poll a JS expression until it is truthy (bounded). */
  async waitFor(expression, { timeoutMs = 20_000, what = expression } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await this.eval(expression)) return;
      await sleep(100);
    }
    throw new Error(`timed out waiting for: ${what}`);
  }

  async screenshot(path, fs) {
    const { data } = await this.s.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path, Buffer.from(data, "base64"));
  }

  async mouseClick(x, y) {
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await this.s.send("Input.dispatchMouseEvent", {
        type,
        x,
        y,
        button: "left",
        clickCount: type === "mouseMoved" ? 0 : 1,
      });
    }
  }

  async key(key, { code = key, keyCode = 0, text, shift = false } = {}) {
    const modifiers = shift ? 8 : 0;
    await this.s.send("Input.dispatchKeyEvent", {
      type: text ? "keyDown" : "rawKeyDown",
      key,
      code,
      windowsVirtualKeyCode: keyCode,
      text,
      modifiers,
    });
    await this.s.send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key,
      code,
      windowsVirtualKeyCode: keyCode,
      modifiers,
    });
  }

  tab(shift = false) {
    return this.key("Tab", { code: "Tab", keyCode: 9, shift });
  }

  enter() {
    return this.key("Enter", { code: "Enter", keyCode: 13, text: "\r" });
  }

  async typeText(text, { charDelayMs = 0 } = {}) {
    if (!charDelayMs) {
      await this.s.send("Input.insertText", { text });
      return;
    }
    for (const ch of text) {
      await this.s.send("Input.insertText", { text: ch });
      await sleep(charDelayMs);
    }
  }
}
