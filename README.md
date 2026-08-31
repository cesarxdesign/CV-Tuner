# TCV — Tuned CV generator

Paste a job advert or a link to one, get a parse-safe PDF tuned to that job, built only from your verified master CV.

Runs locally on your Mac. **No installs.** Python 3 standard library plus the Chrome you already have.

---

## Setup — once

**Nothing, if you have Claude Code.** TCV tunes by calling the `claude` CLI, which runs on your existing subscription — no API key, no per-CV charge. The startup banner tells you which it's using.

If Claude Code isn't installed: `npm install -g @anthropic-ai/claude-code`, then `claude` once to sign in.

**Only if you'd rather use the API** (it bills per call): open the app and it'll show a box to paste an Anthropic key from [console.anthropic.com](https://console.anthropic.com). It saves to `api_key.txt` itself. You can also `export ANTHROPIC_API_KEY=...` in `~/.zshrc`.

**Which backend, explicitly:** `TCV_BACKEND=cli` or `TCV_BACKEND=api`. Default is `auto` — CLI first, API only if the CLI is missing or errors.

**Model:** `TCV_MODEL=sonnet` to trade quality for speed. Default is `opus`, because this document goes to employers.

**Python:** `python3 --version` should print 3.9+. macOS ships with it.

---

## Running it

**Double-click `TCV.command` on your Desktop.** It starts the server, opens the app, and leaves a Terminal window showing the status. Closing that window stops TCV.

By hand, if you prefer:

```bash
cd ~/Claude/CXD/cv/tcv
python3 server.py
```

then open **http://localhost:8765**. Port busy? `TCV_PORT=8766 python3 server.py`.

---

## Using it

**One box.** Paste the advert into it, or paste a link to the advert. A link on its own gets fetched through a four-rung ladder — the ATS's own structured data (Greenhouse, Lever, SmartRecruiters, Recruitee, Workable APIs, schema.org JobPosting), a plain fetch, headless Chrome rendering the page's JavaScript, and finally Claude fetching it — and every rung's output is validated as a real job advert before it's accepted. If all four fail, the error names what each one saw; paste the text then.

**Cmd-V works anywhere on the page** — you don't have to click into the box first. Pasting over a finished run clears it and starts fresh.

1. Pick **1 page or 2**.
2. **Parse.** Fifteen seconds, runs on a fast model. Confirms the advert was actually read and shows what it captured: title, company, location, seniority, hard requirements. If a link came back as a cookie banner or a login wall, this is where you find out. Optional, but cheap.
3. **Create TCV.** Quality-first, expect **10–20 minutes** with live progress: read the JD into a term bank → write the full account of every role → adversarial keyword-coverage audit (the JD's exact strings, verbatim) → claim-by-claim traceability audit against the master → compress the least relevant roles and expand the most relevant, re-rendering the real PDF between passes, until the content exactly fills the page. Writes the PDF to `~/Desktop/TCV/<company-role>/Cesar Garcia CV.pdf`. The input greys out when it's done; hover it and click the **×** to clear and start over. **Show in Finder** opens the folder.

### The panel on the left after creating

- **Read of the role** — the title and seniority it detected. If this is wrong, everything downstream is wrong. Check it first.
- **Led with** — the angle it chose.
- **Matched terms** — JD vocabulary it found a place for.
- **Compressed or cut** — what got shortened, and why.
- **Requirements you don't meet** — read this one. It is the honest list of what the JD asks for that you can't back. Sometimes it tells you not to apply. More often it tells you what to address in the cover note.

### JSON button

Opens the raw tuned CV for editing, then re-renders. Use it when one bullet is nearly right, or to trim a line that pushed you onto a second page. Faster than re-tuning.

---

## The files

| File | What it is |
|---|---|
| `master_cv.md` | **The only source of facts.** Everything the tuner may say about you lives here. It never invents; it selects. Update this when something changes — a new role, a new metric — and every future TCV picks it up. |
| `tuner_prompt.md` | The rules: parse-safety, no invention, no stuffing, credit discipline, voice. Edit this to change *how* it tunes. |
| `cv_template.html` | Layout and CSS. Edit freely — but read the warning block at the top first. Some rules are load-bearing. |
| `server.py` | The app. |
| `ui.html` | The interface. |
| `~/Desktop/TCV/` | Generated PDFs, one folder per application. The file inside is always `Cesar Garcia CV.pdf`. Set `TCV_OUT` to move it. |

---

## The rules baked in

**Format is 100% optimised for the parser.** Single column, one typeface, real selectable text, standard section headings (`Summary`, `Skills`, `Experience`, `Education`), reverse-chronological, dates inline as `MMM YYYY – MMM YYYY`, no tables, no columns, no text boxes, no page headers or footers, no icons, no graphics, no headshot.

**Content is not.** There is no ATS score to inflate, so keyword stuffing buys nothing and loses the human reading the ranked list. The tuner mirrors the JD's exact vocabulary where an honest equivalent exists, and stops there.

**It cannot invent.** Every sentence traces back to `master_cv.md`. No new numbers, no upgraded titles, no implied reports. Dates are LinkedIn-verified and immutable.

**It won't make excuses.** No justifying tenures, no explaining gaps, no softening exits. Every role ends on strength.

---

## When something breaks

**"No way to tune"** — neither Claude Code nor an API key is available. See Setup.

**"Chrome not found"** — install Google Chrome, or add your browser's path to `CHROME_CANDIDATES` at the top of `server.py`.

**"That page returned almost no text"** — the posting is rendered by JavaScript. Paste the text.

**A model error mentioning the model name** — model IDs change. The app auto-picks the newest available on your key, but you can pin one: `TCV_MODEL=<id> python3 server.py`. `curl` the models endpoint or check the console to see what you have.

**PDF ran to 2 pages on a 1-page budget** — it shouldn't. The design never changes to make content fit: type does not scale, gaps do not stretch. The fit loop rewrites the least relevant roles shorter (fresh rewrites, never truncations) until the real rendered PDF meets the budget, then expands the most relevant roles until the page is full. If it still overflows after eight rounds, the message says so and the honest fix is to cut a bullet via the JSON button.

---

## Keeping it honest

The master CV is the whole system. If it drifts from the truth, every TCV drifts with it — and the CV has to survive a recruiter reading it next to your LinkedIn.

When anything changes, update `master_cv.md` first and LinkedIn second, and keep the dates identical in both.
