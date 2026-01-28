import re
import sys
from datetime import datetime, timezone
from dateutil import parser as dtparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator


TOC_URL = "https://www.law.cornell.edu/supremecourt/text"
BASE = "https://www.law.cornell.edu"

CHANNEL_LINK = TOC_URL
TITLE = "upreme Court of the United States - Recent Decisions"
DESC = "Most recent SCOTUS decisions, generated from Cornell LII."

UA = "scotus-rss-bot/1.0 (+https://github.com/)"

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

def extract_cornell_body_html(case_html: str) -> str:
    soup = BeautifulSoup(case_html, "lxml")

    # From your snippet, #content1 is the page content wrapper; main#main exists too.
    main = soup.select_one("#content1") or soup.select_one("main#main") or soup.select_one("main")
    if not main:
        return ""

    # Drop obvious chrome
    for sel in ["nav", "header", "footer", "aside", "script", "style"]:
        for x in main.select(sel):
            x.decompose()

    # Keep formatting-friendly tags, drop the rest
    allowed = {
        "p","br","hr","blockquote","pre","code","em","strong","b","i","u",
        "h1","h2","h3","h4",
        "ol","ul","li",
        "table","thead","tbody","tr","th","td",
        "a","sup","sub"
    }

    for tag in list(main.find_all(True)):
        if tag.name not in allowed:
            tag.unwrap()
        else:
            # keep only safe attrs
            attrs = {}
            if tag.name == "a" and tag.get("href"):
                attrs["href"] = tag["href"]
            tag.attrs = attrs

        # Center headings (SCOTUS-style)
    for h in main.find_all(["h1", "h2", "h3"]):
        h["style"] = (h.get("style", "") + "; text-align:center;").lstrip(";")

    return str(main)

def parse_decided_to_dt(decided: str) -> datetime:
    if not decided:
        return datetime.now(timezone.utc)
    try:
        return dtparser.parse(decided).astimezone(timezone.utc)
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
        fe.id(xml_safe(c["url"]))
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

def main():
    max_items = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    rss = build_rss(max_items)
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)

if __name__ == "__main__":
    main()
