import re
import sys
import time
from datetime import datetime, timezone
from dateutil import parser as dtparser
from urllib.parse import urljoin
import os
import xml.etree.ElementTree as ET
from html import unescape
import markdown

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator


TOC_URL = "https://www.law.cornell.edu/supremecourt/text"
BASE = "https://www.law.cornell.edu"

CHANNEL_LINK = TOC_URL
TITLE = "Supreme Court of the United States - Recent Decisions"
DESC = "Most recent SCOTUS decisions, generated from Cornell LII."

UA = "scotus-rss-bot/1.0 (+https://github.com/)"

FEED_XML_PATH = "feed.xml"
SUMMARY_XML_PATH = "summary.xml"

# --- XML safety (prevents lxml/feedgen crashes on control chars) ---
_invalid_xml_10 = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]")
def xml_safe(s: str) -> str:
    if s is None:
        return ""
    return _invalid_xml_10.sub("", str(s))

DECIDED_RE = re.compile(r"decided date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", re.I)
NO_RE = re.compile(r"\bNo\.\s*([^\s]+)", re.I)

# ---------------- Gemini summarization ----------------

def build_prompt(extracted_text: str) -> str:
    return "\n".join([
        "You are a careful legal editor. Do not invent facts; if missing, write 'Not stated.'",
        "Write ~350 words total (300–400). Plain English but legally precise. Avoid long quotes. Use Markdown Formatting.",
        "",
        "IMPORTANT:",
        "- Do NOT include the case name/caption, docket number, court name, decided date, or source URL.",
        "- Do NOT start with 'In this case...' + caption. Assume metadata is shown elsewhere.",
        "",
        "Output ALL and EXACTLY these headings, in this order:",
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
def list_gemini_models(api_key: str, api_version: str = "v1") -> list[str]:
    # Lists models available to *your* API key
    url = f"https://generativelanguage.googleapis.com/{api_version}/models?key={api_key}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return [m["name"].replace("models/", "") for m in data.get("models", [])]

_retry_re = re.compile(r"retry in\s+([0-9.]+)s", re.I)

def maybe_sleep_retry_in(msg: str) -> bool:
    m = _retry_re.search(msg or "")
    if not m:
        return False
    seconds = float(m.group(1))
    time.sleep(seconds + 0.5)  # small cushion
    return True


def gemini_summarize(extracted_text: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY in environment.")

    prompt = build_prompt(extracted_text)
    api_version = os.getenv("GEMINI_API_VERSION", "v1beta")

    candidates = []
    if os.getenv("GEMINI_MODEL"):
        candidates.append(os.getenv("GEMINI_MODEL"))
    candidates += [
        "gemma-3-12b-it",
        "gemma-3-27b-it",
    ]

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1400},
    }

    last_err = None
    for model in candidates:
        url = f"https://generativelanguage.googleapis.com/{api_version}/models/{model}:generateContent?key={api_key}"

        for attempt in range(8):
            r = requests.post(url, json=payload, timeout=60)

            if r.status_code == 200:
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()

            # try next model name
            if r.status_code == 404:
                last_err = r.text
                break

            # rate/quota backoff
            if r.status_code in (429, 503):
                # Prefer explicit "retry in Xs" if present
                if maybe_sleep_retry_in(r.text):
                    continue
                # Otherwise, fall back to Retry-After header if present
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        time.sleep(float(ra) + 0.5)
                        continue
                    except ValueError:
                        pass

            raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:800]}")

    models = list_gemini_models(api_key, api_version=api_version)
    raise RuntimeError(
        "No candidate Gemini model worked for generateContent.\n"
        f"Tried: {candidates}\n"
        f"Models available to your key (sample): {models[:25]}\n"
        f"Last error: {last_err[:300] if last_err else 'None'}"
    )

# ---------------- Your existing scraping/feed code ----------------

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

def _append_style(tag, css: str) -> None:
    prev = (tag.get("style") or "").strip()
    if prev and not prev.endswith(";"):
        prev += ";"
    tag["style"] = (prev + " " + css).strip()

