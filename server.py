#!/usr/bin/env python3
"""
TCV — Tuned CV generator.

Local web app. Paste a job description (or a URL), get a parse-safe PDF
tuned to that job, built only from the verified master CV.

The tuning is a multi-pass pipeline, quality-first:

    1. analyse   — read the JD, build a literal term bank, rank the roles
    2. account   — write the full, generous account of every role
    3. coverage  — adversarial audit: every claimable JD term, verbatim
    4. trace     — every claim must cite its master CV backing, or it dies
    5. fit       — render the real PDF, compress the least relevant roles
                   (fresh rewrites, never truncations) until it fits, then
                   expand the most relevant until the page is full

The design never changes: type does not scale, gaps do not stretch. The
page is filled by content and only content. --fit stays at 1.0 forever.

Zero third-party dependencies: Python 3 standard library + the Chrome
you already have installed.

    python3 server.py            # then open http://localhost:8765

Tuning runs through the Claude Code CLI (your subscription) when it is
installed; otherwise an Anthropic API key from ANTHROPIC_API_KEY or a
file named api_key.txt next to this script.
"""

import os
import re
import sys
import json
import html
import shutil
import signal
import socket
import string
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote, unquote, urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

# Finished PDFs land on the Desktop, one folder per application, because that
# is where you actually go to attach a file. Override with TCV_OUT=/some/path.
_radar_proc = None  # /api/run-radar single-flight guard

OUT_DIR = os.path.expanduser(
    os.environ.get("TCV_OUT") or os.path.join("~", "Desktop", "TCV")
)
# Scratch: the live preview the UI renders in the iframe, plus the fit loop's
# probe prints. Stays next to the app so it never clutters the Desktop.
WORK_DIR = os.path.join(HERE, ".preview")
# Every CV this tool has made. Survives restarts; lives next to the app, not on
# the Desktop, because it is a log rather than a deliverable.
HISTORY = os.path.join(HERE, "history.json")
PORT = int(os.environ.get("TCV_PORT", "8765"))

# Every generated CV is called this. Uniqueness comes from the folder it
# lands in, never from the filename — the recruiter only ever sees the file.
PDF_FILENAME = "Cesar Garcia CV"

# Bumped whenever the UI starts depending on something new in here. The page
# checks it and tells you to restart rather than failing in a confusing way.
API = 6
API_URL = "https://api.anthropic.com/v1/messages"
MODELS_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"

DEFAULT_MODEL = os.environ.get("TCV_MODEL", "")  # empty = auto-pick newest Opus/Sonnet

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

