#!/usr/bin/env python3
"""
TCV — Tuned CV generator.

Local web app. Paste a job description (or a URL), get a parse-safe PDF
tuned to that job, built only from the verified master CV.

Zero third-party dependencies: Python 3 standard library + the Chrome
you already have installed.

    python3 server.py            # then open http://localhost:8765

Requires an Anthropic API key, from either:
    export ANTHROPIC_API_KEY=sk-ant-...
or a file named  api_key.txt  next to this script.
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
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote, unquote

HERE = os.path.dirname(os.path.abspath(__file__))

# Finished PDFs land on the Desktop, one folder per application, because that
# is where you actually go to attach a file. Override with TCV_OUT=/some/path.
_radar_proc = None  # /api/run-radar single-flight guard

OUT_DIR = os.path.expanduser(
    os.environ.get("TCV_OUT") or os.path.join("~", "Desktop", "TCV")
)
# Scratch: the live preview the UI renders in the iframe. Stays next to the
# app so it never clutters the Desktop.
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
API = 5
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
        with urllib.request.urlopen(req, timeout=300) as r:
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


TCV_TOOL = {
    "name": "emit_tcv",
    "description": "Emit the tuned CV.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "Title line under the name."},
            "summary": {
                "type": "array", "items": {"type": "string"},
                "description": "1-2 short paragraphs.",
            },
            "skills": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["label", "items"],
                },
            },
            "experience": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "company": {"type": "string"},
                        "qualifier": {
                            "type": "string",
                            "description": "e.g. 'Self-employed'. Empty string if none.",
                        },
                        "dates": {"type": "string", "description": "MMM YYYY – MMM YYYY"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "company", "qualifier", "dates", "bullets"],
                },
            },
            "education": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "institution": {"type": "string"},
                        "detail": {"type": "string"},
                        "dates": {"type": "string"},
                    },
                    "required": ["institution", "detail", "dates"],
                },
            },
            "tuning_notes": {
                "type": "object",
                "properties": {
                    "job_title": {"type": "string"},
                    "company": {
                        "type": "string",
                        "description": "The hiring company's name, as the JD writes it. "
                                       "Empty string only if the JD genuinely never names it.",
                    },
                    "seniority": {"type": "string"},
                    "matched_terms": {"type": "array", "items": {"type": "string"}},
                    "led_with": {"type": "string"},
                    "compressed_or_cut": {"type": "array", "items": {"type": "string"}},
                    "unmet_requirements": {
                        "type": "array", "items": {"type": "string"},
                        "description": "JD requirements Cesar does NOT meet. Be honest.",
                    },
                },
                "required": ["job_title", "company", "seniority", "matched_terms",
                             "led_with", "compressed_or_cut", "unmet_requirements"],
            },
        },
        "required": ["headline", "summary", "skills", "experience",
                     "education", "tuning_notes"],
    },
}


def _user_message(jd_text, pages, note=""):
    return (
        "<MASTER_CV>\n" + read("master_cv.md") + "\n</MASTER_CV>\n\n"
        "<JOB_DESCRIPTION>\n" + jd_text.strip() + "\n</JOB_DESCRIPTION>\n\n"
        f"<PAGE_BUDGET>{pages}</PAGE_BUDGET>\n\n"
        + (("<REJECTED_PREVIOUS_ATTEMPT>\n" + note +
            "\n</REJECTED_PREVIOUS_ATTEMPT>\n\n") if note else "")
        + "Produce the tuned CV."
    )


def _extract_json(text):
    """Pull the first complete {...} object out of a model's reply."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    if start < 0:
        raise RuntimeError("No JSON in the reply.\n" + text[:600])
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
    raise RuntimeError("Truncated JSON in the reply.\n" + text[:600])


# ---- backend 1: Claude Code CLI (runs on your subscription, no API credits) --

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