def honor_cornell_classes_inline(main) -> None:
    for el in main.find_all(True):
        classes = el.get("class") or []
        if not classes:
            continue

        if el.name == "span" and "smallcaps" in classes:
            _append_style(el, "font-variant: small-caps;")

        if "forcejy-center" in classes or "jy-center" in classes:
            _append_style(el, "text-align: center;")
        if "jy-right" in classes:
            _append_style(el, "text-align: right;")
        if "jy-both" in classes:
            _append_style(el, "text-align: justify;")

def force_center_headings(main) -> None:
    for h in main.find_all(["h1", "h2", "h3", "h4"]):
        prev = (h.get("style") or "").strip()
        if prev and not prev.endswith(";"):
            prev += ";"
        h["style"] = (prev + " text-align: center;").strip()

def extract_cornell_body_html(case_html: str) -> str:
    soup = BeautifulSoup(case_html, "lxml")

    main = soup.select_one("#content1") or soup.select_one("main#main") or soup.select_one("main")
    if not main:
        return ""

    for sel in ["nav", "header", "footer", "aside", "script", "style"]:
        for x in main.select(sel):
            x.decompose()

    honor_cornell_classes_inline(main)
    force_center_headings(main)

    allowed = {
        "p","br","hr","blockquote","pre","code","em","strong","b","i","u",
        "h1","h2","h3","h4",
        "ol","ul","li",
        "table","thead","tbody","tr","th","td",
        "a","sup","sub","span","div"
    }

    for tag in list(main.find_all(True)):
        if tag.name not in allowed:
            tag.unwrap()
        else:
            attrs = {}
            if tag.name == "a" and tag.get("href"):
                attrs["href"] = tag["href"]
            if tag.get("style"):
                attrs["style"] = tag["style"]
            tag.attrs = attrs

    return str(main)

def parse_decided_to_dt(decided: str) -> datetime:
    if not decided:
        return datetime.now(timezone.utc)

    try:
        d = dtparser.parse(decided).date()
        return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)

def build_rss(max_items: int) -> str:
    cases = fetch_recent_cases(max_items)

    fg = FeedGenerator()
    fg.id(CHANNEL_LINK)
    fg.title(TITLE)
    fg.link(href=CHANNEL_LINK, rel="alternate")
    fg.description(DESC)
    fg.language("en-us")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for c in cases:
        scrape_error = ""
        body_html = ""

        try:
            case_html = fetch(c["url"]).text
            body_html = extract_cornell_body_html(case_html)
            if not body_html.strip():
                raise RuntimeError("Could not extract body HTML.")
        except Exception as e:
            scrape_error = str(e)

        fe = fg.add_entry()
        fe.id(xml_safe(c["url"]))          # GUID
        fe.title(xml_safe(c["title"]))
        fe.link(href=xml_safe(c["url"]))
        fe.pubDate(parse_decided_to_dt(c.get("decided", "")))

        parts = [f'<p><a href="{xml_safe(c["url"])}">Cornell</a></p>']
        if c.get("meta"):
            parts.append(f"<p>{xml_safe(c['meta'])}</p>")
        if scrape_error:
            parts.append(f"<p><b>Error:</b> {xml_safe(scrape_error)}</p>")
        parts.append(body_html if body_html else "")

        fe.description(xml_safe("\n".join([p for p in parts if p])))

    return fg.rss_str(pretty=True).decode("utf-8")

# ---------------- Summary feed creation/updating ----------------

def html_to_text(html: str) -> str:
    # Turn the RSS description HTML into clean-ish plain text for the model.
    # (Also collapses whitespace.)
    soup = BeautifulSoup(html or "", "lxml")
    text = soup.get_text("\n", strip=True)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def parse_rss_items(feed_xml_path: str):
    """
    Returns a list of dicts: guid, title, link, pubDate, description_html
    """
    tree = ET.parse(feed_xml_path)
    root = tree.getroot()

    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = item.findtext("description") or ""
        items.append({
            "guid": guid,
            "title": title,
            "link": link,
            "pubDate": pub,
            "description_html": desc,
        })
    return items