# The template's geometry, used to convert measured white space into a
# character budget for the fit passes. A4 at Chrome's 96dpi is 1122.5px tall;
# body text is 11px at line-height 1.40; a rendered line holds ~125 chars
# (measured empirically against the real print path, slightly conservative
# so the expand loop lands under the page, never over).
PAGE_PX = 1122.5
LINE_PX = 11 * 1.40
CHARS_PER_LINE = 125
# "Flush" = white space at the bottom no taller than about two text lines.
FLUSH_PX = 2.2 * LINE_PX


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def api_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if k:
        return k
    p = os.path.join(HERE, "api_key.txt")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return ""


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    for name in ("google-chrome", "chromium", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def read(path):
    with open(os.path.join(HERE, path), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# Anthropic API
# --------------------------------------------------------------------------

def api_request(url, payload=None, method="GET"):
    key = api_key()
    if not key:
        raise RuntimeError(
            "No API key. Set ANTHROPIC_API_KEY or create api_key.txt next to server.py."
        )
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", API_VERSION)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Anthropic API {e.code}: {body[:900]}")


def list_models():
    d = api_request(MODELS_URL + "?limit=100")
    return [m["id"] for m in d.get("data", [])]


def pick_model():
    """Choose the strongest available model without hardcoding a name that may
    not exist any more. Prefers Opus, then Sonnet, newest first."""
    if DEFAULT_MODEL:
        return DEFAULT_MODEL
    ids = list_models()
    for family in ("opus", "sonnet"):
        hits = sorted([i for i in ids if family in i.lower()], reverse=True)
        if hits:
            return hits[0]
    if ids:
        return ids[0]
    raise RuntimeError("No models available on this API key.")


class NotJSON(RuntimeError):
    """The model replied, but not with a parseable JSON object."""


def _extract_json(text):
    """Pull the first complete {...} object out of a model's reply."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    if start < 0:
        raise NotJSON("No JSON in the reply.\n" + text[:600])
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise NotJSON("Truncated JSON in the reply.\n" + text[:600])


# ---- model backends: Claude Code CLI (subscription) first, API second -------

CLI_CANDIDATES = [
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.bun/bin/claude",
    "~/.local/bin/claude",
    "~/.volta/bin/claude",
]


def have_cli():
    """Find the claude CLI.

    PATH alone is not enough: when TCV is launched by double-clicking from
    Finder it inherits a minimal PATH that usually excludes npm's global bin,
    so `which claude` comes back empty even though the CLI is installed.
    """
    p = shutil.which("claude")
    if p:
        return p
    for c in CLI_CANDIDATES:
        c = os.path.expanduser(c)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    # nvm installs live under a version directory
    import glob as _glob
    for c in _glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude")):
        if os.access(c, os.X_OK):
            return c
    return None


def _cli_error(p):
    """Turn the CLI's JSON envelope into one readable sentence."""
    detail = ""
    for blob in (p.stdout or "", p.stderr or ""):
        try:
            start = blob.index("{")
            detail = str(json.loads(blob[start:]).get("result") or "")
            if detail:
                break
        except Exception:
            continue
    if not detail:
        detail = (p.stderr or p.stdout or "").strip()[-400:]

    low = detail.lower()
    if "oauth" in low or "authenticate" in low or "401" in low:
        return ("Your Claude Code login has expired.\n\n"
                "Open a Terminal, run `claude`, then `/login` and sign in again. "
                "Come back here and hit Tune.")
    if "rate" in low and "limit" in low:
        return ("Claude Code hit your subscription's rate limit. Wait a few minutes "
                "and try again.\n\n" + detail[:300])
    return "Claude Code CLI failed:\n" + detail[:600]


def oauth_token():
    """A long-lived subscription token (`claude setup-token`), the same
    mechanism GitHub Actions uses. Locally it lives in oauth_token.txt next
    to this script (gitignored), so tuning survives the interactive login
    evaporating from the keychain — which it has done before."""
    t = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if t:
        return t
    p = os.path.join(HERE, "oauth_token.txt")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return ""


def _cli_env():
    """A clean environment for the child CLI. Two failure modes to prevent:
    ANTHROPIC_API_KEY would out-rank the OAuth login and quietly bill per
    call; and when TCV itself was launched from inside a Claude Code session,
    the inherited session vars (ANTHROPIC_BASE_URL, CLAUDECODE, CLAUDE_*)
    make the child CLI act as a nested session with no login of its own.
    CLAUDE_CODE_OAUTH_TOKEN survives — it is how CI (and a local
    oauth_token.txt) authenticate on the subscription."""
    keep = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
    env = {k: v for k, v in os.environ.items()
           if k in keep or not (k.startswith(("ANTHROPIC", "CLAUDE"))
                                or k in ("AI_AGENT", "BAGGAGE"))}
    tok = oauth_token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def _llm_cli(prompt, model, timeout=600, allow_webfetch=False):
    tools = "Bash Edit Write Read WebSearch" + ("" if allow_webfetch else " WebFetch")
    cmd = [have_cli(), "-p", "--output-format", "json", "--model", model,
           "--disallowed-tools", tools]
    if allow_webfetch:
        cmd += ["--allowed-tools", "WebFetch"]
    p = subprocess.run(cmd, input=prompt, capture_output=True,
                       text=True, timeout=timeout, env=_cli_env())
    if p.returncode != 0:
        raise RuntimeError(_cli_error(p))
    try:
        env = json.loads(p.stdout)
    except Exception:
        raise RuntimeError("Unexpected CLI output:\n" + p.stdout[-800:])
    if env.get("is_error"):
        class _P:
            stdout, stderr = json.dumps(env), ""
        raise RuntimeError(_cli_error(_P))
    # modelUsage lists every model the CLI touched; the one that actually wrote
    # the answer is the one with the most output tokens.
    usage = env.get("modelUsage") or {}
    main = max(usage.items(), key=lambda kv: (kv[1] or {}).get("outputTokens", 0))[0] if usage else model
    return env.get("result") or "", f"{main} (subscription)"


def _llm_api(prompt, model, max_tokens=8192):
    model = model if model and "-" in model else pick_model()
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = api_request(API_URL, payload, method="POST")
    text = "".join(b.get("text", "") for b in resp.get("content", []))
    return text, model + " (API)"


def llm_json(prompt, model=None, timeout=600):
    """One JSON-returning model call. Prefer the Claude Code CLI — it bills to
    the subscription, not per call. No silent fallback to the paid API when
    the CLI is present but broken — that hides the real error behind a billing
    one. Fix the CLI instead.

    A reply that isn't one JSON object earns exactly one 'JSON only' re-ask:
    models sometimes narrate their changes before (or instead of) the object,
    and a 15-minute pipeline must not die on politeness."""
    backend = (os.environ.get("TCV_BACKEND") or "auto").lower()

    def once(p):
        if backend != "api" and have_cli():
            text, used = _llm_cli(p, model or os.environ.get("TCV_MODEL") or "opus",
                                  timeout)
        else:
            if not api_key():
                raise RuntimeError(
                    "Can't find the Claude Code CLI, and there's no API key either.\n\n"
                    "Claude Code is the free path — it runs on your subscription. Install "
                    "it with `npm i -g @anthropic-ai/claude-code`, run `claude` once to "
                    "sign in, then restart TCV."
                )
            text, used = _llm_api(p, model)
        return _extract_json(text), used

    try:
        return once(prompt)
    except NotJSON as e:
        return once(prompt +
                    "\n\n<FORMAT_REJECTION>\nYour previous reply was rejected: " +
                    str(e).split("\n")[0] + " Reply again with ONLY the JSON "
                    "object. No prose, no explanation of changes, no markdown "
                    "fence. The first character of your reply must be { and "
                    "the last must be }.\n</FORMAT_REJECTION>")


# --------------------------------------------------------------------------
# JD parsing (the quick structured read; also the fetch ladder's gate)
# --------------------------------------------------------------------------

PARSE_PROMPT = """You are reading a job advert so a designer can check the right text was captured before generating a CV against it.

Return ONE JSON object and nothing else. No prose, no markdown fence.

{
  "company": "hiring company, exactly as the advert writes it, or \"\" if never named",
  "job_title": "the role title as written",
  "location": "location and remote policy, or \"\" ",
  "seniority": "one short phrase, e.g. Senior IC, Staff IC, Head of / manager",
  "domain": "one short phrase, e.g. crypto payments, B2B SaaS, consumer fintech",
  "salary": "the pay as the advert states it, with currency, range and period, e.g. \"EUR 75,000 - 95,000 / year\" or \"GBP 450 / day\". Include equity only if a number is given. \"\" if the advert does not state pay.",
  "remote": "exactly one of: Remote, Hybrid, On-site. \"\" if the advert never says.",
  "country": "the country or countries the role can be done from, e.g. \"Germany\", \"Portugal\", \"Anywhere in the EU\", \"US, East Coast hours\". If it names a city, give the country. \"\" if the advert never says.",
  "portugal": "Can this job be done from Portugal? \"Yes\", \"No\", or \"\" if the advert says nothing about where the person must be. Yes when Portugal is named; when the role is open to the EU, Europe, EMEA or anywhere in the world; or when the only constraint is a timezone Portugal sits in (WET, GMT, CET, CET plus or minus 2). No when the advert names countries, regions or timezones Portugal is not part of, e.g. UK only, Spain and Poland, US East Coast hours, Germany.",
  "europe": "Is the role open across Europe or the EU broadly, rather than one or two named countries? \"Yes\", \"No\", or \"\" if the advert says nothing.",
  "years_experience": "the minimum years of experience asked for, as a short string, e.g. \"6+\", \"5-8\", \"3\". \"\" if the advert gives no number.",
  "must_haves": ["the hard requirements, at most 6, each under 12 words"],
  "looks_wrong": "If the text does not read like a job advert at all (a cookie banner, a login wall, a search results page, an empty fetch), say what it looks like instead. Otherwise the empty string."
}

Rules for the fact fields (salary, remote, country, portugal, europe, years_experience):
- Report only what the advert actually says. Never infer, never estimate, never fill a gap with what is typical for the role or market.
- If it is not in the text, the value is the empty string. An empty field is a correct answer and is more useful than a guess.
- Quote the advert's own numbers and currency. Do not convert, annualise or round.

The advert follows.
"""


def parse_jd(jd_text):
    """A quick structured read of the advert. Fast model: this is a sanity check,
    not the tuning step."""
    prompt = PARSE_PROMPT + "\n\n" + jd_text[:20000]
    backend = (os.environ.get("TCV_BACKEND") or "auto").lower()
    if backend != "api" and have_cli():
        text, _ = _llm_cli(prompt, os.environ.get("TCV_PARSE_MODEL") or "haiku",
                           timeout=180)
        return _extract_json(text)
    text, _ = _llm_api(prompt, os.environ.get("TCV_PARSE_MODEL") or None,
                       max_tokens=1200)
    return _extract_json(text)


# --------------------------------------------------------------------------
# JD fetching — the ladder. A link has to work. Four rungs, each validated:
#   1. the ATS's own structured data (public JSON APIs, JSON-LD JobPosting)
#   2. a plain fetch of the page, stripped
#   3. headless Chrome rendering the page's JavaScript, then stripped
#   4. Claude fetching the page itself (WebFetch, on the subscription)
# A rung's output only counts if it reads like a job advert (the parse gate's
# looks_wrong check), so a cookie wall can never become the JD a CV is tuned
# against. Either real advert text comes through, or the error names every
# rung and what it saw.
# --------------------------------------------------------------------------

URLISH = re.compile(r"^(https?://\S+|[\w-]+(\.[\w-]+)+(/\S*)?)$", re.I)


def split_input(raw):
    """One box, two kinds of input. A single token that looks like an address is
    a URL; anything else is the advert text."""
    raw = (raw or "").strip()
    if raw and len(raw.split()) == 1 and "\n" not in raw and URLISH.match(raw):
        return "", raw if raw.lower().startswith("http") else "https://" + raw
    return raw, ""


def _http_get(url, timeout=45):
    req = urllib.request.Request(url)
    req.add_header("User-Agent",
                   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    req.add_header("Accept-Language", "en")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def _strip_html(raw):
    raw = re.sub(r"(?is)<(script|style|nav|footer|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", raw)
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t\xa0]+", " ", txt)
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
    txt = "\n".join(l.strip() for l in txt.splitlines())
    return txt.strip()[:60000]


def _jsonld_jobposting(raw_html):
    """Most job pages embed a schema.org JobPosting. It is the advert's own
    words with none of the page furniture — the best source there is."""
    for m in re.finditer(r'(?is)<script[^>]*ld\+json[^>]*>(.*?)</script>', raw_html):
        blob = m.group(1).strip()
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
                continue
            if not isinstance(x, dict):
                continue
            t = x.get("@type")
            types = [t] if isinstance(t, str) else (t or [])
            if "JobPosting" in types:
                return x
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    return None


def _jobposting_text(jp):
    org = jp.get("hiringOrganization") or {}
    org = org.get("name", "") if isinstance(org, dict) else str(org)
    loc = ""
    jl = jp.get("jobLocation")
    if isinstance(jl, list) and jl:
        jl = jl[0]
    if isinstance(jl, dict):
        addr = jl.get("address") or {}
        if isinstance(addr, dict):
            loc = ", ".join(str(addr.get(k, "")) for k in
                            ("addressLocality", "addressRegion", "addressCountry")
                            if addr.get(k))
    parts = [jp.get("title", ""), org, loc,
             str(jp.get("employmentType", "") or ""),
             _strip_html(str(jp.get("description", "")))]
    return "\n".join(p for p in parts if p).strip()


def _ats_api_text(url):
    """Known ATS hosts publish the posting as public JSON. Ask for that
    instead of scraping their JavaScript shell."""
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path

    # greenhouse EU boards have no public API host — their pages fetch fine
    # statically, so only the US host gets the API shortcut.
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io$", host)
    if m:
        gm = re.search(r"^/(?:embed/job_app.*|([^/]+)/jobs/(\d+))", path)
        if gm and gm.group(1):
            d = json.loads(_http_get(
                f"https://boards-api.greenhouse.io/v1/boards/{gm.group(1)}/jobs/{gm.group(2)}"))
            body = _strip_html(html.unescape(d.get("content", "")))
            loc = (d.get("location") or {}).get("name", "")
            return "\n".join(x for x in (d.get("title", ""), gm.group(1), loc, body) if x)

    if host == "jobs.lever.co":
        lm = re.search(r"^/([^/]+)/([0-9a-f-]{20,})", path)
        if lm:
            d = json.loads(_http_get(
                f"https://api.lever.co/v0/postings/{lm.group(1)}/{lm.group(2)}"))
            parts = [d.get("text", ""), lm.group(1),
                     (d.get("categories") or {}).get("location", ""),
                     d.get("descriptionPlain") or _strip_html(d.get("description", ""))]
            for lst in d.get("lists") or []:
                parts.append(lst.get("text", ""))
                parts.append(_strip_html(lst.get("content", "")))
            parts.append(d.get("additionalPlain") or "")
            return "\n".join(p for p in parts if p)

    if host == "jobs.smartrecruiters.com":
        sm = re.search(r"^/([^/]+)/(\d{15,})", path)
        if sm:
            d = json.loads(_http_get(
                f"https://api.smartrecruiters.com/v1/companies/{sm.group(1)}/postings/{sm.group(2)}"))
            parts = [d.get("name", ""), sm.group(1),
                     (d.get("location") or {}).get("city", "")]
            for sec in ((d.get("jobAd") or {}).get("sections") or {}).values():
                if isinstance(sec, dict):
                    parts.append(sec.get("title", ""))
                    parts.append(_strip_html(sec.get("text", "")))
            return "\n".join(p for p in parts if p)

    rm = re.match(r"^([\w-]+)\.recruitee\.com$", host)
    if rm:
        om = re.search(r"^/o/([^/]+)", path)
        if om:
            d = json.loads(_http_get(
                f"https://{rm.group(1)}.recruitee.com/api/offers/{om.group(1)}"))
            offer = d.get("offer") or d
            return "\n".join(x for x in (
                offer.get("title", ""), offer.get("company_name", ""),
                offer.get("location", ""),
                _strip_html(offer.get("description", "")),
                _strip_html(offer.get("requirements", ""))) if x)

    if host == "apply.workable.com":
        wm = re.search(r"^/([^/]+)/j/([^/]+)", path)
        if wm:
            d = json.loads(_http_get(
                f"https://apply.workable.com/api/v2/accounts/{wm.group(1)}/jobs/{wm.group(2)}"))
            parts = [d.get("title", ""), wm.group(1),
                     _strip_html(d.get("description", "")),
                     _strip_html(d.get("requirements", "")),
                     _strip_html(d.get("benefits", ""))]
            return "\n".join(p for p in parts if p)

    raise RuntimeError("not a known ATS URL shape")


def _static_text(url):
    raw = _http_get(url)
    jp = _jsonld_jobposting(raw)
    if jp:
        txt = _jobposting_text(jp)
        if len(txt) >= 250:
            return txt
    return _strip_html(raw)


def _chrome_text(url):
    """Render the page's JavaScript with headless Chrome and read the DOM.
    Same disease as printing: on some macOS builds Chrome writes everything
    and then never exits. Same medicine: watch the output file, not the
    process, and take the whole group down once the dump is complete."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("Chrome not found")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "dom.html")
        argv = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                f"--user-data-dir={os.path.join(tmp, 'profile')}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--mute-audio",
                "--window-size=1280,3000",
                "--virtual-time-budget=15000", "--timeout=30000",
                "--dump-dom", url]
        with open(out, "w", encoding="utf-8") as f:
            p = subprocess.Popen(argv, stdout=f, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        t0, size, stable = time.time(), -1, 0
        try:
            while True:
                exited = p.poll() is not None
                try:
                    cur = os.path.getsize(out)
                except OSError:
                    cur = -1
                if cur > 500 and cur == size:
                    stable += 1
                    if stable >= 4 or exited:   # ~1s with no growth = done
                        break
                else:
                    stable = 0
                size = cur
                if exited:
                    break
                if time.time() - t0 > 45:
                    break
                time.sleep(0.25)
        finally:
            _kill_group(p)
        with open(out, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    if len(raw) < 500:
        raise RuntimeError("Chrome returned no DOM")
    jp = _jsonld_jobposting(raw)
    if jp:
        txt = _jobposting_text(jp)
        if len(txt) >= 250:
            return txt
    return _strip_html(raw)


def _claude_text(url):
    if not have_cli():
        raise RuntimeError("no Claude Code CLI for the WebFetch rung")
    prompt = (
        "Fetch this URL and return the complete job advert text it contains, "
        "verbatim, as plain text: title, company, location, and the full "
        "description. Return ONLY the advert text, no commentary. If the page "
        "is not a job advert (login wall, error page, listing index), return "
        "exactly: FAILED: <one line saying what the page is>\n\n" + url)
    text, _ = _llm_cli(prompt, os.environ.get("TCV_PARSE_MODEL") or "haiku",
                       timeout=240, allow_webfetch=True)
    text = (text or "").strip()
    if text.startswith("FAILED:") or len(text) < 250:
        raise RuntimeError(text[:300] or "empty reply")
    return text[:60000]


def fetch_jd_ladder(url, progress=None):
    """Turn a link into validated advert text, or explain exactly why not.
    Returns (jd_text, parse_gate_dict, rung_name)."""
    say = progress or (lambda *_: None)
    attempts = []
    rungs = (("structured", _ats_api_text), ("fetch", _static_text),
             ("chrome", _chrome_text), ("claude", _claude_text))
    for name, fn in rungs:
        say(f"fetching the link ({name})…")
        try:
            txt = fn(url)
        except Exception as e:
            attempts.append(f"{name}: {str(e)[:160]}")
            continue
        if len(txt or "") < 250:
            attempts.append(f"{name}: only {len(txt or '')} characters came back")
            continue
        try:
            gate = parse_jd(txt)
        except Exception as e:
            attempts.append(f"{name}: gate check failed ({str(e)[:120]})")
            continue
        if gate.get("looks_wrong"):
            attempts.append(f"{name}: {gate['looks_wrong'][:160]}")
            continue
        say(f"link resolved via {name}: {len(txt):,} characters")
        return txt, gate, name
    raise RuntimeError(
        "Couldn't get a job advert out of that link. Tried every method:\n- "
        + "\n- ".join(attempts) +
        "\n\nOpen the posting, select all, and paste the text instead.")


# --------------------------------------------------------------------------
# the no-invention verifier (mechanical)
# --------------------------------------------------------------------------

def _walk_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _walk_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_strings(v)

_MONTH_TOKEN = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)?\s?\d{4}$")

def verify_doc(doc):
    """Mechanical no-invention gate. Every metric-looking number and every
    management claim in the tuned CV must exist in MASTER_CV. Returns a list
    of violation strings; empty means clean."""
    master = read("master_cv.md")
    mnorm = re.sub(r"[\s,]", "", master.lower()).replace("->", "→")
    text = " ".join(s for s in _walk_strings(doc) if isinstance(s, str))
    bad = []
    # suffix must be ATTACHED to the digits ("200M+", "$6.9M") - a space
    # before it would slurp the next word's first letter out of date rows
    # ("Mar 2020, May" -> "2020, M")
    for m in re.finditer(
            r"\$?\d(?:[\d.,]*\d)?[%MKBmkb]?\+?(?:\s?(?:→|->|to)\s?\$?\d(?:[\d.,]*\d)?[%MKBmkb]?\+?)?",
            text):
        tok = m.group(0).strip().rstrip(".")
        if _MONTH_TOKEN.match(tok) or len(tok) < 2:
            continue  # bare years / dates are chronology, not metrics
        if not re.search(r"[%MKB$→]|->|\+", tok, re.I) and tok.isdigit():
            continue  # plain small integers ("three teams" style numerals)
        norm = re.sub(r"[\s,]", "", tok.lower()).replace("->", "→")
        if norm not in mnorm:
            bad.append(f"number not in MASTER_CV: \"{tok}\"")
    for m in re.finditer(
            r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|zero)"
            r"\s+direct reports?\b", text, re.I):
        claim = m.group(0)
        if re.sub(r"\s", "", claim.lower()) not in re.sub(r"\s", "", master.lower()):
            bad.append(f"report-count claim not in MASTER_CV: \"{claim}\"")
    for m in re.finditer(r"\bmanag(?:e|ed|es|ing|er|ers)\b|\bdirect reports?\b",
                         text, re.I):
        phrase = m.group(0)
        if re.sub(r"\s", "", phrase.lower()) not in re.sub(r"\s", "", master.lower()):
            bad.append(f"management claim not in MASTER_CV: \"{phrase}\" — "
                       "rephrase with MASTER_CV's own verbs (led, ran, leadership)")
    seen = set()
    return [b for b in bad if not (b in seen or seen.add(b))]


# --------------------------------------------------------------------------
# coverage — the ATS-term scoreboard (mechanical, verbatim)
# --------------------------------------------------------------------------

def doc_text(doc):
    """The CV's actual words: everything except tuning notes and private keys,
    so the scoreboard can't be gamed by listing terms in the notes."""
    d = {k: v for k, v in doc.items()
         if k != "tuning_notes" and not str(k).startswith("_")}
    return " ".join(_walk_strings(d))


def term_hits(terms, text):
    """Which of the JD's literal strings appear in the text. Case-insensitive
    (ATS search is), but the string itself must match verbatim: 'UX/UI' and
    'UI/UX' are different terms. Whitespace is flexible so a line wrap never
    hides a match."""
    hits = []
    for t in terms:
        t = (t or "").strip()
        if not t:
            continue
        pat = re.escape(t)
        pat = re.sub(r"(?:\\\s)+", r"\\s+", pat)
        if re.search(r"(?<![A-Za-z0-9])" + pat + r"(?![A-Za-z0-9])",
                     text, re.I):
            hits.append(t)
    return hits


def baseline_text():
    p = os.path.join(HERE, "baseline_cv.txt")
    if os.path.exists(p):
        return read("baseline_cv.txt")
    return ""


# --------------------------------------------------------------------------
# the tuning pipeline
# --------------------------------------------------------------------------

DOC_SCHEMA = """
Return ONE JSON object and absolutely nothing else — no prose, no markdown fence,
no explanation before or after, never a description of what you changed. The
first character of your reply is { and the last is }. It must match this shape
exactly:

{
  "headline": "string",
  "summary": ["string"],
  "skills": [{"label": "string", "items": ["string"]}],
  "experience": [{"title": "string", "company": "string", "qualifier": "string",
                  "dates": "MMM YYYY - MMM YYYY", "bullets": ["string"]}],
  "education": [{"institution": "string", "detail": "string", "dates": "string"}],
  "tuning_notes": {"job_title": "string", "company": "string", "seniority": "string",
                   "matched_terms": ["string"], "led_with": "string",
                   "compressed_or_cut": ["string"], "unmet_requirements": ["string"]}
}

Every field is required. Use "" for a qualifier that does not apply.
"""

ANALYZE_SCHEMA = """
Return ONE JSON object and absolutely nothing else:

{
  "company": "hiring company as the JD writes it, \\"\\" only if truly never named",
  "job_title": "the role title as written",
  "seniority": "one short phrase",
  "domain": "one short phrase",
  "stage": "pre-seed/seed | Series A-B scale-up | growth | enterprise | unknown",
  "ic_or_management": "IC" or "management",
  "led_with": "one sentence: what this CV should lead with and why",
  "must_haves": ["hard requirements, each under 12 words"],
  "unmet_requirements": ["JD requirements Cesar does NOT meet. Be honest."],
  "term_bank": [
    {"term": "the JD's literal string, exact capitalisation and punctuation",
     "claimable": true,
     "evidence": "where MASTER_CV backs it (section or quote), \\"\\" when claimable is false"}
  ],
  "role_ranking": ["every role key, most relevant to this JD first"]
}

term_bank rules:
- 15 to 30 terms: the skills, methods, tools, domains, platforms and phrases a
  recruiter would actually search for. Capture the JD's OWN spelling: if it
  says "UX/UI" the term is "UX/UI", not "UI/UX"; "0-1" not "0→1".
- claimable=true ONLY when MASTER_CV genuinely backs the capability (possibly
  under a different label — the JD's string then becomes the spelling used).
- claimable=false for anything Cesar cannot honestly claim. These never go on
  the CV; they exist so the shortfall is visible.
role_ranking uses exactly these keys, all nine, ordered for THIS JD:
["Confirmo", "DONE", "Trust Wallet", "BLKBOX.ai", "Mara", "Cable", "Penfold",
 "Starcount", "Earlier"]
"""

CRITIC_SCHEMA = """
Return ONE JSON object and absolutely nothing else:

{
  "missing_claimable": [
    {"term": "a claimable term-bank string absent from the CV",
     "where": "which role or section should honestly carry it",
     "evidence": "the MASTER_CV backing"}
  ],
  "not_verbatim": [
    {"jd_term": "the JD's exact string", "cv_says": "the near-miss wording on the CV"}
  ],
  "wasted": ["sentences spending space on things this JD never asks about, quoted"],
  "verdict": "revise" or "done"
}

You are the recruiter running keyword searches over the parsed CV. A term only
counts when it appears VERBATIM (case-insensitive, but the string itself exact).
"done" only when nothing actionable remains.
"""

TRACE_SCHEMA = """
Return ONE JSON object and absolutely nothing else:

{
  "violations": [
    {"quote": "the CV sentence or phrase", "reason": "why MASTER_CV does not back it"}
  ]
}

Go claim by claim through the CV: every skill, tool, domain, method, outcome,
scope and title. For each, find the MASTER_CV passage that backs it. Rephrasing
and the JD's spelling of a backed capability are fine. A capability, fact or
implication with NO backing anywhere in MASTER_CV is a violation — however well
it matches the JD. An empty violations list means the CV is fully traceable.

Deliberate silently. The violations array carries ONLY your final verdicts:
claims you conclusively judge unbacked. A candidate you examined and found
backed, or a defensible rephrase, is NOT a violation and must not appear at
all — never emit an entry whose reason talks itself into withdrawing.
"""


def _ctx(jd_text, budget):
    return ("<MASTER_CV>\n" + read("master_cv.md") + "\n</MASTER_CV>\n\n"
            "<JOB_DESCRIPTION>\n" + jd_text.strip() + "\n</JOB_DESCRIPTION>\n\n"
            f"<PAGE_BUDGET>{budget}</PAGE_BUDGET>\n")


def _prompt(jd_text, budget, pass_instructions, extra=""):
    return (read("tuner_prompt.md") + "\n\n---\n\n" + _ctx(jd_text, budget)
            + "\n" + extra + "\n" + pass_instructions)


def _doc_pass(name, prompt, model, progress):
    """A pass that emits the full CV JSON. The mechanical no-invention verifier
    runs on every one; a violation earns exactly one corrective retry."""
    doc, used = llm_json(prompt, model)
    v = verify_doc(doc)
    if v:
        progress(f"{name}: fact verifier rejected it ({len(v)} violation"
                 f"{'s' if len(v) != 1 else ''}), redoing the pass")
        doc, used = llm_json(
            prompt + "\n\n<REJECTED_PREVIOUS_ATTEMPT>\nYour previous output "
            "failed the mechanical no-invention verifier:\n- " + "\n- ".join(v) +
            "\nEvery number and every management word must exist in MASTER_CV. "
            "Redo the pass.\n</REJECTED_PREVIOUS_ATTEMPT>", model)
        v = verify_doc(doc)
        if v:
            raise RuntimeError(f"{name} failed the no-invention verifier twice:\n- "
                               + "\n- ".join(v))
    return doc, used


def _bullet_chars(doc):
    return sum(len(b) for r in doc.get("experience", []) for b in r.get("bullets", []))


def tune_pipeline(jd_text, budget, model=None, progress=None):
    """The whole flow: analyse → account → coverage → trace → fit-to-full-page.
    Returns (doc, meta) where meta carries coverage and fit measurements."""
    say = progress or (lambda *_: None)
    budget = int(budget or 1)

    # ---- 1 · analyse the JD ------------------------------------------------
    say("reading the JD: term bank, requirements, role ranking…")
    analysis, used = llm_json(_prompt(jd_text, budget, ANALYZE_SCHEMA,
        extra="PASS: ANALYSE. Read the JOB_DESCRIPTION against MASTER_CV and "
              "return the analysis object below. The term bank is the "
              "foundation of everything downstream: capture the JD's literal "
              "strings.\n"), model)
    terms_all = [t.get("term", "") for t in analysis.get("term_bank", [])]
    claimable = [t.get("term", "") for t in analysis.get("term_bank", [])
                 if t.get("claimable")]
    unclaimable = [t for t in terms_all if t not in claimable]
    ranking = analysis.get("role_ranking") or []
    say(f"JD read: {analysis.get('job_title') or '?'}"
        + (f" at {analysis.get('company')}" if analysis.get("company") else "")
        + f" · {len(terms_all)} terms in the bank, {len(claimable)} claimable")

    bank_json = json.dumps(analysis, ensure_ascii=False)

    # ---- 2 · the full account ---------------------------------------------
    say("writing the full account of every role…")
    doc, used = _doc_pass("full account", _prompt(jd_text, budget,
        "PASS: FULL ACCOUNT. Write the complete tuned CV, generously: every "
        "role at its best full length for THIS JD, drawing on the master's "
        "bullet variants and atomic claims but written fresh against the "
        "analysis below. A truly full page holds about 4,300 bullet characters "
        "at one page; aim for 4,800-5,800 total so the server compresses from "
        "above — err long, never thin. Weave every "
        "claimable term-bank string in VERBATIM where it is honest. Order: "
        "reverse chronological, DONE second, Earlier last.\n" + DOC_SCHEMA,
        extra="<JD_ANALYSIS>\n" + bank_json + "\n</JD_ANALYSIS>\n"), model, say)
    say(f"full account written: {_bullet_chars(doc):,} bullet characters")

    # ---- 3 · coverage audit ------------------------------------------------
    for round_no in (1, 2, 3):
        say(f"coverage audit, round {round_no}…")
        critic, _ = llm_json(_prompt(jd_text, budget, CRITIC_SCHEMA,
            extra="<JD_ANALYSIS>\n" + bank_json + "\n</JD_ANALYSIS>\n"
                  "<CURRENT_CV>\n" + json.dumps(doc, ensure_ascii=False) +
                  "\n</CURRENT_CV>\n"), model)
        findings = (critic.get("missing_claimable") or []) + \
                   (critic.get("not_verbatim") or [])
        if critic.get("verdict") == "done" or not (
                findings or critic.get("wasted")):
            say("coverage audit: clean")
            break
        say(f"coverage audit: {len(critic.get('missing_claimable') or [])} missing, "
            f"{len(critic.get('not_verbatim') or [])} not verbatim, "
            f"{len(critic.get('wasted') or [])} wasted — revising")
        doc, used = _doc_pass("coverage revision", _prompt(jd_text, budget,
            "PASS: COVERAGE REVISION. Apply the critic's findings to the CV "
            "below and return the full corrected CV. Add each missing claimable "
            "term verbatim where the critic says it honestly belongs; fix every "
            "near-miss to the JD's exact string; rewrite or cut the wasted "
            "sentences in favour of what this JD asks about. Change nothing "
            "else.\n" + DOC_SCHEMA,
            extra="<JD_ANALYSIS>\n" + bank_json + "\n</JD_ANALYSIS>\n"
                  "<CURRENT_CV>\n" + json.dumps(doc, ensure_ascii=False) +
                  "\n</CURRENT_CV>\n<CRITIC_FINDINGS>\n" +
                  json.dumps(critic, ensure_ascii=False) +
                  "\n</CRITIC_FINDINGS>\n"), model, say)

    # ---- 4 · traceability audit -------------------------------------------
    say("traceability audit: every claim against the master…")
    for attempt in (1, 2, 3):
        trace, _ = llm_json(_prompt(jd_text, budget, TRACE_SCHEMA,
            extra="<CURRENT_CV>\n" + json.dumps(doc, ensure_ascii=False) +
                  "\n</CURRENT_CV>\n"), model)
        viols = trace.get("violations") or []
        if not viols:
            say("traceability: every claim traces to the master")
            break
        if attempt == 3:
            raise RuntimeError(
                "Traceability audit still failing after two fix passes:\n- " +
                "\n- ".join(f"{v.get('quote','')} — {v.get('reason','')}"
                            for v in viols[:8]))
        say(f"traceability: {len(viols)} unbacked claim"
            f"{'s' if len(viols) != 1 else ''} — striking")
        doc, used = _doc_pass("traceability fix", _prompt(jd_text, budget,
            "PASS: TRACEABILITY FIX. The audit found claims MASTER_CV does not "
            "back. Remove or correct each one — replace it with the nearest "
            "claim the master DOES back, or cut it. Never paper over a "
            "violation with a synonym. Return the full corrected CV.\n"
            + DOC_SCHEMA,
            extra="<CURRENT_CV>\n" + json.dumps(doc, ensure_ascii=False) +
                  "\n</CURRENT_CV>\n<VIOLATIONS>\n" +
                  json.dumps(viols, ensure_ascii=False) + "\n</VIOLATIONS>\n"),
            model, say)

    protected = term_hits(claimable, doc_text(doc))

    # ---- 5 · fit: content only, real renders ------------------------------
    fit_extra = ("<JD_ANALYSIS>\n" + bank_json + "\n</JD_ANALYSIS>\n"
                 "<PROTECTED_TERMS>\nThe matched-term set must not shrink. "
                 "These JD strings are on the CV now and must still be on it, "
                 "verbatim, after your rewrite:\n" +
                 json.dumps(protected, ensure_ascii=False) +
                 "\n</PROTECTED_TERMS>\n")

    def _fit_doc_pass(name, instructions):
        d, _ = _doc_pass(name, _prompt(jd_text, budget, instructions + DOC_SCHEMA,
            extra=fit_extra + "<CURRENT_CV>\n" +
                  json.dumps(doc, ensure_ascii=False) + "\n</CURRENT_CV>\n"),
            model, say)
        lost = [t for t in protected if t not in term_hits(protected, doc_text(d))]
        if lost:
            say(f"{name}: dropped matched terms {lost} — one retry to re-home them")
            d, _ = _doc_pass(name, _prompt(jd_text, budget, instructions +
                "\nYour previous rewrite LOST these matched JD terms: " +
                json.dumps(lost, ensure_ascii=False) +
                ". Re-home every one of them verbatim in a surviving sentence.\n"
                + DOC_SCHEMA,
                extra=fit_extra + "<CURRENT_CV>\n" +
                      json.dumps(doc, ensure_ascii=False) + "\n</CURRENT_CV>\n"),
                model, say)
        return d

    ranking_json = json.dumps(ranking, ensure_ascii=False)
    pages = slack = None
    for it in range(8):
        pages = probe_pages(doc)
        if pages <= budget:
            break
        tail = probe_slack(doc, pages)
        over_px = (pages - budget) * PAGE_PX - tail
        cut = max(180, int(over_px / LINE_PX * CHARS_PER_LINE * 1.15))
        say(f"fit: {pages} pages, ~{cut} characters over — compressing the "
            f"least relevant roles (round {it + 1})")
        doc = _fit_doc_pass("compress",
            "PASS: COMPRESS. The rendered CV is over the page budget by about "
            f"{cut} characters of bullet text. Shorten the LEAST relevant "
            "roles first, per this ranking (most relevant first): "
            + ranking_json + ". Take a role one level shorter at a time "
            "(~700 → ~550 → ~400 → ~250 → ~120 characters); spread the cut "
            "across the bottom of the ranking rather than gutting one role. "
            "Each shortened role is a FRESH rewrite: the best content at that "
            "length for this JD, never a truncation. Never drop a role, its "
            "dates, education, or a summary/skills line entirely. Leave the "
            "most relevant roles untouched. Return the full CV.\n")
    else:
        pages = probe_pages(doc)
        if pages > budget:
            say("fit: still over budget after 8 compression rounds — shipping "
                "the smallest version, flagged as overflow")

    if pages is not None and pages <= budget:
        for it in range(4):
            slack = probe_slack(doc, budget)
            filled = 100 * (1 - slack / PAGE_PX)
            say(f"fit: fits {budget} page{'s' if budget > 1 else ''}, "
                f"last page {filled:.0f}% full")
            if slack <= FLUSH_PX:
                break
            add = int((slack - LINE_PX) / LINE_PX * CHARS_PER_LINE)
            if add < 60:
                break
            say(f"fit: room for ~{add} more characters — expanding the most "
                f"relevant roles (round {it + 1})")
            cand = _fit_doc_pass("expand",
                "PASS: EXPAND. The rendered CV fits the page with about "
                f"{add} characters of white space left at the bottom. Add that "
                "much bullet text to the MOST relevant roles, per this ranking "
                "(most relevant first): " + ranking_json + ". Use master "
                "material not yet on the CV (atomic claims, XL variant detail) "
                "chosen for THIS JD. Never repeat a fact already on the CV, "
                "never pad with adjectives, never touch the least relevant "
                "roles. If the master has nothing honest left to add, add "
                "less. Return the full CV.\n")
            if probe_pages(cand) > budget:
                say("fit: expansion overflowed the page — keeping the previous version")
                break
            doc = cand
        slack = probe_slack(doc, budget)

    # ---- 6 · scoreboard ----------------------------------------------------
    final_text = doc_text(doc)
    tuned_hits = term_hits(terms_all, final_text)
    base = baseline_text()
    base_hits = term_hits(terms_all, base) if base else []
    lost_protected = [t for t in protected if t not in tuned_hits]

    notes = doc.setdefault("tuning_notes", {})
    notes["job_title"] = notes.get("job_title") or analysis.get("job_title", "")
    notes["company"] = analysis.get("company", "") or notes.get("company", "")
    notes["seniority"] = notes.get("seniority") or analysis.get("seniority", "")
    notes["led_with"] = notes.get("led_with") or analysis.get("led_with", "")
    notes["matched_terms"] = tuned_hits
    unmet = list(dict.fromkeys((analysis.get("unmet_requirements") or []) +
                               (notes.get("unmet_requirements") or [])))
    notes["unmet_requirements"] = unmet

    meta = {
        "coverage": {
            "total": len(terms_all),
            "tuned": len(tuned_hits),
            "baseline": len(base_hits) if base else None,
            "matched": tuned_hits,
            "missing_unclaimable": unclaimable,
            "missing_claimable": [t for t in claimable if t not in tuned_hits],
            "lost_in_fit": lost_protected,
        },
        "pages": pages,
        "slack_px": slack,
        "filled": round(1 - (slack / PAGE_PX), 3) if slack is not None else None,
        "model": used,
    }
    doc["_model"] = used
    return doc, meta


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

E = lambda s: html.escape(str(s or ""), quote=False)


# Roles with a live case study on cesarxdesign.com get the ↗ mark, exactly as
# the BCV does. Trust Wallet is deliberately absent — teaser card, no page.
PORTFOLIO = {"confirmo", "done", "done workouts", "mara", "cable", "cable tech",
             "penfold", "starcount"}


def render_html(d, fit=1.0, filler=0):
    tpl = read("cv_template.html")

    summary = "".join(f"<p>{E(p)}</p>" for p in d.get("summary", []))

    skills = ""
    for line in d.get("skills", []):
        items = ", ".join(E(i) for i in line.get("items", []))
        skills += (f'<div class="skill"><div class="k">{E(line.get("label"))}</div>'
                   f'<div class="v">{items}</div></div>')

    exp = ""
    for r in d.get("experience", []):
        company = (r.get("company") or "").strip()
        head = E(r.get("title")) + (f", {E(company)}" if company else "")
        if company.lower() in PORTFOLIO:
            head += '<span class="arw">&#8599;</span>'
        else:
            head += " "
        # Dates render with a hyphen, like the BCV, whatever the model emitted.
        dates = (r.get("dates") or "").strip().replace("–", "-").replace("—", "-")
        meta = [x for x in ((r.get("qualifier") or "").strip(), dates) if x]
        if meta:
            head += f'<span class="when">({E(", ".join(meta))})</span>'
        # One justified paragraph per role, like the BCV — no bullet glyphs.
        body = " ".join(b.strip() for b in r.get("bullets", []) if b and b.strip())
        exp += (f'<div class="role"><h3>{head}</h3>'
                f'<p>{E(body)}</p></div>')

    bits = []
    for e in d.get("education", []):
        detail = (e.get("detail") or "").strip()
        dates = (e.get("dates") or "").strip().replace("–", "-").replace("—", "-")
        piece = E(e.get("institution"))
        if detail:
            piece += f", {E(detail)}"
        if dates:
            piece += f' <span class="when">({E(dates)})</span>'
        bits.append(piece)
    edu = '<div class="edu">' + " &nbsp;·&nbsp; ".join(bits) + "</div>" if bits else ""

    out = tpl
    for k, v in {
        "__NAME__": "Cesar Garcia",
        "__HEADLINE__": E(d.get("headline")),
        "__CONTACT__": ('<b><a href="https://cesarxdesign.com">cesarxdesign.com</a></b> - '
                        '<a href="mailto:cesarxdesign@gmail.com">cesarxdesign@gmail.com</a> - '
                        '<a href="https://linkedin.com/in/cesarxdesign">linkedin.com/in/cesarxdesign</a>'),
        "__LOCATION__": "Lisbon, Portugal - Remote across Europe - Portuguese (EU) citizen",
        "__SUMMARY__": summary,
        "__SKILLS__": skills,
        "__EXPERIENCE__": exp,
        "__EDUCATION__": edu,
        "__FIT__": f"{fit:.3f}",
    }.items():
        out = out.replace(k, v)
    if filler:
        # The fit loop's measuring stick: a spacer used only in probe prints,
        # never in a shipped PDF. The trailing dot matters: Chrome silently
        # drops a final page that carries no ink, so a bare spacer can never
        # push the page count up and the probe would always read "fits".
        out = out.replace("</body>",
                          f'<div style="height:{int(filler)}px"></div>'
                          f'<div style="font-size:2px;line-height:2px">.</div></body>')
    return out


def slug(s, fallback="role", limit=70):
    keep = string.ascii_letters + string.digits + " -_"
    s = "".join(c for c in (s or "") if c in keep).strip()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-").lower()
    return s[:limit].strip("-") or fallback


def folder_name(company, role):
    """'Finom' + 'Senior Product Designer (German-speaking) Remote'
       -> 'finom-senior-product-designer-german-speaking-remote'
       The company is not repeated if the role title already carries it."""
    c, r = slug(company, ""), slug(role, "")
    if c and r:
        # drop the company from the role title wherever it appears
        r = re.sub(r"-?" + re.escape(c) + r"-?", "-", r).strip("-")
        r = re.sub(r"-{2,}", "-", r)
    name = "-".join(x for x in (c, r) if x)
    return slug(name, "application")


def pdf_pages(path):
    """Page count without a PDF library — good enough for Chrome's output."""
    try:
        with open(path, "rb") as f:
            blob = f.read()
        counts = [int(m) for m in re.findall(rb"/Count\s+(\d+)", blob)]
        if counts:
            return max(counts)
        return len(re.findall(rb"/Type\s*/Page[^s]", blob)) or 1
    except Exception:
        return 0


def _pdf_finished(path, floor=1200):
    """A PDF Chrome has finished writing ends with %%EOF. A half-written one
    does not, which is how we tell 'still printing' from 'done'."""
    try:
        if os.path.getsize(path) < floor:
            return False
        with open(path, "rb") as f:
            f.seek(-2048, os.SEEK_END)
            return b"%%EOF" in f.read()
    except OSError:
        return False


# Chrome prints the PDF in a second or two and then, on some macOS builds,
# simply never exits: the profile teardown hangs and the process sits there
# until it is killed. Waiting on the exit code meant a perfectly good CV ended
# in a 120-second timeout error. So: watch the file, not the process. The
# moment a complete PDF is on disk we kill Chrome and carry on.
PRINT_DEADLINE = 60          # seconds to wait for the file to appear
PRINT_SETTLE   = 0.25        # poll interval


def _print_once(argv, pdf_path, log_path):
    try:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
    except OSError:
        pass

    # Chrome is chatty on stderr. Give it a file, never a pipe: an unread pipe
    # fills and blocks the very process we are waiting on.
    with open(log_path, "w+", encoding="utf-8", errors="replace") as log:
        # Own process group. Chrome forks renderer and GPU helpers; killing only
        # the parent would leave those behind to pile up over a day of applying.
        p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        t0, size, stable = time.time(), -1, 0
        try:
            while True:
                exited = p.poll() is not None
                cur = os.path.getsize(pdf_path) if os.path.exists(pdf_path) else -1
                if cur > 0 and cur == size and _pdf_finished(pdf_path):
                    stable += 1
                    if stable >= 2 or exited:
                        return True, ""
                else:
                    stable = 0
                size = cur
                if exited:
                    return _pdf_finished(pdf_path), _tail(log_path)
                if time.time() - t0 > PRINT_DEADLINE:
                    return _pdf_finished(pdf_path), (
                        "Chrome ran for %ds without finishing the PDF.\n%s"
                        % (PRINT_DEADLINE, _tail(log_path)))
                time.sleep(PRINT_SETTLE)
        finally:
            _kill_group(p)


def _kill_group(p):
    """Take the whole process group down, Chrome's helpers included."""
    if p.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(p.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.kill()
            except OSError:
                pass
        try:
            p.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _tail(path, n=600):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except OSError:
        return ""


def make_pdf(html_str, basename=None, out_path=None):
    """Print html_str to a PDF. Either into the application folder under
    OUT_DIR (basename), or to an explicit path (the fit loop's probes)."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(
            "Chrome not found. Install Google Chrome, or set the path in "
            "CHROME_CANDIDATES at the top of server.py."
        )
    if out_path:
        pdf_path = out_path
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    else:
        # basename is the application folder; the file inside is always the same
        # name, so whatever a recruiter downloads is called "Cesar Garcia CV.pdf".
        folder = os.path.join(OUT_DIR, basename)
        os.makedirs(folder, exist_ok=True)
        pdf_path = os.path.join(folder, PDF_FILENAME + ".pdf")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "cv.html")
        with open(src, "w", encoding="utf-8") as f:
            f.write(html_str)
        log = os.path.join(tmp, "chrome.log")
        base = [chrome, "--disable-gpu", "--no-sandbox",
                f"--user-data-dir={os.path.join(tmp, 'profile')}",
                # a throwaway profile that must not phone home, update itself,
                # or run first-run chores: all of it only delays the exit
                "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
                "--disable-extensions", "--disable-background-networking",
                "--disable-sync", "--disable-component-update", "--disable-default-apps",
                "--disable-client-side-phishing-detection", "--disable-crash-reporter",
                "--mute-audio", "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}", "file://" + src]
        last = ""
        for flag in ("--headless=new", "--headless"):
            ok, err = _print_once([base[0], flag] + base[1:], pdf_path, log)
            if ok:
                return pdf_path
            last = err
        raise RuntimeError("Chrome failed to produce a PDF.\n" + last)


# ---- the fit loop's measuring instruments ---------------------------------

def _probe_path():
    os.makedirs(WORK_DIR, exist_ok=True)
    return os.path.join(WORK_DIR, "probe.pdf")


def probe_pages(doc):
    """How many pages does this content really take? Real print path, fit 1.0."""
    return pdf_pages(make_pdf(render_html(doc), out_path=_probe_path()))


def probe_slack(doc, budget):
    """White space at the bottom of the last allowed page, in px, measured on
    the REAL print path: binary-search the tallest spacer that still fits.
    No screen-vs-print approximation — the measuring stick goes through the
    same Chrome print pipeline as the shipped PDF."""
    lo, hi = 0, int(PAGE_PX)
    if pdf_pages(make_pdf(render_html(doc, filler=0), out_path=_probe_path())) > budget:
        return -1
    while hi - lo > 6:
        mid = (lo + hi) // 2
        p = make_pdf(render_html(doc, filler=mid), out_path=_probe_path())
        if pdf_pages(p) <= budget:
            lo = mid
        else:
            hi = mid
    return lo


def build_pdf(doc, basename, budget):
    """Print the finished CV, design untouched, at fit 1.0. The content was
    already fitted by the pipeline; this reports honestly if it still spills."""
    budget = int(budget or 1)
    path = make_pdf(render_html(doc), basename)
    pages = pdf_pages(path)
    return path, pages, 1.0, False


def read_history():
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def log_run(entry):
    items = read_history()
    # one row per application: a re-run of the same job replaces its row
    items = [x for x in items if x.get("folder") != entry.get("folder")]
    items.insert(0, entry)
    items = items[:200]
    try:
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
    except Exception as e:
        sys.stderr.write("  could not write history: %s\n" % e)
    return items


# --------------------------------------------------------------------------
# the create flow (shared by the sync API and the UI's background job)
# --------------------------------------------------------------------------

def create_flow(jd, budget, model=None, facts=None, progress=None):
    say = progress or (lambda *_: None)
    doc, meta = tune_pipeline(jd, budget, model, say)
    doc["_jd_chars"] = len(jd)

    os.makedirs(WORK_DIR, exist_ok=True)
    with open(os.path.join(WORK_DIR, "preview.html"), "w", encoding="utf-8") as f:
        f.write(render_html(doc))

    notes = doc.get("tuning_notes", {})
    base = folder_name(notes.get("company", ""), notes.get("job_title", ""))
    say("printing the PDF…")
    p, pages, fit, fitted = build_pdf(doc, base, budget)
    cov = meta.get("coverage") or {}
    facts = facts or {}
    log_run({
        "at": datetime.now().isoformat(timespec="minutes"),
        "company": notes.get("company", ""),
        "role": notes.get("job_title", ""),
        "seniority": notes.get("seniority", ""),
        "salary": str(facts.get("salary") or ""),
        "remote": str(facts.get("remote") or ""),
        "country": str(facts.get("country") or ""),
        "portugal": str(facts.get("portugal") or ""),
        "europe": str(facts.get("europe") or ""),
        "years": str(facts.get("years_experience") or ""),
        "pages": pages,
        "budget": budget,
        "fit": fit,
        "filled": meta.get("filled"),
        "cov": f"{cov.get('tuned', 0)}/{cov.get('total', 0)}" if cov.get("total") else "",
        "cov_base": (f"{cov['baseline']}/{cov['total']}"
                     if cov.get("baseline") is not None and cov.get("total") else ""),
        "overflow": pages > budget,
        "unmet": len(notes.get("unmet_requirements") or []),
        "jd_chars": len(jd),
        "folder": os.path.dirname(p),
        "path": p,
        "url": "/output/" + quote(os.path.relpath(p, OUT_DIR)),
    })
    say("done")
    return {
        "tcv": doc,
        "coverage": cov,
        "preview": "/preview.html",
        "url": "/output/" + quote(os.path.relpath(p, OUT_DIR)),
        "path": p,
        "folder": os.path.dirname(p),
        "filename": os.path.basename(p),
        "bytes": os.path.getsize(p),
        "pages": pages,
        "budget": budget,
        "fit": fit,
        "fitted": fitted,
        "filled": meta.get("filled"),
        "slack_px": meta.get("slack_px"),
        "overflow": pages > budget,
    }


def _resolve_jd(b, progress=None):
    """Body → advert text. Accepts jd text, a url field, or the one-box input."""
    jd = (b.get("jd") or "").strip()
    url = (b.get("url") or "").strip()
    if not jd and not url:
        jd, url = split_input(b.get("input"))
    gate = None
    if url and not jd:
        jd, gate, _rung = fetch_jd_ladder(url, progress)
    return jd, url, gate


# --------------------------------------------------------------------------
# background jobs (the UI's path — a 10-minute tune must not live or die
# with one long browser request, and progress belongs on screen)
# --------------------------------------------------------------------------

JOBS = {}
JOBS_LOCK = threading.Lock()


def start_job(fn):
    jid = "%x%s" % (int(time.time() * 1000), os.urandom(2).hex())
    with JOBS_LOCK:
        # prune: keep the newest 30
        for k in sorted(JOBS, key=lambda k: JOBS[k]["t0"])[:-29]:
            if JOBS[k]["status"] != "running":
                JOBS.pop(k, None)
        JOBS[jid] = {"status": "running", "log": [], "result": None,
                     "error": "", "t0": time.time()}

    def say(msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        sys.stderr.write("  job %s: %s\n" % (jid, msg))
        with JOBS_LOCK:
            JOBS[jid]["log"].append(line)

    def run():
        try:
            result = fn(say)
            with JOBS_LOCK:
                JOBS[jid].update(status="done", result=result)
        except Exception as e:
            sys.stderr.write("  job %s FAILED: %s\n" % (jid, e))
            with JOBS_LOCK:
                JOBS[jid].update(status="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jid


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stderr.write("  %s\n" % (fmt % a))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                return self._send(200, read("ui.html"), "text/html; charset=utf-8")
            if path == "/api/health":
                return self._json(200, {
                    "api_key": bool(api_key()),
                    "chrome": find_chrome() or "",
                    "master_bytes": len(read("master_cv.md")),
                    "baseline": bool(baseline_text()),
                    "out_dir": OUT_DIR,
                    "cli": bool(have_cli()),
                    "api": API,
                })
            if path == "/api/history":
                return self._json(200, {"items": read_history()})
            if path == "/api/job":
                q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                jid = (q.get("id") or [""])[0]
                with JOBS_LOCK:
                    j = JOBS.get(jid)
                    if not j:
                        return self._json(404, {"error": "no such job"})
                    return self._json(200, {
                        "status": j["status"], "log": j["log"][-40:],
                        "result": j["result"] if j["status"] == "done" else None,
                        "error": j["error"],
                        "seconds": int(time.time() - j["t0"]),
                    })

            if path == "/api/models":
                return self._json(200, {"models": list_models()})
            if path == "/preview.html":
                fp = os.path.join(WORK_DIR, "preview.html")
                if not os.path.isfile(fp):
                    return self._json(404, {"error": "nothing rendered yet"})
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path.startswith("/output/"):
                rel = unquote(path[len("/output/"):])
                fp = os.path.realpath(os.path.join(OUT_DIR, rel))
                # never serve outside output/
                if not fp.startswith(os.path.realpath(OUT_DIR) + os.sep):
                    return self._json(404, {"error": "not found"})
                fn = os.path.basename(fp)
                if not os.path.isfile(fp):
                    return self._json(404, {"error": "not found"})
                with open(fp, "rb") as f:
                    data = f.read()
                ctype = "application/pdf" if fn.endswith(".pdf") else "text/html; charset=utf-8"
                disp = {"Content-Disposition": f'inline; filename="{fn}"'}
                return self._send(200, data, ctype, disp)
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            b = self._body()

            if path == "/api/parse":
                jd, url, gate = _resolve_jd(b)
                if len(jd) < 120:
                    got = "a link" if url else ("%d characters" % len(jd))
                    return self._json(400, {
                        "error": "Nothing usable came through (%s). If you pasted a link, the page "
                                 "may block fetching or need a login: paste the advert text instead."
                                 % got})
                out = gate or parse_jd(jd)
                out["jd_chars"] = len(jd)
                out["jd"] = jd          # hand the resolved text back so Create does not refetch
                out["excerpt"] = jd[:400]
                return self._json(200, out)

            if path == "/api/create":
                # The whole pipeline: analyse, write, audit, fit, print.
                budget = 2 if str(b.get("pages")) == "2" else 1
                model = b.get("model") or None
                facts = b.get("facts") or {}

                if b.get("async"):
                    body = dict(b)

                    def job(say):
                        jd, url, gate = _resolve_jd(body, say)
                        if len(jd) < 120:
                            raise RuntimeError("Job description is too short to tune against.")
                        return create_flow(jd, budget, model, facts, say)

                    return self._json(200, {"job": start_job(job)})

                jd, url, gate = _resolve_jd(b)
                if len(jd) < 120:
                    return self._json(400, {"error": "Job description is too short to tune against."})
                return self._json(200, create_flow(jd, budget, model, facts))

            if path == "/api/run-radar":
                # On-demand full radar pass: scrape, then the morning runner
                # (facts, hunts, CVs). Fire-and-forget; the dashboard polls
                # jobs.json for the result like any other run.
                global _radar_proc
                radar = os.path.expanduser(os.path.join("~", "Claude", "jobradar"))
                if not os.path.isfile(os.path.join(radar, "scrape.py")):
                    return self._json(400, {"error": "jobradar not found at " + radar})
                if _radar_proc is not None and _radar_proc.poll() is None:
                    return self._json(200, {"ok": True, "already_running": True})
                logf = open(os.path.join(radar, "runradar.log"), "ab")
                _radar_proc = subprocess.Popen(
                    ["/bin/sh", "-c",
                     "cd '%s' && /usr/bin/python3 scrape.py && /usr/bin/python3 nightcv.py" % radar],
                    stdout=logf, stderr=subprocess.STDOUT)
                return self._json(200, {"ok": True, "started": True})

            if path == "/api/reveal":
                # Show the finished PDF in Finder. Local machine, local file.
                target = (b.get("path") or "").strip()
                root = os.path.realpath(OUT_DIR)
                if not os.path.realpath(target).startswith(root):
                    return self._json(400, {"error": "outside the output folder"})
                subprocess.run(["open", "-R", target], check=False)
                return self._json(200, {"ok": True})

            if path == "/api/key":
                # Written by the user, in their own local app, to their own disk.
                k = (b.get("key") or "").strip()
                if not k.startswith("sk-"):
                    return self._json(400, {"error": "That doesn't look like an Anthropic key — they start with sk-."})
                fp = os.path.join(HERE, "api_key.txt")
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(k)
                os.chmod(fp, 0o600)          # readable only by you
                return self._json(200, {"ok": True, "path": fp})

            if path == "/api/render":
                doc = b.get("tcv") or {}
                out = render_html(doc)
                os.makedirs(WORK_DIR, exist_ok=True)
                with open(os.path.join(WORK_DIR, "preview.html"), "w", encoding="utf-8") as f:
                    f.write(out)
                return self._json(200, {"url": "/preview.html"})

            if path == "/api/pdf":
                doc = b.get("tcv") or {}
                notes = doc.get("tuning_notes", {})
                company = (b.get("company") or notes.get("company") or "").strip()
                role = (b.get("label") or notes.get("job_title") or "").strip()
                base = folder_name(company, role)
                budget = int(b.get("pages") or 1)
                p, pages, fit, fitted = build_pdf(doc, base, budget)
                rel = os.path.relpath(p, OUT_DIR)
                return self._json(200, {
                    "url": "/output/" + quote(rel),
                    "path": p,
                    "folder": os.path.dirname(p),
                    "filename": os.path.basename(p),
                    "bytes": os.path.getsize(p),
                    "pages": pages,
                    "budget": budget,
                    "fit": fit,
                    "fitted": fitted,
                    "overflow": pages > budget,
                })

            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": str(e)})


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for f in ("ui.html", "cv_template.html", "tuner_prompt.md", "master_cv.md"):
        if not os.path.exists(os.path.join(HERE, f)):
            sys.exit(f"Missing required file: {f}")

    print("\n  TCV — Tuned CV generator")
    print("  " + "-" * 46)
    print(f"  master_cv.md   {len(read('master_cv.md')):,} bytes")
    if have_cli():
        auth = "long-lived token" if oauth_token() else "interactive login"
        print(f"  Tuning         Claude Code CLI — subscription, via {auth}")
    else:
        print(f"  Tuning         Anthropic API {'(key found)' if api_key() else '— NO KEY, see README'}")
    print(f"  Baseline       {'baseline_cv.txt found — coverage deltas on' if baseline_text() else 'no baseline_cv.txt — coverage deltas off'}")
    print(f"  Chrome         {find_chrome() or 'NOT FOUND — see README'}")
    print(f"  Output         {OUT_DIR}")
    print(f"\n  →  http://localhost:{PORT}\n")

    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped\n")
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):
            sys.exit(f"  Port {PORT} is already in use. "
                     f"Try:  TCV_PORT=8766 python3 server.py\n")
        raise


if __name__ == "__main__":
    main()