SCHEMA_NOTE = """

---

Return ONE JSON object and absolutely nothing else — no prose, no markdown fence,
no explanation before or after. It must match this shape exactly:

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


def tune_via_cli(jd_text, pages, model=None, note=""):
    prompt = (read("tuner_prompt.md") + SCHEMA_NOTE + "\n\n" +
              _user_message(jd_text, pages, note))
    # Accuracy matters more than speed here — this document goes to employers.
    model = model or os.environ.get("TCV_MODEL") or "opus"
    cmd = [have_cli(), "-p", "--output-format", "json",
           "--model", model,
           "--disallowed-tools", "Bash Edit Write Read WebFetch WebSearch"]
    # The CLI prefers ANTHROPIC_API_KEY over the OAuth login when both exist,
    # which would quietly bill per call. Strip it: this path is the subscription.
    child_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    p = subprocess.run(cmd, input=prompt, capture_output=True,
                       text=True, timeout=600, env=child_env)
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
    out = _extract_json(env.get("result") or "")
    # modelUsage lists every model the CLI touched; the one that actually wrote
    # the CV is the one with the most output tokens.
    usage = env.get("modelUsage") or {}
    main = max(usage.items(), key=lambda kv: (kv[1] or {}).get("outputTokens", 0))[0] if usage else model
    out["_model"] = f"{main} (subscription)"
    return out


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
    if have_cli():
        cmd = [have_cli(), "-p", "--output-format", "json",
               "--model", os.environ.get("TCV_PARSE_MODEL") or "haiku",
               "--disallowed-tools", "Bash Edit Write Read WebFetch WebSearch"]
        child_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = subprocess.run(cmd, input=PARSE_PROMPT + "\n\n" + jd_text[:20000],
                           capture_output=True, text=True, timeout=180, env=child_env)
        if p.returncode != 0:
            raise RuntimeError(_cli_error(p))
        env = json.loads(p.stdout)
        if env.get("is_error"):
            class _P:
                stdout, stderr = json.dumps(env), ""
            raise RuntimeError(_cli_error(_P))
        return _extract_json(env.get("result") or "")

    payload = {
        "model": pick_model(),
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": PARSE_PROMPT + "\n\n" + jd_text[:20000]}],
    }
    resp = api_request(API_URL, payload, method="POST")
    text = "".join(b.get("text", "") for b in resp.get("content", []))
    return _extract_json(text)


# ---- backend 2: Anthropic API (pay per call) --------------------------------

def tune_via_api(jd_text, pages, model=None, note=""):
    model = model or pick_model()
    payload = {
        "model": model,
        "max_tokens": 8000,
        "system": read("tuner_prompt.md"),
        "tools": [TCV_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_tcv"},
        "messages": [{"role": "user", "content": _user_message(jd_text, pages, note)}],
    }
    resp = api_request(API_URL, payload, method="POST")
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_tcv":
            out = block["input"]
            out["_model"] = model + " (API)"
            return out
    raise RuntimeError("Model did not return a tuned CV.\n" + json.dumps(resp)[:900])


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

def tune(jd_text, pages, model=None, note=""):
    """Prefer the Claude Code CLI — it bills to your subscription, not per call.
    Fall back to the API only when the CLI is absent or fails."""
    backend = (os.environ.get("TCV_BACKEND") or "auto").lower()

    if backend != "api" and have_cli():
        # No silent fallback to the paid API when the CLI is present but broken —
        # that hides the real error behind a billing one. Fix the CLI instead.
        return tune_via_cli(jd_text, pages, model, note)

    if not api_key():
        raise RuntimeError(
            "Can't find the Claude Code CLI, and there's no API key either.\n\n"
            "Claude Code is the free path — it runs on your subscription. Install "
            "it with `npm i -g @anthropic-ai/claude-code`, run `claude` once to "
            "sign in, then restart TCV."
        )
    return tune_via_api(jd_text, pages, model, note)


# --------------------------------------------------------------------------
# JD fetching
# --------------------------------------------------------------------------

BLOCKED_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.")


URLISH = re.compile(r"^(https?://\S+|[\w-]+(\.[\w-]+)+(/\S*)?)$", re.I)


def split_input(raw):
    """One box, two kinds of input. A single token that looks like an address is
    a URL; anything else is the advert text."""
    raw = (raw or "").strip()
    if raw and len(raw.split()) == 1 and "\n" not in raw and URLISH.match(raw):
        return "", raw if raw.lower().startswith("http") else "https://" + raw
    return raw, ""


def fetch_jd(url):
    for h in BLOCKED_HOSTS:
        if h in url:
            raise RuntimeError(
                f"{h} blocks automated fetching. Open the posting, select all, "
                "and paste the text instead — it works just as well."
            )
    req = urllib.request.Request(url)
    req.add_header("User-Agent",
                   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read().decode(errors="replace")
    raw = re.sub(r"(?is)<(script|style|nav|footer|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", raw)
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t\xa0]+", " ", txt)
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
    txt = "\n".join(l.strip() for l in txt.splitlines())
    txt = txt.strip()
    if len(txt) < 250:
        raise RuntimeError(
            "That page returned almost no text — it is probably rendered by "
            "JavaScript. Paste the job description text instead."
        )
    return txt[:60000]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

E = lambda s: html.escape(str(s or ""), quote=False)


# Roles with a live case study on cesarxdesign.com get the ↗ mark, exactly as
# the BCV does. Trust Wallet is deliberately absent — teaser card, no page.
PORTFOLIO = {"confirmo", "done", "done workouts", "mara", "cable", "cable tech",
             "penfold", "starcount"}


def render_html(d, fit=1.0, vj=1.0):
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
        dates = (r.get("dates") or "").strip().replace("\u2013", "-").replace("\u2014", "-")
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
        dates = (e.get("dates") or "").strip().replace("\u2013", "-").replace("\u2014", "-")
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
        "__VJUST__": f"{vj:.3f}",
    }.items():
        out = out.replace(k, v)
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


def make_pdf(html_str, basename):
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(
            "Chrome not found. Install Google Chrome, or set the path in "
            "CHROME_CANDIDATES at the top of server.py."
        )
    # basename is the application folder; the file inside is always the same name,
    # so whatever a recruiter downloads is called "Cesar Garcia CV.pdf".
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
# fit-to-budget
# --------------------------------------------------------------------------

# Steps the CSS --fit scale down until the PDF meets the page budget.
# 0.86 is the floor: below that the type is too small to hand to a human,
# and the honest answer is "this content does not fit, cut a bullet".
# Search from large to small and take the LARGEST fit that still fits the page
# budget. Steps above 1.0 let a short (under-written) CV grow to fill the page
# instead of sitting tiny with white space at the bottom; steps below 1.0 shrink
# an over-long one as before. Either way the page ends up full.
FIT_STEPS = (1.30, 1.25, 1.20, 1.15, 1.11, 1.08, 1.05, 1.03, 1.0,
             0.975, 0.95, 0.925, 0.90, 0.88, 0.86, 0.84, 0.82)


VJUST_STEPS = (1.0, 1.15, 1.3, 1.5, 1.7, 1.9, 2.1, 2.4, 2.7, 3.0)

def build_pdf(doc, basename, budget):
    """Render, measure, shrink, repeat, then SPREAD to fill. Returns
    (path, pages, fit, fitted). First pick the largest font fit that lands
    inside the page budget; then open up the gaps between sections and roles
    (vj) as far as they go without spilling to a new page, so the page ends
    up full instead of stopping two thirds down."""
    budget = int(budget or 1)
    path = pages = None
    for i, fit in enumerate(FIT_STEPS):
        path = make_pdf(render_html(doc, fit, 1.0), basename)
        pages = pdf_pages(path)
        if pages <= budget:
            # fill the remaining white space by spreading the vertical gaps
            best = path
            for vj in VJUST_STEPS[1:]:
                p = make_pdf(render_html(doc, fit, vj), basename)
                if pdf_pages(p) <= budget:
                    best = p
                else:
                    break
            return best, budget, fit, i > 0
    return path, pages, FIT_STEPS[-1], True


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
                    "out_dir": OUT_DIR,
                    "cli": bool(have_cli()),
                    "api": API,
                })
            if path == "/api/history":
                return self._json(200, {"items": read_history()})

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
                jd = (b.get("jd") or "").strip()
                url = (b.get("url") or "").strip()
                if not jd and not url:
                    jd, url = split_input(b.get("input"))
                if url and not jd:
                    jd = fetch_jd(url)
                if len(jd) < 120:
                    got = "a link" if url else ("%d characters" % len(jd))
                    return self._json(400, {
                        "error": "Nothing usable came through (%s). If you pasted a link, the page "
                                 "may block fetching or need a login: paste the advert text instead."
                                 % got})
                out = parse_jd(jd)
                out["jd_chars"] = len(jd)
                out["jd"] = jd          # hand the resolved text back so Create does not refetch
                out["excerpt"] = jd[:400]
                return self._json(200, out)

            if path == "/api/create":
                # One shot: tune, preview, print. This is what the button calls.
                jd = (b.get("jd") or "").strip()
                url = (b.get("url") or "").strip()
                if not jd and not url:
                    jd, url = split_input(b.get("input"))
                if url and not jd:
                    jd = fetch_jd(url)
                if len(jd) < 120:
                    return self._json(400, {"error": "Job description is too short to tune against."})
                budget = 2 if str(b.get("pages")) == "2" else 1

                doc = tune(jd, budget, b.get("model") or None)
                violations = verify_doc(doc)
                if violations:
                    doc = tune(jd, budget, b.get("model") or None,
                               note="Your previous CV was rejected by the fact "
                                    "verifier. Violations:\n- " + "\n- ".join(violations))
                    violations = verify_doc(doc)
                    if violations:
                        return self._json(500, {"error":
                            "Tuned CV failed the no-invention verifier twice:\n- "
                            + "\n- ".join(violations)})
                doc["_jd_chars"] = len(jd)

                os.makedirs(WORK_DIR, exist_ok=True)
                with open(os.path.join(WORK_DIR, "preview.html"), "w", encoding="utf-8") as f:
                    f.write(render_html(doc))

                notes = doc.get("tuning_notes", {})
                base = folder_name(notes.get("company", ""), notes.get("job_title", ""))
                p, pages, fit, fitted = build_pdf(doc, base, budget)
                if pages > budget:
                    # never ship an overflowing CV: one trim pass, model cuts
                    # the least valuable content, then re-render
                    try:
                        trimmed = tune("", budget, b.get("model") or None,
                            note=("The CV below overflows a %d-page budget even at "
                                  "minimum type size. Return the same JSON with the "
                                  "least valuable content removed until it plausibly "
                                  "fits. Cut whole bullets or skills, never facts "
                                  "inside a sentence.\n\n<CURRENT_CV_JSON>\n%s\n"
                                  "</CURRENT_CV_JSON>") % (budget, json.dumps(doc)))
                        if not verify_doc(trimmed):
                            p2, pg2, fit2, fitted2 = build_pdf(trimmed, base, budget)
                            if pg2 <= budget:
                                doc, p, pages, fit, fitted = trimmed, p2, pg2, fit2, fitted2
                    except Exception:
                        pass  # keep the overflowing original; flag stays loud

                notes = doc.get("tuning_notes", {})
                # the four facts the read pulled off the advert, carried through so the
                # history row can still answer "what did that one pay?" months later
                facts = b.get("facts") or {}
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
                    "overflow": pages > budget,
                    "unmet": len(notes.get("unmet_requirements") or []),
                    "jd_chars": len(jd),
                    "folder": os.path.dirname(p),
                    "path": p,
                    "url": "/output/" + quote(os.path.relpath(p, OUT_DIR)),
                })

                return self._json(200, {
                    "tcv": doc,
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
                    "overflow": pages > budget,
                })

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

            if path == "/api/tune":
                jd = (b.get("jd") or "").strip()
                url = (b.get("url") or "").strip()
                if url and not jd:
                    jd = fetch_jd(url)
                if len(jd) < 120:
                    return self._json(400, {"error": "Job description is too short to tune against."})
                pages = 2 if str(b.get("pages")) == "2" else 1
                d = tune(jd, pages, b.get("model") or None)
                violations = verify_doc(d)
                if violations:
                    d = tune(jd, pages, b.get("model") or None,
                             note="Your previous CV was rejected by the fact "
                                  "verifier. Violations:\n- " + "\n- ".join(violations))
                    violations = verify_doc(d)
                    if violations:
                        return self._json(500, {"error":
                            "Tuned CV failed the no-invention verifier twice:\n- "
                            + "\n- ".join(violations)})
                d["_jd_chars"] = len(jd)
                return self._json(200, d)

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
                # folder reads "finom-senior-product-designer-2026-08-22"
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
        print(f"  Tuning         Claude Code CLI — runs on your subscription")
    else:
        print(f"  Tuning         Anthropic API {'(key found)' if api_key() else '— NO KEY, see README'}")
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