def md_to_html(md: str) -> str:
    md = (md or "").strip()
    try:
        return markdown.markdown(md, extensions=["extra"])
    except Exception:
        # minimal fallback: bold/italics + paragraphs
        html = md
        html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
        html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
        # headings are literally "Background:" etc; turn into <h3>
        for h in REQUIRED_HEADINGS:
            html = html.replace(h, f"<h3>{h[:-1]}</h3>")
        # paragraph-ish
        parts = [p.strip() for p in html.split("\n\n") if p.strip()]
        return "\n".join(
            "<p>{}</p>".format(p.replace("\n", "<br/>"))
            for p in parts
        )


def load_existing_summary_guids(summary_xml_path: str) -> set[str]:
    if not os.path.exists(summary_xml_path):
        return set()

    tree = ET.parse(summary_xml_path)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        return set()

    guids = set()
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        if guid:
            guids.add(guid)
    return guids

def ensure_summary_feed_root(summary_xml_path: str) -> ET.ElementTree:
    """
    If summary.xml exists: parse & return.
    Else: create a new RSS 2.0 skeleton and return.
    """
    if os.path.exists(summary_xml_path):
        return ET.parse(summary_xml_path)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{TITLE} — Summaries"
    ET.SubElement(channel, "link").text = CHANNEL_LINK
    ET.SubElement(channel, "description").text = "Model-written summaries corresponding to the main feed items."
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    return ET.ElementTree(rss)

def append_summary_item(channel: ET.Element, src_item: dict, summary_text: str) -> None:
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "guid").text = xml_safe(src_item["guid"])
    ET.SubElement(item, "title").text = xml_safe(src_item["title"])
    ET.SubElement(item, "link").text = xml_safe(src_item["link"])
    if src_item.get("pubDate"):
        ET.SubElement(item, "pubDate").text = xml_safe(src_item["pubDate"])
    ET.SubElement(item, "description").text = xml_safe(summary_text)

def update_summary_feed(feed_xml_path: str, summary_xml_path: str) -> int:
    feed_items = parse_rss_items(feed_xml_path)
    existing = load_existing_summary_guids(summary_xml_path)

    missing = [it for it in feed_items if it["guid"] and it["guid"] not in existing]
    if not missing:
        return 0

    summary_tree = ensure_summary_feed_root(summary_xml_path)
    root = summary_tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("summary.xml missing <channel> root.")

    # Update build date
    lbd = channel.find("lastBuildDate")
    if lbd is None:
        lbd = ET.SubElement(channel, "lastBuildDate")
    lbd.text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    added = 0
    for it in missing:
        extracted_text = html_to_text(it.get("description_html", ""))

        # Optional: keep prompts bounded (avoids giant opinions).
        # Tune as you like.
        if len(extracted_text) > 30_000:
            extracted_text = extracted_text[:30_000] + "\n\n[Truncated]"

        summary_md = gemini_summarize(extracted_text)
        print(summary_md[0:400]) #TESTING
        summary_html = md_to_html(summary_md)
        append_summary_item(channel, it, summary_html)

        added += 1

    # Write summary.xml
    ET.indent(summary_tree, space="  ", level=0)  # Python 3.9+
    summary_tree.write(summary_xml_path, encoding="utf-8", xml_declaration=True)

    return added

# ---------------- main ----------------

def main():
    max_items = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    rss = build_rss(max_items)
    with open(FEED_XML_PATH, "w", encoding="utf-8") as f:
        f.write(rss)

    try:
        added = update_summary_feed(FEED_XML_PATH, SUMMARY_XML_PATH)
        print(f"summary.xml updated: {added} new item(s) added.")
    except Exception as e:
        # Don’t break feed generation if summarization fails.
        print(f"WARNING: summary feed update failed: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
