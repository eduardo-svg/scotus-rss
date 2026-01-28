#!/usr/bin/env python3
"""
Generate an RSS feed of ~300-word SCOTUS summaries (Background / Holding / Reasoning / Outcome)
from Cornell LII "Most Recent Decisions", using a Gemini AI Studio API key.

Usage:
  python scripts/generate_summary_feed.py 10

Env:
  GEMINI_API_KEY=...   (required)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from feedgen.feed import FeedGenerator
from google import genai


TOC_URL = "https://www.law.cornell.edu/supremecourt/text"
BASE = "https://www.law.cornell.edu"

CHANNEL_LINK = TOC_URL
TITLE = "Supreme Court of the United States — Recent Decisions (Summaries)"
DESC = "Background / Holding / Reasoning / Outcome summaries generated from Cornell LII."

UA = "scotus-rss-bot/1.0 (+https://github.com/)"
MODEL = "gemini-2.5-flash-lite"

CACHE_PATH = "data/summaries_cache.json"  # commit this file so summaries persist across runs

# --- XML safety (prevents lxml/feedgen crashes on control chars) ---
_invalid_xml_10 = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]")
def xml_safe(s: str) -> str:
    if s is None:
        return ""
    return _invalid_xml_10.sub("", str(s))

DECIDED_RE = re.compile(r"decided date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", re.I)
NO_RE = re.compile(r"\bNo\.\s*([^\s]+)", re.I)

def fetch(url: str) -> requests.Response:
    r = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r

def fetch_recent_cases(max_items: int):
    html = fetch(TOC_URL).text
    soup = BeautifulSoup(html, "lxml")

    h2 = soup.find("h2", string=lambda s: s and s.strip() == "Most Recent Decisions")
    if not h2:
        return []

    dl = h2.find_next("dl")
    if not dl:
        return []

    out = []
    for dt in dl.find_all("dt"):
        a = dt.find("a", href=True)
        if not a:
            continue

        title = a.get_text(" ", strip=True)
        url = urljoin(BASE, a["href"])

        dd = dt.find_next_sibling("dd")
        meta = dd.get_text(" ", strip=True) if dd else ""

        decided = ""
        m = DECIDED_RE.search(meta)
        if m:
            decided = m.group(1)

        docket = ""
        m2 = NO_RE.search(meta)
        if m2:
            docket = m2.group(1)

        out.append({"title": title, "url": url, "meta": meta, "decided": decided, "docket": docket})
        if len(out) >= max_items:
            break

    return out

def parse_decided_to_dt(decided: str) -> datetime:
    # Noon UTC prevents “previous day” displays in US time zones.
    if not decided:
        return datetime.now(timezone.utc)
    try:
        d = dtparser.parse(decided).date()
        return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)

def extract_case_text_for_llm(case_html: str) -> str:
    """
    Pull a large "content block" from the Cornell case page and convert to text.
    This intentionally keeps a lot of content (as requested), but removes nav chrome.
    """
    soup = BeautifulSoup(case_html, "lxml")

    # Prefer the opinion text area if present; fall back to broader containers
    main = (
        soup.select_one(".bodytext")
        or soup.select_one("#content1")
        or soup.select_one("main#main")
        or soup.select_one("main")
    )
    if not main:
        return ""

    # remove obvious chrome
    for sel in ["nav", "header", "footer", "aside", "script", "style"]:
        for x in main.select(sel):
            x.decompose()

    # Remove tab navigation if present
    for ul in main.select("ul"):
        txt = ul.get_text(" ", strip=True).lower()
        if "supreme court" in txt and any(a.get("href", "").startswith("#tab_") for a in ul.find_all("a")):
            ul.decompose()

    # In-page anchors (#...) are only navigation; keep marker text but drop the link
    for a in main.find_all("a", href=True):
        if a["href"].startswith("#"):
            a.unwrap()

    # Drop standalone "TOP" remnants (sometimes not a link after unwrapping)
    for t in list(main.stripped_strings):
        if t.upper() == "TOP":
            # best-effort: remove the exact text nodes equal to TOP
            for node in main.find_all(string=lambda s: isinstance(s, str) and s.strip().upper() == "TOP"):
                node.extract()
            break

    # Convert to plain text for the model
    text = main.get_text("\n", strip=True)

    # Light cleanup
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Cost-control / safety: cap the prompt size (still "whole block-ish" but bounded)
    # Adjust upward if you want.
    MAX_CHARS = 80_000
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[TRUNCATED]\n"

    return text

def load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)

def build_prompt(extracted_text: str) -> str:
    return "\n".join([
        "You are a careful legal editor. Do not invent facts; if missing, write 'Not stated.'",
        "Write ~300 words total (260–340). Plain English but legally precise. Avoid long quotes.",
        "",
        "IMPORTANT:",
        "- Do NOT include the case name/caption, docket number, court name, decided date, or source URL.",
        "- Do NOT start with 'In this case...' + caption. Assume metadata is shown elsewhere.",
        "",
        "Output EXACTLY these headings, in this order:",
        "Background:",
        "Holding:",
        "Reasoning:",
        "Outcome:",
        "",
        "Background should include procedural posture + what question the Court answered (if stated).",
        "Holding should be 1–2 sentences.",
        "Outcome must say affirmed/reversed/vacated/remanded and what happens next (if stated).",
        "",
        "Source text:",
        extracted_text,
    ])

def gemini_summarize(client: genai.Client, prompt: str) -> str:
    # Keep summaries consistent / cheap
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config={
            "temperature": 0.2,
            "max_output_tokens": 650,  # ~300 words-ish
        },
    )
    return (resp.text or "").strip()

def build_summary_rss(max_items: int) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY (or GOOGLE_API_KEY) in environment.")

    client = genai.Client(api_key=api_key)

    cache = load_cache()
    cases = fetch_recent_cases(max_items)

    fg = FeedGenerator()
    fg.id(CHANNEL_LINK)
    fg.title(TITLE)
    fg.link(href=CHANNEL_LINK, rel="alternate")
    fg.description(DESC)
    fg.language("en-us")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for case in cases:
        url = case["url"]
        decided = case.get("decided", "")
        cache_key = url  # stable

        summary = ""
        err = ""

        # Cache by URL + decided date (if Cornell updates dates, we regenerate)
        cached = cache.get(cache_key)
        if cached and cached.get("decided") == decided and cached.get("summary"):
            summary = cached["summary"]
        else:
            try:
                case_html = fetch(url).text
                extracted = extract_case_text_for_llm(case_html)
                if not extracted:
                    raise RuntimeError("Could not extract case text from page.")

                prompt = build_prompt(extracted)
                summary = gemini_summarize(client, prompt)

                if not summary:
                    raise RuntimeError("Empty summary from model.")

                cache[cache_key] = {
                    "decided": decided,
                    "summary": summary,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                err = str(e)
                summary = f"Background:\nNot stated.\n\nHolding:\nNot stated.\n\nReasoning:\nNot stated.\n\nOutcome:\nNot stated.\n\nError: {err}"

        fe = fg.add_entry()
        fe.id(xml_safe(url))
        fe.title(xml_safe(case.get("title", "Untitled")))
        fe.link(href=xml_safe(url))
        fe.pubDate(parse_decided_to_dt(decided))

        # Put summary in description, as plain text with newlines (RSS readers handle this well)
        fe.description(xml_safe(summary))

    save_cache(cache)
    return fg.rss_str(pretty=True).decode("utf-8")

def main():
    max_items = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    rss = build_summary_rss(max_items)

    os.makedirs("public", exist_ok=True)
    out_path = "public/summary.xml"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rss)

    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
