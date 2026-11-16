# Account Opening Assistant: recorded demo (A7.6, E12)

An independent prototype built on public HDFC Bank forms; not affiliated with or endorsed by HDFC Bank or the RBI. Synthetic data only.

## The recording
The video is **not in Git**. It is a local file, by owner decision on 2026-11-16. Its checksum identifies the exact recording described here.

| | |
|---|---|
| File | `data/processed/a7/demo/account-opening-assistant-demo.mp4` (gitignored) |
| SHA-256 | `fce8956c9b8e034dfa08fdc167dd5b235e43f69fca8bb229e87097eb381d61d2` |
| Size | 3,212,113 bytes |
| Length | 2 min 31 s (151.3 s) |
| Format | H.264, 1440×900, 25 fps, no audio |
| Recorded | 2026-11-16, branch `feat/account-opening-assistant` at `29ea27e`; the only uncommitted files were evaluation tooling (the browser scripts, the live-run module, the Makefile), no application code |
| Stack | Vite dev server and the real API on the development corpus; **deterministic voice (no live LLM)** |
| Console | no errors or warnings during the recording |

To verify a copy: `sha256sum account-opening-assistant-demo.mp4` (or, on Windows, `Get-FileHash -Algorithm SHA256`).

Key frames are committed in [`a7/demo/`](a7/demo/):
- 1440 px: welcome, the Form 60/61 source, the daughter offer, the India fallback, back to the LLP checklist, the synthetic PAN refused, the NRI checklist;
- 390 px: your answers, and the conversation after a reload.

## What it shows (A7_PLAN.md §7)
1. **Welcome:** the disclaimer and the real-data warning. There is no text box before "I understand — start".
2. **"Hi":** a greeting, then the account question.
3. **Current account → LLP:**
   - the checklist summary;
   - **Checklist** → Form 60/61: the approximate reading, the scanned-text warning, and the source with its page.
4. **"Do I need a copy of the PAN card?":** listed under *could not be confirmed*. There is no PAN-card requirement.
5. **"How can I open an account for my daughter who is six?":**
   - the offer → **Ask about a separate account** → "No, we are in India.";
   - the honest fallback: the corpus has no resident opening form.
6. **"what about the LLP current account?":** **Yes, go back to it**. The LLP checklist returns unchanged (6 items).
7. **Your answers:** progress, Change, and the other account. Re-shot at 390×844.
8. **"my PAN is ABCPE1234F"** (synthetic): refused ("Message not sent") and never echoed.
9. **Reload:** the conversation is restored. Re-shot at 390×844.
10. **Print layout:** the checklist only; the app chrome is hidden.
11. **Start again → "I live in Singapore and want a savings account":** confirm → NRE → Joint → minor No → the NRI checklist.

The plan estimated about 4 minutes. The recorded script takes 2 min 31 s, with a pause on every screen long enough to read it.

**Live voice:** a second take with the live voice was optional (§7) and was **not recorded**. The live LLM was evaluated separately, see [`a7/live_llm_report.md`](a7/live_llm_report.md).

## Recording it yourself
You need Chrome, Node 26, and ffmpeg on `PATH` (or `FFMPEG=<path>`). ffmpeg is a local tool, not a project dependency.

```bash
make up && make migrate
# the API, deterministic (no live LLM for the recording):
cd backend && GROQ_LIVE_ENABLED=false ../.venv/Scripts/python -m uvicorn app.main:app --port 8000 \
    --loop app.core.loop:selector_loop_factory          # or `make api` with live Groq off
make web                                                # http://127.0.0.1:5173
node scripts/browser/a7_browser.mjs demo                # records, encodes, prints the SHA-256
```

How the recording is made:
- **Driver:** `scripts/browser/a7_browser.mjs` drives headless Chrome over the DevTools protocol, using Node's built-in `WebSocket` (no added dependency).
- **Frames:** the page is captured with `Page.startScreencast`.
- **Encoding:** the frames are written with their real timestamps, then ffmpeg encodes them with letterboxing to a constant 1440×900.
- **Output:** the video, the key frames and `demo.json` go to `data/processed/a7/demo/`.
- **Checksum:** a new recording has a different SHA-256. Update the table above only if the new file replaces this one.

## The browser matrix
The same script, run as `node scripts/browser/a7_browser.mjs matrix`, checks the main flow at 1440, 1024, 768 and 390 px. It also runs a keyboard-only flow and a reduced-motion check at each width. Results: [`a7/browser.md`](a7/browser.md).
